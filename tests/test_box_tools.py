"""Tests for tools/box_selftest.py, the on-box self test (run on the Batocera, or any Linux, by hand)."""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from padspeaker import protocol
from padspeaker.pads import Pad
from padspeaker.sinks import Sink, SinkError

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "box_selftest.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("box_selftest", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["box_selftest"] = mod                      # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def st():
    return load_tool()


GOOD_FRAME = b"\x9c\x75\x19" + bytes(109)


class FakeSbc:
    """Stands in for the padspeaker.sbc module."""

    class SbcUnavailable(Exception):
        pass

    class SbcError(Exception):
        pass

    def __init__(self, available=True, frame=GOOD_FRAME, raises=None):
        self._available = available
        self.frame = frame
        self.raises = raises
        self.encoded = []

    def available(self):
        return self._available

    def SbcEncoder(self):
        outer = self

        class Enc:
            def encode(self, pcm):
                outer.encoded.append(bytes(pcm))
                if outer.raises:
                    raise outer.raises
                return outer.frame

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        return Enc()


class FakeSinks:
    """Stands in for SinkManager; remembers what was created and removed."""

    def __init__(self, sinks=None, default="alsa_output.pci", fail_list=None, fail_create=None, leak=False):
        self.sinks = list(sinks or [Sink("alsa_output.pci", "Built-in", 80, False)])
        self.default = default
        self.fail_list = fail_list
        self.fail_create = fail_create
        self.leak = leak
        self.created = []
        self.removed = []

    def list_sinks(self):
        if self.fail_list:
            raise self.fail_list
        return list(self.sinks)

    def default_sink(self):
        if self.fail_list:
            raise self.fail_list
        return self.default

    def create_null_sink(self, name, description):
        if self.fail_create:
            raise self.fail_create
        self.created.append((name, description))
        self.sinks.append(Sink(name, description, 100, False))
        return 42

    def remove_null_sink(self, module):
        self.removed.append(module)
        if not self.leak:
            self.sinks = [s for s in self.sinks if not s.name.startswith("padselftest_")]


class FakeCapture:
    def __init__(self, data=b"\0" * 512, error=None):
        self.data = data
        self.error = error
        self.started = False
        self.closed = False
        self.sink = None

    def factory(self, sink, runtime_dir=None):
        self.sink = sink
        return self

    def __enter__(self):
        self.started = True
        return self

    def __exit__(self, *a):
        self.closed = True

    def read(self, n, timeout=0.1):
        if self.error:
            raise self.error
        return self.data[:n]


def pad_at(tmp_path, with_leds=True):
    led_dir = None
    if with_leds:
        led_dir = tmp_path / "leds"
        for colour, value in (("red", 10), ("green", 20), ("blue", 30)):
            d = led_dir / f"0005:054C:09CC.0001:{colour}"
            d.mkdir(parents=True)
            (d / "brightness").write_text(f"{value}\n")
        led_dir = str(led_dir)
    return Pad("/dev/hidraw7", "aa:bb:cc:dd:ee:ff", "Wireless Controller", "0005:054C:09CC", led_dir)


def by_name(results):
    return {r.name: r for r in results}


# ---------------------------------------------------------------- the individual checks

def test_python_version_check(st):
    assert st.check_python((3, 11, 8)).status == "PASS"
    assert st.check_python((3, 8, 18)).status == "FAIL"


def test_libsbc_check_passes_and_fails_with_a_fix(st):
    assert st.check_libsbc(FakeSbc(available=True)).status == "PASS"
    r = st.check_libsbc(FakeSbc(available=False))
    assert r.status == "FAIL" and "libsbc" in r.detail and "apt install libsbc1" in r.detail


def test_sbc_frame_check(st):
    sbc = FakeSbc()
    r = st.check_sbc_frame(sbc)
    assert r.status == "PASS" and "112" in r.detail and "9c 75 19" in r.detail
    assert len(sbc.encoded[0]) == 512                       # one frame of silence in
    assert st.check_sbc_frame(FakeSbc(frame=b"\x00" * 112)).status == "FAIL"
    assert st.check_sbc_frame(FakeSbc(frame=GOOD_FRAME[:100])).status == "FAIL"
    assert st.check_sbc_frame(FakeSbc(raises=RuntimeError("boom"))).status == "FAIL"
    assert st.check_sbc_frame(FakeSbc(available=False)).status == "SKIP"


def test_tool_check_uses_which(st):
    assert st.check_tool("pactl", lambda n: "/usr/bin/" + n).status == "PASS"
    r = st.check_tool("parec", lambda n: None)
    assert r.status == "FAIL" and r.name == "parec" and "pulseaudio-utils" in r.detail


def test_audio_server_check(st):
    r = st.check_audio_server(FakeSinks(), "/var/run")
    assert r.status == "PASS" and "1 sink" in r.detail and "alsa_output.pci" in r.detail and "/var/run" in r.detail
    r = st.check_audio_server(FakeSinks(fail_list=SinkError("Connection refused")), "/var/run")
    assert r.status == "FAIL" and "Connection refused" in r.detail and "PipeWire" in r.detail
    assert st.check_audio_server(None, "/var/run").status == "SKIP"


def test_sink_roundtrip_creates_checks_and_always_removes(st):
    sinks, cap = FakeSinks(), FakeCapture()
    results = st.check_sink_and_capture(sinks, cap.factory, "/var/run")
    named = by_name(results)
    assert named["create and remove a test sink"].status == "PASS"
    assert named["capture from the sink monitor"].status == "PASS"
    assert len(sinks.created) == 1 and sinks.created[0][0].startswith("padselftest_")
    assert sinks.removed == [42]
    assert [s.name for s in sinks.sinks] == ["alsa_output.pci"]          # nothing left behind
    assert cap.started and cap.closed and cap.sink == sinks.created[0][0]


def test_sink_roundtrip_removes_the_sink_even_when_capture_blows_up(st):
    sinks = FakeSinks()
    cap = FakeCapture(error=RuntimeError("parec exploded"))
    results = by_name(st.check_sink_and_capture(sinks, cap.factory, "/var/run"))
    assert results["capture from the sink monitor"].status == "FAIL"
    assert "parec exploded" in results["capture from the sink monitor"].detail
    assert sinks.removed == [42] and cap.closed
    assert results["create and remove a test sink"].status == "PASS"


def test_sink_roundtrip_reports_a_sink_that_would_not_go_away(st):
    sinks = FakeSinks(leak=True)
    results = by_name(st.check_sink_and_capture(sinks, FakeCapture().factory, "/var/run"))
    r = results["create and remove a test sink"]
    assert r.status == "FAIL" and "still listed" in r.detail and "pactl unload-module 42" in r.detail


def test_sink_roundtrip_create_failure_skips_capture(st):
    sinks = FakeSinks(fail_create=SinkError("Module initialization failed"))
    results = by_name(st.check_sink_and_capture(sinks, FakeCapture().factory, "/var/run"))
    assert results["create and remove a test sink"].status == "FAIL"
    assert "Module initialization failed" in results["create and remove a test sink"].detail
    assert results["capture from the sink monitor"].status == "SKIP"
    assert sinks.removed == []


def test_sink_roundtrip_capture_timeout_is_a_failure(st):
    results = by_name(st.check_sink_and_capture(FakeSinks(), FakeCapture(data=b"").factory, "/var/run"))
    assert results["capture from the sink monitor"].status == "FAIL"
    assert "no audio" in results["capture from the sink monitor"].detail


def test_sink_roundtrip_is_skipped_without_an_audio_server(st):
    results = st.check_sink_and_capture(None, FakeCapture().factory, "/var/run")
    assert {r.status for r in results} == {"SKIP"} and len(results) == 2


def test_pad_discovery_check(st, tmp_path):
    r, pads = st.check_pads(lambda: [])
    assert r.status == "SKIP" and pads == [] and "Bluetooth" in r.detail and not r.required
    pad = pad_at(tmp_path)
    r, pads = st.check_pads(lambda: [pad])
    assert r.status == "PASS" and "aa:bb:cc:dd:ee:ff" in r.detail and "/dev/hidraw7" in r.detail and pads == [pad]
    r, pads = st.check_pads(lambda: (_ for _ in ()).throw(OSError("sysfs gone")))
    assert r.status == "FAIL" and "sysfs gone" in r.detail


def test_hidraw_check_opens_write_only_and_never_writes(st, tmp_path, monkeypatch):
    opened = []
    closed = []
    monkeypatch.setattr(os, "write", lambda *a, **k: pytest.fail("the self test must not write to the pad"))
    r = st.check_hidraw(pad_at(tmp_path), open_fn=lambda p, flags: (opened.append((p, flags)), 99)[1],
                        close_fn=closed.append)
    assert r.status == "PASS"
    assert opened == [("/dev/hidraw7", os.O_WRONLY)] and closed == [99]


def test_hidraw_check_permission_denied_says_what_to_do(st, tmp_path):
    def deny(p, flags):
        raise PermissionError(13, "Permission denied")
    r = st.check_hidraw(pad_at(tmp_path), open_fn=deny, close_fn=lambda fd: None)
    assert r.status == "FAIL" and "Permission denied" in r.detail and "root" in r.detail


def test_lightbar_check(st, tmp_path):
    r = st.check_lightbar(pad_at(tmp_path))
    assert r.status == "PASS" and "10" in r.detail and "20" in r.detail and "30" in r.detail
    r = st.check_lightbar(pad_at(tmp_path, with_leds=False))
    assert r.status == "SKIP" and not r.required and "lightbar" in r.detail


def test_results_format_as_status_lines(st):
    line = st.format_result(st.Result("PASS", "libsbc loads", "libsbc.so.1"))
    assert re.match(r"^PASS +libsbc loads: libsbc\.so\.1$", line)
    assert re.match(r"^FAIL +x: y$", st.format_result(st.Result("FAIL", "x", "y")))
    assert re.match(r"^SKIP +x: y$", st.format_result(st.Result("SKIP", "x", "y")))


# ---------------------------------------------------------------- the whole run and the exit code

def fake_deps(st, tmp_path, pads=True, sbc=None, sinks=None, which=lambda n: "/usr/bin/" + n, hidraw_ok=True):
    pad = pad_at(tmp_path)
    cap = FakeCapture()

    def opener(p, flags):
        if not hidraw_ok:
            raise PermissionError(13, "Permission denied")
        return 5
    return st.Deps(sbc=sbc or FakeSbc(), which=which, make_sinks=lambda runtime_dir: sinks or FakeSinks(),
                   make_capture=cap.factory, discover=(lambda: [pad]) if pads else (lambda: []),
                   open_fn=opener, close_fn=lambda fd: None, write_fn=lambda fd, data: len(data),
                   clock=lambda: 0.0, sleep=lambda s: None)


def test_bluetooth_tool_check_warns_instead_of_failing(st):
    ok = st.check_bluetooth_tool("hcitool", lambda n: "/usr/bin/" + n)
    assert ok.status == "PASS" and not ok.required
    missing = st.check_bluetooth_tool("bluetoothctl", lambda n: None)
    assert missing.status == "WARN" and not missing.required
    assert "disconnects" in missing.detail and "BlueZ" in missing.detail
    assert re.match(r"^WARN +bluetoothctl: ", st.format_result(missing))


def test_main_lists_the_bluetooth_tools_and_a_missing_one_is_only_a_warning(st, tmp_path, capsys):
    first = tmp_path / "first"
    first.mkdir()
    rc, out = run_main(st, [], fake_deps(st, first), capsys)
    assert rc == 0 and re.search(r"^PASS +hcitool", out, re.M)
    assert re.search(r"^PASS +bluetoothctl", out, re.M)
    deps = fake_deps(st, tmp_path, which=lambda n: None if n in ("hcitool", "bluetoothctl")
                     else "/usr/bin/" + n)
    rc, out = run_main(st, [], deps, capsys)
    assert rc == 0, out
    assert re.search(r"^WARN +hcitool", out, re.M) and re.search(r"^WARN +bluetoothctl", out, re.M)
    assert re.search(r"\d+ passed, 0 failed, \d+ skipped, 2 warnings", out)


def run_main(st, argv, deps, capsys):
    rc = st.main(argv, deps=deps)
    return rc, capsys.readouterr().out


def test_main_all_good_exits_zero(st, tmp_path, capsys):
    rc, out = run_main(st, [], fake_deps(st, tmp_path), capsys)
    assert rc == 0, out
    lines = [l for l in out.splitlines() if re.match(r"^(PASS|FAIL|SKIP) ", l)]
    assert lines and not [l for l in lines if l.startswith("FAIL")]
    for needed in ("python", "libsbc", "SBC frame", "pactl", "parec", "audio server", "test sink", "pad", "hidraw", "lightbar"):
        assert any(needed in l for l in lines), needed
    assert re.search(r"\d+ passed, 0 failed, \d+ skipped", out)


def test_main_without_a_pad_skips_pad_checks_and_still_passes(st, tmp_path, capsys):
    rc, out = run_main(st, [], fake_deps(st, tmp_path, pads=False), capsys)
    assert rc == 0, out
    assert re.search(r"^SKIP +.*hidraw", out, re.M) and re.search(r"^SKIP +.*pad", out, re.M)


@pytest.mark.parametrize("broken", ["libsbc", "pactl", "sink", "hidraw"])
def test_main_exits_one_on_a_required_failure(st, tmp_path, capsys, broken):
    kwargs = {}
    if broken == "libsbc":
        kwargs["sbc"] = FakeSbc(available=False)
    elif broken == "pactl":
        kwargs["which"] = lambda n: None if n == "pactl" else "/usr/bin/" + n
    elif broken == "sink":
        kwargs["sinks"] = FakeSinks(fail_create=SinkError("nope"))
    elif broken == "hidraw":
        kwargs["hidraw_ok"] = False
    rc, out = run_main(st, [], fake_deps(st, tmp_path, **kwargs), capsys)
    assert rc == 1, out
    assert re.search(r"^FAIL ", out, re.M) and re.search(r"[1-9]\d* failed", out)


def test_main_runtime_dir_defaults(st, tmp_path, capsys, monkeypatch):
    seen = []
    deps = fake_deps(st, tmp_path)
    deps.make_sinks = lambda runtime_dir: (seen.append(runtime_dir), FakeSinks())[1]
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    run_main(st, [], deps, capsys)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    run_main(st, [], deps, capsys)
    run_main(st, ["--runtime-dir", "/tmp/x"], deps, capsys)
    assert seen == ["/var/run", None, "/tmp/x"]            # Batocera sessions have none set: use what the service uses


def test_main_never_touches_pads_without_tone(st, tmp_path, capsys):
    deps = fake_deps(st, tmp_path)
    writes = []
    deps.write_fn = lambda fd, data: writes.append(data) or len(data)
    run_main(st, [], deps, capsys)
    assert writes == []


# ---------------------------------------------------------------- the tone

def tone_deps(st, tmp_path):
    deps = fake_deps(st, tmp_path)
    deps.writes = []
    deps.write_fn = lambda fd, data: deps.writes.append(bytes(data)) or len(data)
    ticks = {"t": 0.0}

    def sleep(s):
        ticks["t"] += s
    deps.clock = lambda: ticks["t"]
    deps.sleep = sleep
    return deps


def test_tone_plays_three_seconds_and_restores_the_lightbar(st, tmp_path, capsys):
    deps = tone_deps(st, tmp_path)
    pad = deps.discover()[0]
    # the lightbar goes dark while streaming; the restore must put the saved colours back
    def dark_on_audio_write(fd, data):
        deps.writes.append(bytes(data))
        if data[0] == protocol.AUDIO_REPORT_ID:
            for colour in ("red", "green", "blue"):
                (Path(pad.led_dir) / f"0005:054C:09CC.0001:{colour}" / "brightness").write_text("0\n")
        return len(data)
    deps.write_fn = dark_on_audio_write
    rc, out = run_main(st, ["--tone"], deps, capsys)
    assert rc == 0, out
    ids = [w[0] for w in deps.writes]
    assert set(ids) <= {protocol.AUDIO_REPORT_ID, protocol.STATE_REPORT_ID}      # only the two documented report types
    assert ids[0] == protocol.STATE_REPORT_ID and len(deps.writes[0]) == protocol.STATE_REPORT_LEN
    audio = [w for w in deps.writes if w[0] == protocol.AUDIO_REPORT_ID]
    assert 180 <= len(audio) <= 190                                               # 3 s / 16 ms = 187
    assert all(len(a) == protocol.AUDIO_REPORT_LEN for a in audio)
    parsed = [protocol.parse_audio_report(a) for a in audio]
    assert all(p.crc_ok and p.target == protocol.TARGET_SPEAKER for p in parsed)
    assert [p.counter for p in parsed][:3] == [0, 4, 8]
    assert all(f[:3] == b"\x9c\x75\x19" for p in parsed for f in p.frames)
    # lightbar restored from the values read before streaming
    values = [(Path(pad.led_dir) / f"0005:054C:09CC.0001:{c}" / "brightness").read_text().strip() for c in ("red", "green", "blue")]
    assert values == ["10", "20", "30"]
    assert re.search(r"^PASS +tone", out, re.M)


def test_tone_encodes_a_sine_not_silence(st, tmp_path, capsys):
    deps = tone_deps(st, tmp_path)
    sbc = deps.sbc
    run_main(st, ["--tone"], deps, capsys)
    assert len(sbc.encoded) >= 4 * 180
    assert all(len(p) == 512 for p in sbc.encoded)
    assert any(any(p) for p in sbc.encoded)                                       # real samples went in


def test_tone_without_a_pad_fails(st, tmp_path, capsys):
    deps = tone_deps(st, tmp_path)
    deps.discover = lambda: []
    rc, out = run_main(st, ["--tone"], deps, capsys)
    assert rc == 1 and re.search(r"^FAIL +tone", out, re.M)


def test_tone_write_error_fails_but_still_restores_the_lightbar(st, tmp_path, capsys):
    deps = tone_deps(st, tmp_path)
    pad = deps.discover()[0]
    calls = {"n": 0}

    def flaky(fd, data):
        calls["n"] += 1
        if calls["n"] == 5:
            for c in ("red", "green", "blue"):
                (Path(pad.led_dir) / f"0005:054C:09CC.0001:{c}" / "brightness").write_text("0\n")
            raise OSError(107, "Transport endpoint is not connected")
        return len(data)
    deps.write_fn = flaky
    rc, out = run_main(st, ["--tone"], deps, capsys)
    assert rc == 1 and "Transport endpoint is not connected" in out
    assert (Path(pad.led_dir) / "0005:054C:09CC.0001:red" / "brightness").read_text().strip() == "10"


def test_tone_options(st, tmp_path, capsys):
    deps = tone_deps(st, tmp_path)
    rc, out = run_main(st, ["--tone", "--seconds", "1", "--volume", "200"], deps, capsys)
    assert rc == 0
    audio = [w for w in deps.writes if w[0] == protocol.AUDIO_REPORT_ID]
    assert 58 <= len(audio) <= 66
    volume_report = deps.writes[0]
    assert volume_report[3 + 21] == protocol.MAX_SPEAKER_VOLUME                   # capped, never above the pad's maximum


# ---------------------------------------------------------------- as a program

def test_script_runs_standalone_without_a_traceback():
    """Real run on whatever machine the tests run on: libsbc/pactl may be missing, so only the shape is checked."""
    r = subprocess.run([sys.executable, str(TOOL)], capture_output=True, text=True, timeout=60,
                       cwd="/", env={**os.environ, "PYTHONPATH": ""})
    assert "Traceback" not in r.stderr + r.stdout, r.stderr
    assert r.returncode in (0, 1)
    assert re.search(r"^(PASS|FAIL|SKIP) ", r.stdout, re.M)
    assert re.search(r"\d+ passed, \d+ failed, \d+ skipped", r.stdout)


def test_script_help_and_bad_option():
    r = subprocess.run([sys.executable, str(TOOL), "--help"], capture_output=True, text=True, timeout=30, cwd="/")
    assert r.returncode == 0 and "--tone" in r.stdout
    r = subprocess.run([sys.executable, str(TOOL), "--bogus"], capture_output=True, text=True, timeout=30, cwd="/")
    assert r.returncode == 2
