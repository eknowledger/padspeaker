"""Command line interface: ``padspeaker <command>`` / ``python3 -m padspeaker <command>``.

Commands
--------
``run``
    Run the daemon: one virtual sink and one streamer per connected pad.
``list``
    Show the connected pads, their sinks and which tools are available.
``tone``
    Play a test tone on a pad, saving and restoring its lightbar.
``status``
    Show what the running daemon is doing.
``check``
    Print a PASS / FAIL checklist of the environment.

Exit codes
----------
==== ==========================================================================
0    success (``status``: the daemon is running)
1    the command ran but failed: a missing tool, a failed check, an I/O error
2    wrong usage or an invalid configuration (the message says what to change)
3    ``status`` only: the daemon is not running
4    busy: ``run`` found another instance running, or ``tone`` was asked to
     use a pad the daemon is streaming to right now
130  interrupted with Ctrl-C
==== ==========================================================================

Everything the commands touch (sysfs, pactl, libsbc, the pad, the clock) comes
from an :class:`Environment`, so the whole module is tested with fakes.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .combos import ComboFlags, PadSink, is_ours, plan_combos, resolve_label
from .daemon import (AlreadyRunning, ConfigError, Daemon, DaemonConfig, default_status_file,
                     load_config_file, normalize_mac, setup_logging)
from .linkpolicy import BluetoothLink
from .leds import preserved_lightbar
from .pacer import ReportClock
from .pads import (OtherDevice, Pad, discover_pads, list_unrecognised_gamepads, mac_tail,
                   normalize_pad_id, recognised_ids)
from .protocol import (FRAMES_PER_REPORT, MAX_SPEAKER_VOLUME, PCM_BYTES_PER_FRAME,
                       REPORT_SECONDS, SAMPLE_RATE, FrameCounter, build_audio_report,
                       build_lightbar_report, build_volume_report)
from .sbc import SbcEncoder, SbcError, available as sbc_available
from .sinks import SinkManager
from .streamer import StreamerConfig, open_hidraw

__all__ = ["Environment", "main", "sine_pcm", "EXIT_OK", "EXIT_FAILURE", "EXIT_USAGE",
           "EXIT_NOT_RUNNING", "EXIT_BUSY"]

PROG = "padspeaker"
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_NOT_RUNNING = 3
EXIT_BUSY = 4
EXIT_INTERRUPTED = 130

MIN_PYTHON = (3, 9)
STATUS_MAX_AGE = 10.0
"""A status file older than this (seconds) does not count as a running daemon."""

TONE_DEFAULT_VOLUME = 48
TONE_AMPLITUDE = 0.25
TONE_TAIL_REPORTS = 3
"""Silent reports sent after the tone so it does not end with a click."""

_INSTALL_HINTS = {
    "libsbc": "install it, for example 'apt install libsbc1' (Debian, Ubuntu), "
              "'dnf install sbc' (Fedora) or 'pacman -S sbc' (Arch)",
    "pactl": "install the PulseAudio command line tools ('apt install pulseaudio-utils' "
             "or 'pipewire-pulse' tools)",
    "parec": "install the PulseAudio command line tools ('apt install pulseaudio-utils' "
             "or 'pipewire-pulse' tools)",
}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass
class Environment:
    """Everything the commands need from the outside world (replaced in tests)."""

    discover: Callable[[], List[Pad]] = discover_pads
    #: discovery with the ``extra_pad_ids`` of the configuration (used only when there are some)
    discover_extra: Callable[[Sequence[str]], List[Pad]] = lambda ids: discover_pads(extra_ids=ids)
    #: Bluetooth gamepads that were not recognised (the hint in ``check``; read-only)
    unrecognised: Callable[[Sequence[str]], List[OtherDevice]] = (
        lambda ids: list_unrecognised_gamepads(extra_ids=ids))
    make_sinks: Callable[[Optional[str]], Any] = lambda runtime_dir: SinkManager(
        runtime_dir=runtime_dir)
    which: Callable[[str], Optional[str]] = shutil.which
    sbc_available: Callable[[], bool] = sbc_available
    encoder_factory: Callable[[], Any] = SbcEncoder
    open_pad: Callable[[str], Any] = open_hidraw
    access: Callable[[str, int], bool] = os.access
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    wall_clock: Callable[[], float] = time.time
    make_daemon: Callable[[DaemonConfig], Any] = Daemon
    pid_alive: Callable[[int], bool] = _pid_alive
    python_version: Callable[[], Tuple[int, ...]] = lambda: tuple(sys.version_info[:3])
    isdir: Callable[[str], bool] = os.path.isdir
    getenv: Callable[[str], Optional[str]] = os.environ.get
    default_status_file: Callable[[Optional[str]], str] = default_status_file
    link: Any = field(default_factory=BluetoothLink)


class CliError(Exception):
    """A failure to report to the user as one line, with an exit code."""

    def __init__(self, message: str, code: int = EXIT_FAILURE) -> None:
        super().__init__(message)
        self.code = code


# --- argument parsing -------------------------------------------------------------

def _number(name: str, low: float, high: float, integer: bool = False) -> Callable[[str], Any]:
    def convert(text: str) -> Any:
        try:
            value = int(text) if integer else float(text)
        except ValueError:
            kind = "a whole number" if integer else "a number"
            raise argparse.ArgumentTypeError("must be %s, got %r" % (kind, text))
        if not low <= value <= high:
            raise argparse.ArgumentTypeError("must be from %g to %g, got %s" % (low, high, text))
        return value
    convert.__name__ = name
    return convert


def _mac(text: str) -> str:
    try:
        return normalize_mac(text)
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def _pad_id(text: str) -> str:
    try:
        return normalize_pad_id(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "%r is not a pad id; use the Bluetooth vendor and product id as VVVV:PPPP in hex, "
            "such as 054C:09CC" % (text,))


def _colour(text: str) -> List[int]:
    parts = text.split(",")
    try:
        values = [int(p) for p in parts]
    except ValueError:
        values = []
    if len(values) != 3 or not all(0 <= v <= 255 for v in values):
        raise argparse.ArgumentTypeError(
            "must be three numbers from 0 to 255 separated by commas, such as 0,0,64; got %r"
            % text)
    return values


def build_parser(out: Any, err: Any) -> argparse.ArgumentParser:
    """Build the argument parser; its output goes to ``out`` and ``err``."""

    class Parser(argparse.ArgumentParser):
        def _print_message(self, message: str, file: Any = None) -> None:
            if message:
                (out if file is sys.stdout else err).write(message)

        def error(self, message: str) -> None:  # type: ignore[override]
            err.write("%s: %s\nRun '%s --help' for the commands and options.\n"
                      % (self.prog, message, PROG))
            self.exit(EXIT_USAGE)

    parser = Parser(prog=PROG, description="Play your computer's sound on the speaker of a "
                    "Bluetooth DualShock 4 style controller.",
                    epilog="Exit codes: 0 ok, 1 failed, 2 usage error, 3 not running "
                           "(status), 4 busy (another instance, or pad in use).")
    parser.add_argument("--version", action="version", version="%s %s" % (PROG, __version__))
    sub = parser.add_subparsers(dest="command", metavar="command", parser_class=Parser)

    def common(p: argparse.ArgumentParser, status: bool = False) -> None:
        p.add_argument("--config", metavar="FILE", help="JSON configuration file")
        p.add_argument("--runtime-dir", metavar="DIR",
                       help="XDG_RUNTIME_DIR of the audio server (needed when started at boot)")
        if status:
            p.add_argument("--status-file", metavar="FILE",
                           help="status file of the daemon (default: in its state directory)")
        p.add_argument("--extra-pad-id", action="append", type=_pad_id, metavar="VVVV:PPPP",
                       help="also treat Bluetooth pads with this vendor:product id (hex) as "
                            "DS4-compatible; repeatable (config key extra_pad_ids)")

    run = sub.add_parser("run", help="run the daemon")
    common(run, status=True)
    run.add_argument("--lock-file", metavar="FILE", help="single instance lock file")
    run.add_argument("--log-file", metavar="FILE", help="also log to this file (rotated)")
    run.add_argument("--poll-interval", type=_number("--poll-interval", 0.1, 60), metavar="SEC",
                     help="seconds between pad scans (default 1)")
    run.add_argument("--jitter-reports", type=_number("--jitter-reports", 1, 8, True), metavar="N",
                     help="reports buffered before sending, 1 to 8 (default 3)")
    run.add_argument("--speaker-volume-max", type=_number("--speaker-volume-max", 0,
                                                          MAX_SPEAKER_VOLUME, True),
                     metavar="N", help="cap for the pad speaker volume, 0 to 79 (default 79)")
    run.add_argument("--sink-name-prefix", metavar="TEXT",
                     help="prefix of the virtual sink names (default padspeaker_)")
    run.add_argument("--only-mac", action="append", type=_mac, metavar="MAC",
                     help="only serve this pad (repeatable); others are left alone")
    run.add_argument("--ignore-mac", action="append", type=_mac, metavar="MAC",
                     help="never touch this pad (repeatable)")
    run.add_argument("--no-keep-lightbar", dest="keep_lightbar", action="store_const",
                     const=False, default=None,
                     help="do not re-send the lightbar colour while streaming")
    run.add_argument("--no-disable-sniff", dest="disable_sniff", action="store_const",
                     const=False, default=None,
                     help="do not take the pads' Bluetooth links out of sniff mode")
    run.add_argument("--no-disconnect-on-hid-loss", dest="disconnect_on_hid_loss",
                     action="store_const", const=False, default=None,
                     help="do not run 'bluetoothctl disconnect' when a pad's HID device vanishes")
    run.add_argument("--no-sync-batocera-audio-device", dest="sync_batocera_audio_device",
                     action="store_const", const=False, default=None,
                     help="do not update Batocera's saved audio output on fall-back")
    run.add_argument("--no-combine-pads", dest="combine_pads", action="store_const",
                     const=False, default=None,
                     help="do not create the 'Controller speakers (all)' output")
    run.add_argument("--no-combine-tv-pad", dest="combine_tv_pad", action="store_const",
                     const=False, default=None,
                     help="do not create the '<main output> + controller speaker' outputs")
    run.add_argument("--no-combine-tv-all", dest="combine_tv_all", action="store_const",
                     const=False, default=None,
                     help="do not create the '<main output> + all controller speakers' output")
    run.add_argument("--tv-sink", metavar="SINK",
                     help="sink the combined outputs play to together with the pads, usually the TV "
                          "(default: the output selected when the first pad appears)")
    run.add_argument("--tv-delay-ms", type=_number("--tv-delay-ms", 0, 2000, True), metavar="MS",
                     help="planned: delay for the main output leg; accepted but not implemented yet")
    run.add_argument("--no-start-on-running", dest="start_on_running", action="store_const",
                     const=False, default=None,
                     help="start streaming only when the pad's output is the default, not "
                          "when sound reaches it through a combined output")
    run.add_argument("--lightbar-fallback", type=_colour, metavar="R,G,B",
                     help="lightbar colour when the pad's own cannot be read (default 0,0,64)")
    run.add_argument("-v", "--verbose", action="store_true", help="log debug detail")
    run.add_argument("-q", "--quiet", action="store_true", help="log warnings and errors only")

    lst = sub.add_parser("list", help="show connected pads, their sinks and available tools")
    common(lst, status=True)
    lst.add_argument("--json", action="store_true", help="machine readable output")
    for command in (run, lst):
        command.add_argument("--tv-label", metavar="TEXT",
                             help="wording for the main output in the combined outputs' names, "
                                  "for example TV (default: the output's own name, from its EDID, with its port; else its description)")

    tone = sub.add_parser("tone", help="play a test tone on a pad")
    common(tone, status=True)
    tone.add_argument("--pad", type=_mac, metavar="MAC", help="the pad's Bluetooth address")
    tone.add_argument("--hidraw", metavar="PATH", help="the pad's /dev/hidrawN node")
    tone.add_argument("--seconds", type=_number("--seconds", 0.1, 60), default=4.0, metavar="N",
                      help="length of the tone, 0.1 to 60 (default 4)")
    tone.add_argument("--volume", type=_number("--volume", 0, MAX_SPEAKER_VOLUME, True),
                      default=TONE_DEFAULT_VOLUME, metavar="V",
                      help="speaker volume, 0 to 79 (default %d)" % TONE_DEFAULT_VOLUME)
    tone.add_argument("--freq", type=_number("--freq", 50, 8000), default=440.0, metavar="HZ",
                      help="tone frequency, 50 to 8000 (default 440)")

    status = sub.add_parser("status", help="show what the daemon is doing "
                            "(exit 0 running, 3 not running)")
    common(status, status=True)
    status.add_argument("--json", action="store_true", help="machine readable output")

    check = sub.add_parser("check", help="check that this machine is ready")
    common(check)
    return parser


# --- configuration ------------------------------------------------------------------

_RUN_FLAGS = {
    "log_file": "log_file", "status_file": "status_file", "lock_file": "lock_file",
    "runtime_dir": "runtime_dir", "poll_interval": "poll_interval",
    "jitter_reports": "jitter_reports", "speaker_volume_max": "speaker_volume_max",
    "sink_name_prefix": "sink_name_prefix", "keep_lightbar": "keep_lightbar",
    "combine_pads": "combine_pads", "combine_tv_pad": "combine_tv_pad",
    "combine_tv_all": "combine_tv_all", "tv_sink": "tv_sink", "tv_label": "tv_label", "tv_delay_ms": "tv_delay_ms",
    "start_on_running": "start_on_running",
    "disable_sniff": "disable_sniff", "disconnect_on_hid_loss": "disconnect_on_hid_loss",
    "sync_batocera_audio_device": "sync_batocera_audio_device",
    "lightbar_fallback": "lightbar_fallback", "only_mac": "only_macs", "ignore_mac": "ignore_macs",
    "extra_pad_id": "extra_pad_ids",
}


def build_config(args: argparse.Namespace) -> DaemonConfig:
    """Merge defaults, the config file (``--config``) and command line flags.

    Raises:
        ConfigError: with a plain-language message.
    """
    data: Dict[str, Any] = {}
    if getattr(args, "config", None):
        data.update(load_config_file(args.config))
    for attr, key in _RUN_FLAGS.items():
        value = getattr(args, attr, None)
        if value is not None:
            data[key] = value
    return DaemonConfig.from_dict(data)


def _status_path(config: DaemonConfig, env: Environment) -> str:
    if config.status_file is None:
        raise CliError("the status file is switched off in the configuration, "
                       "so there is nothing to read", EXIT_FAILURE)
    if isinstance(config.status_file, str):
        return config.status_file
    return env.default_status_file(config.runtime_dir)


def read_status(path: str, env: Environment) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return ``(snapshot, "")`` if the daemon is running, else ``(None, reason)``."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None, "no status file at %s" % path
    except (OSError, ValueError) as exc:
        return None, "the status file %s cannot be read (%s)" % (path, exc)
    if not isinstance(data, dict):
        return None, "the status file %s is not valid" % path
    age = env.wall_clock() - float(data.get("updated_at", 0) or 0)
    if age > STATUS_MAX_AGE:
        return None, "the status file is %.0f s old" % age
    pid = data.get("pid")
    if not isinstance(pid, int) or not env.pid_alive(pid):
        return None, "process %s is gone" % pid
    return data, ""


# --- commands -----------------------------------------------------------------------

def _tools(env: Environment) -> Dict[str, bool]:
    return {"libsbc": bool(env.sbc_available()), "pactl": bool(env.which("pactl")),
            "parec": bool(env.which("parec"))}


def _pads(env: Environment, config: DaemonConfig) -> List[Pad]:
    """The connected pads, including those found through ``extra_pad_ids``."""
    if config.extra_pad_ids:
        return list(env.discover_extra(config.extra_pad_ids))
    return list(env.discover())


def cmd_run(args: argparse.Namespace, env: Environment, out: Any, err: Any) -> int:
    config = build_config(args)
    verbosity = -1 if args.quiet else 1 if args.verbose else 0
    setup_logging(verbosity, config.log_file, stream=err)
    missing = [name for name, ok in _tools(env).items() if not ok]
    if missing:
        err.write("%s: cannot start, %s not found. Run '%s check' for details.\n"
                  % (PROG, ", ".join(missing), PROG))
        for name in missing:
            err.write("  %s: %s\n" % (name, _INSTALL_HINTS[name]))
        return EXIT_FAILURE
    try:
        return int(env.make_daemon(config).run())
    except AlreadyRunning as exc:
        err.write("%s: %s\n" % (PROG, exc))
        return EXIT_BUSY


def cmd_list(args: argparse.Namespace, env: Environment, out: Any, err: Any) -> int:
    config = build_config(args)
    tools = _tools(env)
    pads = _pads(env, config)
    sink_names: Optional[set] = None
    descriptions: Dict[str, str] = {}
    properties: Dict[str, Dict[str, str]] = {}
    default: Optional[str] = None
    if tools["pactl"]:
        try:
            sinks = env.make_sinks(config.runtime_dir)
            listed = list(sinks.list_sinks())
            sink_names = {s.name for s in listed}
            descriptions = {s.name: s.description for s in listed}
            try:
                properties = {n: d.properties for n, d in sinks.sink_details().items()}
            except Exception:
                properties = {}
            default = sinks.default_sink()
        except Exception:
            sink_names = None
    streaming: Dict[str, bool] = {}
    daemon_running = False
    try:
        snapshot, _ = read_status(_status_path(config, env), env)
    except (ConfigError, CliError):
        snapshot = None
    if snapshot is not None:
        daemon_running = True
        for entry in snapshot.get("pads") or []:
            state = (entry.get("streamer") or {}).get("state")
            streaming[entry.get("mac")] = state == "streaming"
    pad_ids = recognised_ids(config.extra_pad_ids)
    entries = []
    pad_sinks = []
    for pad in pads:
        sink = config.sink_name_prefix + mac_tail(pad.mac)
        pad_sinks.append(PadSink(pad.mac, pad.name, sink))
        entries.append({
            "mac": pad.mac, "name": pad.name, "hidraw": pad.hidraw_path, "hid_id": pad.hid_id,
            "sink": sink, "id_source": "config" if pad.extra_id else "built-in",
            "sink_exists": None if sink_names is None else sink in sink_names,
            "sink_is_default": None if sink_names is None else default == sink,
            "streaming": streaming.get(pad.mac, False) if daemon_running else None,
        })
    tv_sink: Optional[str] = None
    if config.tv_sink:
        tv_sink = config.tv_sink
    elif default and sink_names is not None and default in sink_names \
            and not is_ours(default, config.sink_name_prefix):
        tv_sink = default
    combos = []
    for spec in plan_combos(pad_sinks, tv_sink,
                            ComboFlags(config.combine_pads, config.combine_tv_pad,
                                       config.combine_tv_all), config.sink_name_prefix,
                            resolve_label(config.tv_label, tv_sink, descriptions, properties)):
        combos.append({"name": spec.name, "description": spec.description,
                       "slaves": list(spec.slaves), "group": spec.group,
                       "exists": None if sink_names is None else spec.name in sink_names,
                       "is_default": None if sink_names is None else default == spec.name})
    if args.json:
        out.write(json.dumps({"version": __version__, "tools": tools, "daemon_running":
                              daemon_running, "tv_sink": tv_sink, "pads": entries,
                              "pad_ids": [{"id": i, "source": src} for i, src in pad_ids],
                              "combos": combos}, indent=2, sort_keys=True) + "\n")
        return EXIT_OK
    out.write("%s %s\n\nTools\n" % (PROG, __version__))
    for name, ok in tools.items():
        out.write("  %-7s %s\n" % (name, "available" if ok else "MISSING - " + _INSTALL_HINTS[name]))
    out.write("\nRecognised pad ids (Bluetooth only)\n")
    for pad_id, source in pad_ids:
        out.write("  %s  %s\n" % (pad_id, "built in" if source == "built-in"
                                  else "from extra_pad_ids (your configuration)"))
    out.write("\nPads\n")
    if not entries:
        out.write("  No Bluetooth DualShock 4 pads are connected.\n")
    for entry in entries:
        if entry["sink_exists"] is None:
            state = "sink state unknown"
        else:
            state = "sink present" if entry["sink_exists"] else "no sink yet (daemon not running?)"
            if entry["sink_is_default"]:
                state += ", default output"
        if entry["streaming"]:
            state += ", streaming now"
        out.write("  %s  %s  %s%s\n      sink %s: %s\n" % (
            entry["mac"], entry["name"], entry["hidraw"],
            "  [from extra_pad_ids]" if entry["id_source"] == "config" else "",
            entry["sink"], state))
    out.write("\nCombined outputs (main output: %s)\n" % (tv_sink or "none chosen"))
    if not combos:
        out.write("  None (needs a main output and a pad, or two pads).\n")
    for combo in combos:
        state = {None: "unknown", True: "present", False: "not created yet"}[combo["exists"]]
        if combo["is_default"]:
            state += ", default output"
        out.write("  %s  %s\n      plays to %s: %s\n" % (
            combo["name"], combo["description"], ", ".join(combo["slaves"]), state))
    return EXIT_OK


def _format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %dm" % (hours, minutes)
    if minutes:
        return "%dm %ds" % (minutes, secs)
    return "%ds" % secs


def cmd_status(args: argparse.Namespace, env: Environment, out: Any, err: Any) -> int:
    config = build_config(args)
    path = _status_path(config, env)
    data, reason = read_status(path, env)
    if data is None:
        if args.json:
            out.write(json.dumps({"running": False}) + "\n")
        else:
            out.write("%s is not running (%s).\n" % (PROG, reason))
        return EXIT_NOT_RUNNING
    if args.json:
        data = dict(data, running=True)
        out.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        return EXIT_OK
    uptime = _format_duration(env.wall_clock() - float(data.get("started_at", 0) or 0))
    out.write("%s %s is running (pid %s), up %s\n" % (PROG, data.get("version", "?"),
                                                      data.get("pid"), uptime))
    pads = data.get("pads") or []
    if not pads:
        out.write("No pads are connected.\n")
    for pad in pads:
        out.write("\n%s  %s  %s\n" % (pad.get("mac"), pad.get("name"), pad.get("hidraw")))
        if not pad.get("sink_ready"):
            out.write("  sink %s: not ready (%s)\n" % (pad.get("sink"), pad.get("sink_error")))
            continue
        streamer = pad.get("streamer") or {}
        stats = streamer.get("stats") or {}
        silent = " (silent)" if streamer.get("silent") else ""
        via = " via %s" % streamer["via"] if streamer.get("via") else ""
        out.write("  sink %s: %s%s%s\n" % (pad.get("sink"), streamer.get("state", "?"), via, silent))
        out.write("  reports sent %s, underruns %s, overruns %s, write errors %s, restarts %s\n"
                  % tuple(stats.get(k, 0) for k in ("reports_sent", "underruns", "overruns",
                                                    "write_errors", "restarts")))
        if stats.get("last_error"):
            out.write("  last error: %s\n" % stats["last_error"])
    combos = data.get("combos") or []
    if combos:
        out.write("\nCombined outputs (main output: %s)\n" % (data.get("tv_sink") or "none"))
        for combo in combos:
            out.write("  %s: %s\n" % (combo.get("name"), ", ".join(combo.get("slaves") or [])))
    for mac in data.get("ignored") or []:
        out.write("\n%s is ignored by the configuration\n" % mac)
    return EXIT_OK


# --- check --------------------------------------------------------------------------

def _check_items(args: argparse.Namespace, env: Environment) -> List[Tuple[str, str, str]]:
    """Return ``(status, name, detail)`` rows; status is PASS, FAIL, WARN or SKIP."""
    config = build_config(args)
    rows: List[Tuple[str, str, str]] = []
    version = tuple(env.python_version())
    text = ".".join(str(v) for v in version)
    rows.append(("PASS" if version >= MIN_PYTHON else "FAIL", "python",
                 text if version >= MIN_PYTHON else
                 "%s is too old, version %d.%d or newer is needed" % ((text,) + MIN_PYTHON)))
    for name, ok in _tools(env).items():
        rows.append(("PASS" if ok else "FAIL", name,
                     "found" if ok else "not found; " + _INSTALL_HINTS[name]))
    if env.which("pactl"):
        try:
            env.make_sinks(config.runtime_dir).default_sink()
            rows.append(("PASS", "audio server", "answers pactl"))
        except Exception as exc:
            rows.append(("FAIL", "audio server", "pactl cannot reach it: %s" % exc))
    else:
        rows.append(("SKIP", "audio server", "needs pactl"))
    runtime = config.runtime_dir or env.getenv("XDG_RUNTIME_DIR")
    if not runtime:
        rows.append(("WARN", "runtime dir", "XDG_RUNTIME_DIR is not set; pass --runtime-dir "
                     "if pactl cannot connect"))
    elif env.isdir(runtime):
        rows.append(("PASS", "runtime dir", runtime))
    else:
        rows.append(("FAIL", "runtime dir", "%s does not exist" % runtime))
    for tool, why in (("hcitool", "needed to keep the Bluetooth link out of sniff mode "
                                  "(streaming may drop after about 14 s without it)"),
                      ("bluetoothctl", "needed to disconnect a pad cleanly when its HID "
                                       "device vanishes")):
        if env.which(tool):
            rows.append(("PASS", tool, "found"))
        else:
            rows.append(("WARN", tool, "not found; %s (optional, part of the BlueZ tools)" % why))
    try:
        rows.append(("PASS", "state dir", os.path.dirname(
            config.status_file if isinstance(config.status_file, str)
            else env.default_status_file(config.runtime_dir))))
    except ConfigError as exc:
        rows.append(("FAIL", "state dir", str(exc)))
    try:
        others = list(env.unrecognised(config.extra_pad_ids))
    except Exception:
        others = []
    if others:
        listing = "; ".join("%s (%s, id %s)" % (o.name, o.mac or "no address", o.pad_id)
                            for o in others)
        rows.append(("WARN", "other gamepads",
                     "Bluetooth gamepads that were not recognised: %s. If one of them is "
                     "DS4-compatible (it accepts the same speaker reports), add its id to "
                     "extra_pad_ids in the configuration or pass --extra-pad-id VVVV:PPPP. "
                     "Nothing was sent to them." % listing))
    pads = _pads(env, config)
    if not pads:
        rows.append(("WARN", "hidraw access", "no pad connected, so nothing to test"))
        rows.append(("WARN", "pad", "none connected; pair and connect one, then run again"))
        return rows
    for pad in pads:
        ok = env.access(pad.hidraw_path, os.R_OK | os.W_OK)
        rows.append(("PASS" if ok else "FAIL", "hidraw access",
                     "%s is readable and writable" % pad.hidraw_path if ok else
                     "%s is not readable and writable by this user; run as root or "
                     "add a udev rule" % pad.hidraw_path))
    rows.append(("PASS", "pad", ", ".join("%s (%s)" % (p.mac, p.name) for p in pads)))
    return rows


def cmd_check(args: argparse.Namespace, env: Environment, out: Any, err: Any) -> int:
    rows = _check_items(args, env)
    for status, name, detail in rows:
        out.write("[%-4s] %-13s %s\n" % (status, name, detail))
    failed = [name for status, name, _ in rows if status == "FAIL"]
    if failed:
        out.write("\n%d required check(s) failed: %s\n" % (len(failed), ", ".join(failed)))
        return EXIT_FAILURE
    out.write("\nAll required checks passed.\n")
    return EXIT_OK


# --- tone ---------------------------------------------------------------------------

def sine_pcm(frequency: float, start_sample: int, samples: int,
             amplitude: float = TONE_AMPLITUDE) -> bytes:
    """Return ``samples`` stereo frames (s16le, 32 kHz) of a sine wave.

    ``start_sample`` is the index of the first sample, so consecutive calls join
    without a phase jump.  Left and right carry the same signal.
    """
    peak = amplitude * 32767
    step = 2 * math.pi * frequency / SAMPLE_RATE
    mono = [int(peak * math.sin(step * (start_sample + i))) for i in range(samples)]
    stereo = [value for value in mono for _ in (0, 1)]
    return struct.pack("<%dh" % len(stereo), *stereo)


def _select_pad(args: argparse.Namespace, pads: Sequence[Pad]) -> Pad:
    if args.pad:
        for pad in pads:
            if pad.mac == args.pad:
                return pad
        raise CliError("no connected pad has the address %s; '%s list' shows the pads"
                       % (args.pad, PROG))
    if args.hidraw:
        wanted = os.path.realpath(args.hidraw)
        for pad in pads:
            if pad.hidraw_path == args.hidraw or os.path.realpath(pad.hidraw_path) == wanted:
                return pad
        raise CliError("%s is not a connected Bluetooth DualShock 4 pad; '%s list' shows "
                       "the pads" % (args.hidraw, PROG))
    if not pads:
        raise CliError("no Bluetooth DualShock 4 pad is connected")
    if len(pads) > 1:
        raise CliError("several pads are connected, choose one with --pad: %s"
                       % ", ".join("%s (%s)" % (p.mac, p.hidraw_path) for p in pads))
    return pads[0]


def play_tone(pad: Pad, env: Environment, seconds: float, volume: int, frequency: float,
              out: Any, config: Optional[DaemonConfig] = None) -> None:
    """Send a sine tone to ``pad``; the lightbar is saved and restored around it.

    Like the daemon, it re-sends the lightbar colour about twice a second so the
    light stays on (see ``keep_lightbar`` and ``lightbar_fallback``).

    Raises:
        CliError: if libsbc is missing or the pad cannot be opened.
    """
    try:
        encoder = env.encoder_factory()
    except SbcError as exc:
        raise CliError(str(exc))
    lead = StreamerConfig().lead_reports * REPORT_SECONDS
    reports = int(math.ceil(seconds / REPORT_SECONDS))
    samples_per_frame = PCM_BYTES_PER_FRAME // 4
    config = config or DaemonConfig()
    if config.disable_sniff:
        sniff = env.link.disable_sniff(pad.mac)
        if sniff.changed:
            out.write("Turned off sniff mode on the Bluetooth link (it makes streaming drop).\n")
    interval = StreamerConfig().lightbar_interval
    try:
        with preserved_lightbar(pad) as saved:
            colour = (saved.r, saved.g, saved.b) if saved else tuple(config.lightbar_fallback)
            try:
                device = env.open_pad(pad.hidraw_path)
            except OSError as exc:
                raise CliError("cannot open %s for writing: %s. Run as root, or give your "
                               "user write access to the device." % (pad.hidraw_path,
                                                                     exc.strerror or exc))
            try:
                out.write("Playing %g Hz for %g s on %s %s (%s), volume %d\n"
                          % (frequency, seconds, pad.name, pad.mac, pad.hidraw_path, volume))
                device.write(build_volume_report(volume))
                clock = ReportClock(lead=lead, clock=env.clock, sleep=env.sleep)
                counter = FrameCounter()
                position = 0
                next_light = env.clock()
                for index in range(reports + TONE_TAIL_REPORTS):
                    tone = index < reports
                    frames = []
                    for _ in range(FRAMES_PER_REPORT):
                        pcm = (sine_pcm(frequency, position, samples_per_frame) if tone
                               else bytes(PCM_BYTES_PER_FRAME))
                        frames.append(encoder.encode(pcm))
                        position += samples_per_frame
                    clock.wait_next()
                    device.write(build_audio_report(counter.next(), frames))
                    if config.keep_lightbar and env.clock() >= next_light:
                        device.write(build_lightbar_report(*colour))
                        next_light = env.clock() + interval
            except OSError as exc:
                raise CliError("writing to %s failed: %s" % (pad.hidraw_path,
                                                             exc.strerror or exc))
            finally:
                device.close()
    finally:
        encoder.close()
    out.write("Done.\n")


def cmd_tone(args: argparse.Namespace, env: Environment, out: Any, err: Any) -> int:
    if args.pad and args.hidraw:
        raise ConfigError("use --pad or --hidraw, not both")
    config = build_config(args)
    pad = _select_pad(args, _pads(env, config))
    data, _ = read_status(_status_path(config, env), env) if config.status_file is not None \
        else (None, "")
    if data is not None:
        for entry in data.get("pads") or []:
            streamer = entry.get("streamer") or {}
            if entry.get("mac") == pad.mac and streamer.get("state") == "streaming":
                raise CliError(
                    "the daemon (pid %s) is streaming to this pad right now. Choose another "
                    "audio output first, then try again." % data.get("pid"), EXIT_BUSY)
    play_tone(pad, env, args.seconds, args.volume, args.freq, out, config)
    return EXIT_OK


# --- entry point ----------------------------------------------------------------------

_COMMANDS: Dict[str, Callable[[argparse.Namespace, Environment, Any, Any], int]] = {
    "run": cmd_run, "list": cmd_list, "tone": cmd_tone, "status": cmd_status,
    "check": cmd_check,
}


def main(argv: Optional[Sequence[str]] = None, env: Optional[Environment] = None,
         out: Any = None, err: Any = None) -> int:
    """Run the command line and return the process exit code (never raises)."""
    env = env or Environment()
    out = out or sys.stdout
    err = err or sys.stderr
    parser = build_parser(out, err)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (EXIT_OK if exc.code is None
                                                             else EXIT_USAGE)
    if not args.command:
        err.write("%s: choose a command: %s\nRun '%s --help' for details.\n"
                  % (PROG, ", ".join(_COMMANDS), PROG))
        return EXIT_USAGE
    try:
        return _COMMANDS[args.command](args, env, out, err)
    except ConfigError as exc:
        err.write("%s: %s\n" % (PROG, exc))
        return EXIT_USAGE
    except CliError as exc:
        err.write("%s: %s\n" % (PROG, exc))
        return exc.code
    except KeyboardInterrupt:
        err.write("%s: interrupted\n" % PROG)
        return EXIT_INTERRUPTED
    except Exception as exc:
        logging.getLogger("padspeaker.cli").debug("command failed", exc_info=True)
        err.write("%s: unexpected error: %s\n" % (PROG, exc))
        return EXIT_FAILURE
