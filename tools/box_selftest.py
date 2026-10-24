#!/usr/bin/env python3
"""On-box self test for padspeaker: is this machine ready to play sound on a pad's speaker?

Run it on the machine that has the pad (on Batocera: ``ssh root@batocera.local``, then
``cd /userdata/system/padspeaker && python3 tools/box_selftest.py``). It needs only the Python standard
library and the ``padspeaker`` package next to this folder.

Every check prints one line, ``PASS``, ``FAIL``, ``WARN`` or ``SKIP``, and the exit status is 0 unless a required check
FAILED:

  python           Python 3.9 or newer
  libsbc           libsbc.so.1 loads
  SBC frame        encoding 128 samples of silence gives a 112 byte frame that starts 9c 75 19
  pactl, parec     the PulseAudio command line tools are installed
  audio server     pactl can talk to PipeWire/PulseAudio (with XDG_RUNTIME_DIR=/var/run when none is set)
  test sink        a temporary null sink can be created and is removed again (always cleaned up)
  capture          parec can record that sink's monitor
  pad discovery    which Bluetooth DualShock 4 pads are connected (none is a SKIP, not a failure)
  hidraw access    the pad's /dev/hidrawN opens for writing (opened only, nothing is written)
  lightbar LED     the kernel LED class of the pad is readable (needed to restore the light after streaming)

Without ``--tone`` nothing is ever sent to a pad. ``--tone`` plays a 3 second 440 Hz tone on the first pad's speaker
through the same library code the daemon uses, then puts the lightbar colour back.
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import struct
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"
BATOCERA_RUNTIME_DIR = "/var/run"
MIN_PYTHON = (3, 9)
CAPTURE_TIMEOUT = 3.0
TEST_SINK_PREFIX = "padselftest_"
PCM_BYTES = 512                      # 128 stereo samples of s16le: what one SBC frame consumes
SBC_HEADER = b"\x9c\x75\x19"


@dataclass
class Result:
    status: str
    name: str
    detail: str = ""
    required: bool = True


def format_result(result: Result) -> str:
    text = "%-5s %s" % (result.status, result.name)
    return "%s: %s" % (text, result.detail) if result.detail else text


# ---------------------------------------------------------------- the individual checks

def check_python(version: Tuple[int, ...]) -> Result:
    text = ".".join(str(part) for part in version[:3])
    if tuple(version[:2]) >= MIN_PYTHON:
        return Result(PASS, "python", text)
    return Result(FAIL, "python", "%s is too old, Python %d.%d or newer is needed" % ((text,) + MIN_PYTHON))


def check_libsbc(sbc: Any) -> Result:
    if sbc.available():
        return Result(PASS, "libsbc loads", "libsbc.so.1")
    return Result(FAIL, "libsbc loads",
                  "libsbc.so.1 (the SBC audio codec) cannot be loaded. Install it: Debian or Ubuntu: apt install libsbc1; "
                  "Fedora: dnf install sbc; Arch: pacman -S sbc. Batocera v41 ships it.")


def check_sbc_frame(sbc: Any) -> Result:
    name = "SBC frame"
    if not sbc.available():
        return Result(SKIP, name, "libsbc is not available")
    try:
        with sbc.SbcEncoder() as encoder:
            frame = bytes(encoder.encode(bytes(PCM_BYTES)))
    except Exception as exc:                      # any encoder problem is a plain failure of this check
        return Result(FAIL, name, "encoding failed: %s" % (exc,))
    if len(frame) != 112 or frame[:3] != SBC_HEADER:
        return Result(FAIL, name, "got %d bytes starting %s, expected 112 bytes starting 9c 75 19"
                      % (len(frame), " ".join("%02x" % b for b in frame[:3])))
    return Result(PASS, name, "112 bytes, header 9c 75 19")


def check_tool(name: str, which: Callable[[str], Optional[str]] = shutil.which) -> Result:
    path = which(name)
    if path:
        return Result(PASS, name, path)
    return Result(FAIL, name, "not found. It is part of the PulseAudio utilities (Debian or Ubuntu: apt install "
                              "pulseaudio-utils; Fedora: dnf install pulseaudio-utils; Arch: pacman -S libpulse)")


def check_bluetooth_tool(name: str, which: Callable[[str], Optional[str]] = shutil.which) -> Result:
    """``hcitool`` and ``bluetoothctl`` are optional: without them two protections are off."""
    path = which(name)
    if path:
        return Result(PASS, name, path, required=False)
    what = ("keeps the pad's Bluetooth link out of sniff mode (without it streaming can drop "
            "after about 14 seconds)" if name == "hcitool"
            else "disconnects a pad cleanly when its HID device vanishes")
    return Result(WARN, name, "not found; it %s. It is part of the BlueZ tools (Debian or Ubuntu: apt install "
                              "bluez); Batocera has it built in" % what, required=False)


def check_audio_server(sinks: Any, runtime_dir: Optional[str]) -> Result:
    name = "audio server"
    if sinks is None:
        return Result(SKIP, name, "pactl is not installed")
    env = "XDG_RUNTIME_DIR=%s" % runtime_dir if runtime_dir else "XDG_RUNTIME_DIR inherited (%s)" % (
        os.environ.get("XDG_RUNTIME_DIR") or "unset")
    try:
        listed = sinks.list_sinks()
        default = sinks.default_sink()
    except Exception as exc:
        return Result(FAIL, name, "pactl cannot reach the audio server with %s: %s. Is PipeWire running "
                                  "(is pipewire-pulse up)? Right after boot it may need a minute." % (env, exc))
    return Result(PASS, name, "%d sink(s), default %s, %s" % (len(listed), default or "none", env))


def check_sink_and_capture(sinks: Any, make_capture: Callable[..., Any], runtime_dir: Optional[str],
                           parec_found: bool = True) -> List[Result]:
    """Create a null sink, record its monitor for a moment, and always remove the sink again."""
    sink_name = "create and remove a test sink"
    cap_name = "capture from the sink monitor"
    if sinks is None:
        return [Result(SKIP, sink_name, "no working audio server"), Result(SKIP, cap_name, "no working audio server")]
    sink = "%s%d" % (TEST_SINK_PREFIX, os.getpid())
    try:
        module = sinks.create_null_sink(sink, "padspeaker self test")
    except Exception as exc:
        return [Result(FAIL, sink_name, "could not create %s: %s" % (sink, exc)),
                Result(SKIP, cap_name, "no test sink")]
    sink_result: Optional[Result] = None
    cap_result: Result
    try:
        if not any(s.name == sink for s in sinks.list_sinks()):
            sink_result = Result(FAIL, sink_name, "%s was created (module %s) but is not listed" % (sink, module))
        if not parec_found:
            cap_result = Result(SKIP, cap_name, "parec is not installed")
        else:
            try:
                with make_capture(sink, runtime_dir) as capture:
                    data = capture.read(PCM_BYTES, CAPTURE_TIMEOUT)
                if len(data) == PCM_BYTES:
                    cap_result = Result(PASS, cap_name, "read %d bytes of PCM from %s.monitor" % (len(data), sink))
                else:
                    cap_result = Result(FAIL, cap_name, "no audio data arrived from %s.monitor within %.0f s"
                                        % (sink, CAPTURE_TIMEOUT))
            except Exception as exc:
                cap_result = Result(FAIL, cap_name, "recording %s.monitor failed: %s" % (sink, exc))
    finally:
        removal_error = None
        try:
            sinks.remove_null_sink(module)
        except Exception as exc:
            removal_error = str(exc)
        try:
            still_there = any(s.name == sink for s in sinks.list_sinks())
        except Exception as exc:
            still_there = False
            removal_error = removal_error or str(exc)
    if removal_error or still_there:
        reason = removal_error or "still listed after removal"
        sink_result = Result(FAIL, sink_name, "%s: %s. Remove it by hand: pactl unload-module %s" % (sink, reason, module))
    elif sink_result is None:
        sink_result = Result(PASS, sink_name, "%s created (module %s), listed, removed" % (sink, module))
    return [sink_result, cap_result]


def check_pads(discover: Callable[[], List[Any]]) -> Tuple[Result, List[Any]]:
    name = "pad discovery"
    try:
        pads = list(discover())
    except Exception as exc:
        return Result(FAIL, name, "scanning for pads failed: %s" % (exc,)), []
    if not pads:
        return (Result(SKIP, name, "no Bluetooth DualShock 4 pad is connected. Connect one over Bluetooth in its normal "
                                   "(PlayStation) mode and run this again", required=False), [])
    listing = "; ".join("%s %s (%s, %s)" % (p.mac, p.name, p.hidraw_path, p.hid_id) for p in pads)
    return Result(PASS, name, "%d pad(s): %s" % (len(pads), listing)), pads


def check_hidraw(pad: Any, open_fn: Callable[[str, int], int] = os.open,
                 close_fn: Callable[[int], None] = os.close) -> Result:
    """Open the pad's hidraw node for writing and close it again. Nothing is written."""
    name = "hidraw write access (%s)" % pad.hidraw_path
    try:
        fd = open_fn(pad.hidraw_path, os.O_WRONLY)
    except OSError as exc:
        return Result(FAIL, name, "cannot open for writing: %s. Run as root (Batocera does), or give your user "
                                  "write access to the device with a udev rule" % (exc,))
    close_fn(fd)
    return Result(PASS, name, "opened write-only and closed again; nothing was written")


def check_lightbar(pad: Any) -> Result:
    from padspeaker import leds
    name = "lightbar LED class"
    state = leds.read_led_state(pad)
    if state is None:
        return Result(SKIP, name, "no readable kernel LED class for this pad: the lightbar may stay dark after "
                                  "streaming because it cannot be restored", required=False)
    return Result(PASS, name, "lightbar colour is %d,%d,%d and can be restored" % (state.r, state.g, state.b))


# ---------------------------------------------------------------- the tone

def _sine_pcm(freq: float, amp: float, start: int, samples: int = 128) -> bytes:
    values = []
    for k in range(samples):
        v = int(32767 * amp * math.sin(2.0 * math.pi * freq * (start + k) / 32000.0))
        values.extend((v, v))
    return struct.pack("<%dh" % len(values), *values)


def play_tone(pad: Any, deps: "Deps", seconds: float = 3.0, volume: int = 64, freq: float = 440.0,
              amp: float = 0.25) -> Result:
    """Send a sine tone to the pad's speaker; the lightbar colour is put back afterwards."""
    from padspeaker import leds, pacer, protocol
    name = "tone on %s" % pad.hidraw_path
    volume = max(0, min(int(volume), protocol.MAX_SPEAKER_VOLUME))
    reports = int(seconds / protocol.REPORT_SECONDS)
    sent = 0
    try:
        with leds.preserved_lightbar(pad) as saved:
            fd = deps.open_fn(pad.hidraw_path, os.O_WRONLY)
            try:
                with deps.sbc.SbcEncoder() as encoder:
                    _write(deps, fd, protocol.build_volume_report(volume))
                    counter = protocol.FrameCounter()
                    clock = pacer.ReportClock(lead=0.03, clock=deps.clock, sleep=deps.sleep)
                    position = 0
                    for _ in range(reports):
                        clock.wait_next()
                        frames = []
                        for _frame in range(protocol.FRAMES_PER_REPORT):
                            frames.append(encoder.encode(_sine_pcm(freq, amp, position)))
                            position += protocol.SAMPLES_PER_FRAME
                        _write(deps, fd, protocol.build_audio_report(counter.next(), frames))
                        sent += 1
                    deps.sleep(0.15)                       # let the pad play what it has buffered
            finally:
                deps.close_fn(fd)
    except Exception as exc:
        return Result(FAIL, name, "stopped after %d of %d reports: %s (the lightbar was restored if it could be read)"
                      % (sent, reports, exc))
    restored = "lightbar restored" if saved is not None else "lightbar could not be read, so it was not restored"
    return Result(PASS, name, "sent %d audio reports (%.1f s, %g Hz, speaker volume %d); %s. Did you hear it?"
                  % (sent, sent * protocol.REPORT_SECONDS, freq, volume, restored))


def _write(deps: "Deps", fd: int, data: bytes) -> None:
    written = deps.write_fn(fd, data)
    if written != len(data):
        raise OSError("short write: %s of %d bytes" % (written, len(data)))


# ---------------------------------------------------------------- wiring

@dataclass
class Deps:
    """Everything the self test touches outside itself; tests replace these with fakes."""

    sbc: Any = None
    which: Callable[[str], Optional[str]] = shutil.which
    make_sinks: Callable[[Optional[str]], Any] = None          # runtime_dir -> SinkManager
    make_capture: Callable[..., Any] = None                    # (sink, runtime_dir) -> context manager with read()
    discover: Callable[[], List[Any]] = None
    open_fn: Callable[[str, int], int] = os.open
    close_fn: Callable[[int], None] = os.close
    write_fn: Callable[[int, bytes], int] = os.write
    clock: Callable[[], float] = field(default=None)
    sleep: Callable[[float], None] = field(default=None)

    def __post_init__(self) -> None:
        import time
        from padspeaker import capture, pads, sbc, sinks
        if self.sbc is None:
            self.sbc = sbc
        if self.make_sinks is None:
            self.make_sinks = lambda runtime_dir: sinks.SinkManager(runtime_dir=runtime_dir)
        if self.make_capture is None:
            self.make_capture = lambda sink, runtime_dir=None: capture.Capture(sink, runtime_dir=runtime_dir)
        if self.discover is None:
            self.discover = pads.discover_pads
        if self.clock is None:
            self.clock = time.monotonic
        if self.sleep is None:
            self.sleep = time.sleep


def run_checks(deps: Deps, runtime_dir: Optional[str]) -> Tuple[List[Result], List[Any]]:
    results: List[Result] = [check_python(sys.version_info)]
    results.append(check_libsbc(deps.sbc))
    results.append(check_sbc_frame(deps.sbc))
    pactl = check_tool("pactl", deps.which)
    parec = check_tool("parec", deps.which)
    results += [pactl, parec]
    results += [check_bluetooth_tool("hcitool", deps.which), check_bluetooth_tool("bluetoothctl", deps.which)]
    sinks = deps.make_sinks(runtime_dir) if pactl.status == PASS else None
    audio = check_audio_server(sinks, runtime_dir)
    results.append(audio)
    results += check_sink_and_capture(sinks if audio.status == PASS else None, deps.make_capture, runtime_dir,
                                      parec_found=parec.status == PASS)
    discovered, pads = check_pads(deps.discover)
    results.append(discovered)
    if pads:
        for pad in pads:
            results.append(check_hidraw(pad, deps.open_fn, deps.close_fn))
        results.append(check_lightbar(pads[0]))
    else:
        results.append(Result(SKIP, "hidraw write access", "no pad", required=False))
        results.append(Result(SKIP, "lightbar LED class", "no pad", required=False))
    return results, pads


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check that this machine can play sound on a DualShock 4 pad's speaker. "
                    "Nothing is sent to a pad unless --tone is given.")
    parser.add_argument("--tone", action="store_true",
                        help="also play a test tone on the first pad's speaker (the lightbar is restored afterwards)")
    parser.add_argument("--seconds", type=float, default=3.0, help="tone length in seconds (default 3)")
    parser.add_argument("--volume", type=int, default=64, help="tone speaker volume 0-79 (default 64)")
    parser.add_argument("--freq", type=float, default=440.0, help="tone frequency in Hz (default 440)")
    parser.add_argument("--runtime-dir", default=None,
                        help="XDG_RUNTIME_DIR for pactl and parec (default: the current one, or %s when none is set, "
                             "as the Batocera service does)" % BATOCERA_RUNTIME_DIR)
    return parser


def main(argv: Optional[List[str]] = None, deps: Optional[Deps] = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_dir = args.runtime_dir
    if runtime_dir is None and not os.environ.get("XDG_RUNTIME_DIR"):
        runtime_dir = BATOCERA_RUNTIME_DIR
    deps = deps or Deps()
    print("padspeaker self test")
    results, pads = run_checks(deps, runtime_dir)
    if args.tone:
        if pads:
            results.append(play_tone(pads[0], deps, seconds=args.seconds, volume=args.volume, freq=args.freq))
        else:
            results.append(Result(FAIL, "tone", "no pad to play it on"))
    for result in results:
        print(format_result(result))
    passed = sum(1 for r in results if r.status == PASS)
    failed = sum(1 for r in results if r.status == FAIL)
    skipped = sum(1 for r in results if r.status == SKIP)
    warned = sum(1 for r in results if r.status == WARN)
    print("%d passed, %d failed, %d skipped, %d warnings" % (passed, failed, skipped, warned))
    required_failed = any(r.status == FAIL and r.required for r in results)
    print("Self test: %s" % ("FAILED, see the FAIL lines above" if required_failed else "OK"))
    return 1 if required_failed else 0


if __name__ == "__main__":
    sys.exit(main())
