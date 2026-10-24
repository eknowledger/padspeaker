"""Tests for padspeaker.cli: argument handling, exit codes and each command.

Everything the commands touch (pad discovery, pactl, libsbc, the pad device, the
clock, the daemon) is injected through ``cli.Environment`` and replaced by fakes.
"""
import io
import json
import os

import pytest

from padspeaker import __version__, cli
from padspeaker.daemon import AlreadyRunning, DaemonConfig
from padspeaker.pads import Pad
from padspeaker.protocol import (MAX_SPEAKER_VOLUME, is_audio_report, is_state_report,
                                 parse_audio_report)
from padspeaker.sbc import SbcUnavailable
from padspeaker.sinks import Sink, SinkDetails, SinkError

MAC1 = "aa:bb:cc:dd:11:22"
MAC2 = "aa:bb:cc:dd:ee:ff"


def make_pad(mac=MAC1, node=7, led_dir=None):
    return Pad("/dev/hidraw%d" % node, mac, "Wireless Controller", "0005:054C:09CC", led_dir)


# --- fakes -------------------------------------------------------------------

class FakeSinks:
    def __init__(self):
        self.sink_list = [Sink("alsa_output.hdmi", "HDMI", 80, False)]
        self.default = "alsa_output.hdmi"
        self.props = {}
        self.fail_details = False
        self.fail = False

    def list_sinks(self):
        if self.fail:
            raise SinkError("pactl: connection refused")
        return list(self.sink_list)

    def default_sink(self):
        if self.fail:
            raise SinkError("pactl: connection refused")
        return self.default

    def sink_details(self):
        if self.fail or self.fail_details:
            raise SinkError("pactl: connection refused")
        return {s.name: SinkDetails(s.name, s.description, dict(self.props.get(s.name, {})))
                for s in self.sink_list}


class FakeDevice:
    def __init__(self, clock):
        self.clock = clock
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append((self.clock.now, bytes(data)))
        return len(data)

    def close(self):
        self.closed = True


class FakeEncoder:
    def __init__(self):
        self.count = 0

    def encode(self, pcm):
        assert len(pcm) == 512
        self.count += 1
        return b"\x9c\x75\x19" + bytes(109)

    def close(self):
        pass


class FakeTime:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


class FakeLink:
    def __init__(self):
        self.sniff = []
        self.changed = False

    def disable_sniff(self, mac):
        from padspeaker.linkpolicy import SniffResult
        self.sniff.append(mac)
        return SniffResult(True, self.changed, "link policy RSWITCH")


class FakeDaemon:
    def __init__(self, config, error=None):
        self.config = config
        self.error = error
        self.ran = False

    def run(self):
        self.ran = True
        if self.error:
            raise self.error
        return 0


class Box:
    """A cli.Environment plus the fakes behind it."""

    def __init__(self, tmp_path, pads=None):
        self.time = FakeTime()
        self.pads = [make_pad()] if pads is None else pads
        self.sinks = FakeSinks()
        self.tools = {"pactl": "/usr/bin/pactl", "parec": "/usr/bin/parec"}
        self.sbc = True
        self.devices = []
        self.open_error = None
        self.denied = set()
        self.alive = True
        self.python = (3, 11, 2)
        self.runtime_dir_ok = True
        self.daemons = []
        self.link = FakeLink()
        self.daemon_error = None
        self.status_path = str(tmp_path / "status.json")
        self.tmp = tmp_path
        self.env = cli.Environment(
            discover=lambda: list(self.pads),
            make_sinks=lambda runtime_dir: self.sinks,
            which=lambda name: self.tools.get(name),
            sbc_available=lambda: self.sbc,
            encoder_factory=self._encoder,
            open_pad=self._open,
            access=lambda path, mode: path not in self.denied,
            clock=self.time,
            sleep=self.time.sleep,
            wall_clock=lambda: 1700000100.0,
            make_daemon=self._daemon,
            link=self.link,
            pid_alive=lambda pid: self.alive,
            python_version=lambda: self.python,
            isdir=lambda path: self.runtime_dir_ok,
            getenv=lambda name: "/run/user/1000" if name == "XDG_RUNTIME_DIR" else None,
            default_status_file=lambda runtime_dir=None: self.status_path,
        )

    def _encoder(self):
        if not self.sbc:
            raise SbcUnavailable("libsbc could not be loaded (test)")
        return FakeEncoder()

    def _open(self, path):
        if self.open_error is not None:
            raise self.open_error
        dev = FakeDevice(self.time)
        self.devices.append(dev)
        return dev

    def _daemon(self, config):
        d = FakeDaemon(config, self.daemon_error)
        self.daemons.append(d)
        return d

    def write_status(self, **overrides):
        data = {"version": __version__, "pid": 4242, "state": "running",
                "started_at": 1700000000.0, "updated_at": 1700000099.0,
                "pads": [{"mac": MAC1, "name": "Wireless Controller", "hidraw": "/dev/hidraw7",
                          "sink": "padspeaker_1122", "sink_ready": True, "sink_error": None,
                          "streamer": {"state": "idle", "stats": {"reports_sent": 12,
                                                                  "underruns": 1}}}],
                "ignored": []}
        data.update(overrides)
        with open(self.status_path, "w") as handle:
            json.dump(data, handle)

    def run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), env=self.env, out=out, err=err)
        return code, out.getvalue(), err.getvalue()


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


# --- general ----------------------------------------------------------------------

def test_version(box):
    code, out, _ = box.run("--version")
    assert code == 0 and out.strip() == "padspeaker " + __version__


def test_version_string_lives_in_the_package():
    assert __version__ == "0.1.0"


def test_no_command_is_a_usage_error(box):
    code, out, err = box.run()
    assert code == 2
    assert "choose a command" in err and "run" in err and "tone" in err


def test_unknown_command_and_option_are_plain_errors(box):
    code, _, err = box.run("dance")
    assert code == 2 and "padspeaker:" in err and "dance" in err
    assert "Traceback" not in err
    code, _, err = box.run("list", "--bogus")
    assert code == 2 and "--bogus" in err


@pytest.mark.parametrize("args, fragment", [
    (("tone", "--seconds", "abc"), "--seconds"),
    (("tone", "--seconds", "0"), "--seconds"),
    (("tone", "--seconds", "500"), "--seconds"),
    (("tone", "--volume", "200"), "--volume"),
    (("tone", "--volume", "x"), "--volume"),
    (("tone", "--freq", "0"), "--freq"),
    (("tone", "--freq", "20000"), "--freq"),
    (("tone", "--pad", "nonsense"), "MAC address"),
    (("tone", "--pad", MAC1, "--hidraw", "/dev/hidraw7"), "not both"),
    (("run", "--only-mac", "zz"), "MAC address"),
    (("run", "--poll-interval", "0"), "--poll-interval"),
    (("run", "--jitter-reports", "99"), "--jitter-reports"),
    (("run", "--speaker-volume-max", "100"), "--speaker-volume-max"),
    (("run", "--sink-name-prefix", "no spaces"), "sink_name_prefix"),
    (("run", "--config", "/nonexistent/x.json"), "cannot read"),
])
def test_bad_arguments_get_plain_language_errors(box, args, fragment):
    code, _, err = box.run(*args)
    assert code == 2
    assert fragment in err
    assert "Traceback" not in err and "invalid" not in err.lower().replace("not a valid", "")


# --- list ---------------------------------------------------------------------------

def test_list_human_readable(box):
    box.sinks.sink_list.append(Sink("padspeaker_1122", "Controller speaker", 100, False))
    box.sinks.default = "padspeaker_1122"
    code, out, _ = box.run("list")
    assert code == 0
    assert MAC1 in out and "/dev/hidraw7" in out and "Wireless Controller" in out
    assert "padspeaker_1122" in out and "default" in out
    assert "libsbc" in out and "pactl" in out and "parec" in out


def test_list_without_pads_or_tools(box):
    box.pads = []
    box.sbc = False
    box.tools = {}
    code, out, _ = box.run("list")
    assert code == 0
    assert "no" in out.lower() and "pad" in out.lower()
    assert "missing" in out.lower() or "not found" in out.lower()


def test_list_json(box):
    box.sinks.sink_list.append(Sink("padspeaker_1122", "Controller speaker", 100, False))
    code, out, _ = box.run("list", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["version"] == __version__
    assert data["tools"] == {"libsbc": True, "pactl": True, "parec": True}
    pad = data["pads"][0]
    assert pad["mac"] == MAC1 and pad["hidraw"] == "/dev/hidraw7"
    assert pad["sink"] == "padspeaker_1122"
    assert pad["sink_exists"] is True and pad["sink_is_default"] is False


def two_pads(box):
    box.pads = [make_pad(MAC1, 7), make_pad(MAC2, 8)]


def test_list_shows_the_combined_outputs_it_would_create(box):
    two_pads(box)
    box.sinks.sink_list.append(Sink("padspeaker_tv_all", "TV + all controller speakers", 100, False))
    code, out, _ = box.run("list", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["tv_sink"] == "alsa_output.hdmi"
    combos = {c["name"]: c for c in data["combos"]}
    assert set(combos) == {"padspeaker_all", "padspeaker_tv_1122", "padspeaker_tv_eeff", "padspeaker_tv_all"}
    assert combos["padspeaker_tv_all"]["exists"] is True and combos["padspeaker_tv_1122"]["exists"] is False
    assert combos["padspeaker_tv_all"]["slaves"] == ["alsa_output.hdmi", "padspeaker_1122",
                                              "padspeaker_eeff"]
    assert combos["padspeaker_all"]["description"] == "Controller speakers (all)"
    # the descriptions use the main output's own description (FakeSinks calls it "HDMI")
    assert combos["padspeaker_tv_all"]["description"] == "HDMI + all controller speakers"
    assert combos["padspeaker_tv_1122"]["description"] == "HDMI + controller speaker (Wireless Controller 1122)"
    _, text, _ = box.run("list")
    assert "Combined outputs" in text and "padspeaker_tv_all" in text and "HDMI + all controller speakers" in text
    assert "alsa_output.hdmi" in text


def test_list_combos_follow_the_configuration(box):
    two_pads(box)
    path = box.tmp / "c.json"
    path.write_text(json.dumps({"combine_pads": False, "combine_tv_all": False,
                                "tv_sink": "alsa_output.hdmi"}))
    _, out, _ = box.run("list", "--json", "--config", str(path))
    assert {c["name"] for c in json.loads(out)["combos"]} == {"padspeaker_tv_1122", "padspeaker_tv_eeff"}


def test_list_labels_combos_with_the_tvs_own_name(box):
    two_pads(box)
    box.sinks.sink_list[0] = Sink("alsa_output.hdmi", "HD-Audio Generic Digital Stereo (HDMI 2)",
                                  80, False)
    box.sinks.props["alsa_output.hdmi"] = {"node.nick": "Example Display", "alsa.name": "Example Display",
                                           "device.description": "HD-Audio Generic"}
    _, out, _ = box.run("list", "--json")
    combos = {c["name"]: c for c in json.loads(out)["combos"]}
    assert combos["padspeaker_tv_all"]["description"] == "Example Display (HDMI 2) + all controller speakers"
    assert combos["padspeaker_tv_1122"]["description"] == \
        "Example Display (HDMI 2) + controller speaker (Wireless Controller 1122)"
    _, out, _ = box.run("list", "--json", "--tv-label", "TV")
    assert {c["name"]: c for c in json.loads(out)["combos"]}["padspeaker_tv_all"]["description"] == \
        "TV + all controller speakers"
    box.sinks.fail_details = True                      # properties unreadable: the description
    _, out, _ = box.run("list", "--json")
    assert {c["name"]: c for c in json.loads(out)["combos"]}["padspeaker_tv_all"]["description"] == \
        "HD-Audio Generic Digital Stereo (HDMI 2) + all controller speakers"


def test_list_uses_tv_label_from_flag_or_config(box):
    two_pads(box)
    path = box.tmp / "c.json"
    path.write_text(json.dumps({"tv_label": 'Sound "bar"'}))
    _, out, _ = box.run("list", "--json", "--config", str(path))
    combos = {c["name"]: c for c in json.loads(out)["combos"]}
    assert combos["padspeaker_tv_all"]["description"] == "Sound bar + all controller speakers"
    _, out, _ = box.run("list", "--json", "--tv-label", "TV")
    combos = {c["name"]: c for c in json.loads(out)["combos"]}
    assert combos["padspeaker_tv_eeff"]["description"] == "TV + controller speaker (Wireless Controller eeff)"
    assert combos["padspeaker_tv_eeff"]["name"] == "padspeaker_tv_eeff"


def test_run_accepts_tv_label(box):
    assert box.run("run", "--tv-label", "TV")[0] == 0
    assert box.daemons[0].config.tv_label == "TV"
    box.run("run")
    assert box.daemons[1].config.tv_label is None


def test_list_has_no_combos_without_a_tv_and_a_single_pad(box):
    box.sinks.default = "padspeaker_1122"                 # no usable TV sink
    _, out, _ = box.run("list", "--json")
    data = json.loads(out)
    assert data["combos"] == [] and data["tv_sink"] is None


def test_list_shows_which_pads_are_streaming(box):
    two_pads(box)
    box.write_status()
    data = json.load(open(box.status_path))
    data["pads"][0]["streamer"]["state"] = "streaming"
    data["pads"][0]["streamer"]["via"] = "running"
    json.dump(data, open(box.status_path, "w"))
    _, out, _ = box.run("list", "--json", "--status-file", box.status_path)
    pads = {p["mac"]: p for p in json.loads(out)["pads"]}
    assert pads[MAC1]["streaming"] is True and pads[MAC2]["streaming"] is False
    _, text, _ = box.run("list", "--status-file", box.status_path)
    assert "streaming" in text
    box.alive = False
    _, out, _ = box.run("list", "--json", "--status-file", box.status_path)
    assert all(p["streaming"] is None for p in json.loads(out)["pads"])      # daemon not running


def test_status_shows_combos_and_streaming(box):
    box.write_status(tv_sink="alsa_output.hdmi", combos=[
        {"name": "padspeaker_tv_1122", "description": "TV + controller speaker (Wireless Controller 1122)",
         "slaves": ["alsa_output.hdmi", "padspeaker_1122"], "group": "tv_pad"}])
    code, out, _ = box.run("status", "--status-file", box.status_path)
    assert code == 0 and "padspeaker_tv_1122" in out and "alsa_output.hdmi" in out


def test_list_survives_pactl_failure(box):
    box.sinks.fail = True
    code, out, _ = box.run("list", "--json")
    assert code == 0
    pad = json.loads(out)["pads"][0]
    assert pad["sink_exists"] is None


def test_list_uses_the_configured_sink_prefix(box):
    path = box.tmp / "c.json"
    path.write_text(json.dumps({"sink_name_prefix": "pad_"}))
    _, out, _ = box.run("list", "--json", "--config", str(path))
    assert json.loads(out)["pads"][0]["sink"] == "pad_1122"


# --- status -------------------------------------------------------------------------

def test_status_running(box):
    box.write_status()
    code, out, _ = box.run("status", "--status-file", box.status_path)
    assert code == 0
    assert "running" in out and "4242" in out and MAC1 in out and "padspeaker_1122" in out
    assert "idle" in out and "12" in out


def test_status_json_prints_the_snapshot(box):
    box.write_status()
    code, out, _ = box.run("status", "--json", "--status-file", box.status_path)
    assert code == 0 and json.loads(out)["pid"] == 4242


def test_status_not_running_when_no_file(box):
    code, out, err = box.run("status", "--status-file", box.status_path)
    assert code == 3
    assert "not running" in (out + err)


def test_status_not_running_when_stale_or_dead(box):
    box.write_status(updated_at=1700000000.0)         # 100 s old
    code, out, err = box.run("status", "--status-file", box.status_path)
    assert code == 3 and "not running" in (out + err)
    box.write_status()
    box.alive = False
    code, out, err = box.run("status", "--status-file", box.status_path)
    assert code == 3 and "not running" in (out + err)


def test_status_with_corrupt_file(box):
    with open(box.status_path, "w") as handle:
        handle.write("{half")
    code, out, err = box.run("status", "--status-file", box.status_path)
    assert code == 3


def test_status_json_when_not_running_still_signals_with_exit_code(box):
    code, out, _ = box.run("status", "--json", "--status-file", box.status_path)
    assert code == 3
    assert json.loads(out) == {"running": False}


def test_status_default_path_comes_from_the_environment(box):
    box.write_status()
    code, _, _ = box.run("status")
    assert code == 0


# --- check --------------------------------------------------------------------------

def test_check_all_pass(box):
    code, out, _ = box.run("check")
    assert code == 0
    for item in ("python", "libsbc", "pactl", "parec", "hidraw", "runtime", "pad"):
        assert item in out.lower()
    assert "FAIL" not in out and out.count("PASS") >= 7


def test_check_reports_missing_requirements(box):
    box.sbc = False
    box.tools = {}
    code, out, _ = box.run("check")
    assert code == 1
    lines = [l for l in out.splitlines() if "FAIL" in l]
    joined = "\n".join(lines).lower()
    assert "libsbc" in joined and "pactl" in joined and "parec" in joined
    assert "install" in out.lower()         # tells the user what to do


def test_check_fails_when_hidraw_is_not_writable(box):
    box.denied.add("/dev/hidraw7")
    code, out, _ = box.run("check")
    assert code == 1
    assert any("FAIL" in l and "hidraw" in l.lower() for l in out.splitlines())


def test_check_without_pad_warns_but_passes(box):
    box.pads = []
    code, out, _ = box.run("check")
    assert code == 0
    assert any("WARN" in l and "pad" in l.lower() for l in out.splitlines())


def test_check_old_python_fails(box):
    box.python = (3, 7, 3)
    code, out, _ = box.run("check")
    assert code == 1
    assert any("FAIL" in l and "python" in l.lower() for l in out.splitlines())


def test_check_missing_runtime_dir_fails(box):
    box.runtime_dir_ok = False
    code, out, _ = box.run("check")
    assert code == 1
    assert any("FAIL" in l and "runtime" in l.lower() for l in out.splitlines())


def test_check_audio_server_unreachable_fails(box):
    box.sinks.fail = True
    code, out, _ = box.run("check")
    assert code == 1
    assert any("FAIL" in l and "audio server" in l.lower() for l in out.splitlines())


def test_check_warns_when_bluetooth_tools_are_missing(box):
    box.tools = {"pactl": "/usr/bin/pactl", "parec": "/usr/bin/parec"}   # no hcitool, no bluetoothctl
    code, out, _ = box.run("check")
    assert code == 0                                       # a warning, not a failure
    warns = [l for l in out.splitlines() if "WARN" in l]
    assert any("hcitool" in l for l in warns) and any("bluetoothctl" in l for l in warns)
    assert not any("FAIL" in l for l in out.splitlines())


def test_check_passes_the_bluetooth_tools_when_present(box):
    box.tools.update({"hcitool": "/usr/bin/hcitool", "bluetoothctl": "/usr/bin/bluetoothctl"})
    _, out, _ = box.run("check")
    assert any("PASS" in l and "hcitool" in l for l in out.splitlines())
    assert any("PASS" in l and "bluetoothctl" in l for l in out.splitlines())


# --- tone ---------------------------------------------------------------------------

def make_led_dir(tmp_path, values=(10, 20, 30)):
    leds = tmp_path / "leds"
    for colour, value in zip(("red", "green", "blue"), values):
        d = leds / ("input20:" + colour)
        d.mkdir(parents=True)
        (d / "brightness").write_text("%d\n" % value)
    return str(leds)


def led_values(led_dir):
    return [int(open(os.path.join(led_dir, "input20:" + c, "brightness")).read())
            for c in ("red", "green", "blue")]


def test_tone_plays_valid_paced_reports_and_restores_the_lightbar(box):
    led_dir = make_led_dir(box.tmp)
    box.pads = [make_pad(led_dir=led_dir)]
    code, out, _ = box.run("tone", "--seconds", "0.5", "--volume", "40")
    assert code == 0
    dev = box.devices[0]
    assert dev.closed
    states = [d for _, d in dev.writes if is_state_report(d)]
    audio = [(t, d) for t, d in dev.writes if is_audio_report(d)]
    assert states and states[0][3 + 21] == 40
    assert len(audio) >= 31
    counters = [parse_audio_report(d).counter for _, d in audio]
    assert counters[0] == 0 and all(b - a == 4 for a, b in zip(counters, counters[1:]))
    assert all(parse_audio_report(d).crc_ok for _, d in audio)
    assert dev.writes[0][1][0] == 0x11               # volume goes first
    times = [t for t, _ in audio]
    assert times[-1] - times[0] == pytest.approx((len(audio) - 1) * 0.016 - 0.032, abs=0.02)
    assert led_values(led_dir) == [10, 20, 30]
    assert "440" in out


def lights_of(device):
    return [(t, d) for t, d in device.writes if is_state_report(d) and d[3] == 0x02]


def test_tone_keeps_the_lightbar_on_with_the_saved_colour(box):
    box.pads = [make_pad(led_dir=make_led_dir(box.tmp, (10, 20, 30)))]
    assert box.run("tone", "--seconds", "1.6")[0] == 0
    lights = lights_of(box.devices[0])
    assert 3 <= len(lights) <= 5                              # about every 0.5 s
    assert all(tuple(d[8:11]) == (10, 20, 30) for _, d in lights)
    gaps = [b[0] - a[0] for a, b in zip(lights, lights[1:])]
    assert min(gaps) >= 0.49


def test_tone_uses_the_fallback_colour_when_the_light_is_unreadable(box):
    assert box.run("tone", "--seconds", "1")[0] == 0         # pad without LED class
    lights = lights_of(box.devices[0])
    assert lights and all(tuple(d[8:11]) == (0, 0, 64) for _, d in lights)


def test_tone_honours_the_configuration(box):
    path = box.tmp / "c.json"
    path.write_text(json.dumps({"keep_lightbar": False}))
    assert box.run("tone", "--seconds", "1", "--config", str(path))[0] == 0
    assert lights_of(box.devices[0]) == []
    path.write_text(json.dumps({"lightbar_fallback": [5, 6, 7]}))
    assert box.run("tone", "--seconds", "1", "--config", str(path))[0] == 0
    assert tuple(lights_of(box.devices[1])[0][1][8:11]) == (5, 6, 7)


def test_tone_restores_the_lightbar_when_it_fails(box):
    led_dir = make_led_dir(box.tmp)
    box.pads = [make_pad(led_dir=led_dir)]
    for colour in ("red", "green", "blue"):          # something changed it during playback
        pass
    original_open = box.env.open_pad

    def failing(path):
        for c, v in zip(("red", "green", "blue"), (0, 0, 0)):
            with open(os.path.join(led_dir, "input20:" + c, "brightness"), "w") as handle:
                handle.write("%d\n" % v)
        return original_open(path)
    box.env.open_pad = failing
    box.open_error = OSError(13, "Permission denied")
    code, out, err = box.run("tone", "--seconds", "0.5")
    assert code == 1
    assert "Permission denied" in err or "permission" in err.lower()
    assert led_values(led_dir) == [10, 20, 30]


def test_tone_selects_pad_by_mac_and_hidraw(box):
    box.pads = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    assert box.run("tone", "--seconds", "0.2")[0] == 1          # ambiguous
    code, out, err = box.run("tone", "--seconds", "0.2")
    assert "--pad" in err and MAC1 in err and MAC2 in err
    assert box.run("tone", "--seconds", "0.2", "--pad", MAC2.upper())[0] == 0
    assert box.run("tone", "--seconds", "0.2", "--hidraw", "/dev/hidraw7")[0] == 0
    assert len(box.devices) == 2


def test_tone_refuses_devices_that_are_not_known_pads(box):
    code, _, err = box.run("tone", "--hidraw", "/dev/hidraw99")
    assert code == 1 and "not a connected" in err and box.devices == []
    code, _, err = box.run("tone", "--pad", MAC2)
    assert code == 1 and MAC2 in err and box.devices == []


def test_tone_without_any_pad(box):
    box.pads = []
    code, _, err = box.run("tone")
    assert code == 1 and "no" in err.lower() and "pad" in err.lower()


def test_tone_refuses_while_the_daemon_streams_that_pad(box):
    box.write_status()
    data = json.load(open(box.status_path))
    data["pads"][0]["streamer"]["state"] = "streaming"
    json.dump(data, open(box.status_path, "w"))
    code, _, err = box.run("tone", "--status-file", box.status_path)
    assert code == 4
    assert "streaming" in err and "daemon" in err
    assert box.devices == []


def test_tone_allowed_when_daemon_is_idle_or_not_running(box):
    box.write_status()                                     # daemon up, pad idle
    assert box.run("tone", "--seconds", "0.2", "--status-file", box.status_path)[0] == 0
    box.alive = False                                      # stale status, daemon gone
    data = json.load(open(box.status_path))
    data["pads"][0]["streamer"]["state"] = "streaming"
    json.dump(data, open(box.status_path, "w"))
    assert box.run("tone", "--seconds", "0.2", "--status-file", box.status_path)[0] == 0


def test_tone_without_libsbc(box):
    box.sbc = False
    code, _, err = box.run("tone")
    assert code == 1 and "libsbc" in err and box.devices == []


def test_tone_default_volume_is_within_the_cap(box):
    box.run("tone", "--seconds", "0.2")
    state = [d for _, d in box.devices[0].writes if is_state_report(d)][0]
    assert 0 < state[3 + 21] <= MAX_SPEAKER_VOLUME


def test_tone_pcm_is_a_sine_at_the_requested_frequency():
    pcm = cli.sine_pcm(frequency=1000.0, start_sample=0, samples=64, amplitude=0.5)
    assert len(pcm) == 64 * 4
    import struct
    values = struct.unpack("<128h", pcm)
    left, right = values[0::2], values[1::2]
    assert left == right and left[0] == 0
    assert max(left) == pytest.approx(0.5 * 32767, rel=0.01)
    # 1 kHz at 32 kHz: peak after 8 samples
    assert left[8] == max(left)
    # continues seamlessly from a given start sample
    tail = struct.unpack("<8h", cli.sine_pcm(1000.0, 8, 4, 0.5))
    assert tail[0::2] == left[8:12]


# --- run -----------------------------------------------------------------------------

def test_run_builds_the_daemon_from_flags_and_runs_it(box):
    code, _, _ = box.run("run", "--poll-interval", "2", "--only-mac", MAC1,
                         "--ignore-mac", MAC2.upper(), "--speaker-volume-max", "40",
                         "--jitter-reports", "4", "--sink-name-prefix", "pad_",
                         "--status-file", "/tmp/s.json", "--lock-file", "/tmp/l.lock",
                         "--runtime-dir", "/run/x")
    assert code == 0
    config = box.daemons[0].config
    assert box.daemons[0].ran
    assert config.poll_interval == 2.0 and config.only_macs == [MAC1]
    assert config.ignore_macs == [MAC2] and config.speaker_volume_max == 40
    assert config.jitter_reports == 4 and config.sink_name_prefix == "pad_"
    assert config.status_file == "/tmp/s.json" and config.lock_file == "/tmp/l.lock"
    assert config.runtime_dir == "/run/x"


def test_run_lightbar_flags(box):
    code, _, _ = box.run("run", "--no-keep-lightbar", "--lightbar-fallback", "10,20,30")
    assert code == 0
    config = box.daemons[0].config
    assert config.keep_lightbar is False and config.lightbar_fallback == [10, 20, 30]
    box.run("run")
    assert box.daemons[1].config.keep_lightbar is True
    assert box.daemons[1].config.lightbar_fallback == [0, 0, 64]


@pytest.mark.parametrize("value", ["1,2", "a,b,c", "1,2,300", "red"])
def test_run_rejects_a_bad_lightbar_fallback(box, value):
    code, _, err = box.run("run", "--lightbar-fallback", value)
    assert code == 2 and "--lightbar-fallback" in err and "Traceback" not in err


def test_run_link_flags(box):
    box.run("run")
    c = box.daemons[0].config
    assert c.disable_sniff is True and c.disconnect_on_hid_loss is True
    assert c.sync_batocera_audio_device is None
    box.run("run", "--no-disable-sniff", "--no-disconnect-on-hid-loss",
            "--no-sync-batocera-audio-device")
    c = box.daemons[1].config
    assert c.disable_sniff is False and c.disconnect_on_hid_loss is False
    assert c.sync_batocera_audio_device is False


def test_tone_turns_sniff_mode_off_first(box):
    box.link.changed = True
    code, out, _ = box.run("tone", "--seconds", "0.2")
    assert code == 0 and box.link.sniff == [MAC1]
    assert "sniff" in out.lower()


def test_tone_respects_disable_sniff_setting(box):
    path = box.tmp / "c.json"
    path.write_text(json.dumps({"disable_sniff": False}))
    assert box.run("tone", "--seconds", "0.2", "--config", str(path))[0] == 0
    assert box.link.sniff == []


def test_run_combo_flags(box):
    box.run("run")
    c = box.daemons[0].config
    assert (c.combine_pads, c.combine_tv_pad, c.combine_tv_all) == (True, True, True)
    assert c.tv_sink is None and c.tv_delay_ms == 0 and c.start_on_running is True
    box.run("run", "--no-combine-pads", "--no-combine-tv-pad", "--no-combine-tv-all",
            "--tv-sink", "alsa_output.hdmi", "--tv-delay-ms", "120", "--no-start-on-running")
    c = box.daemons[1].config
    assert (c.combine_pads, c.combine_tv_pad, c.combine_tv_all) == (False, False, False)
    assert c.tv_sink == "alsa_output.hdmi" and c.tv_delay_ms == 120
    assert c.start_on_running is False


def test_run_rejects_bad_combo_arguments(box):
    code, _, err = box.run("run", "--tv-delay-ms", "-5")
    assert code == 2 and "--tv-delay-ms" in err
    code, _, err = box.run("run", "--tv-sink", "has space")
    assert code == 2 and "tv_sink" in err


def test_run_flags_override_the_config_file(box):
    path = box.tmp / "c.json"
    path.write_text(json.dumps({"poll_interval": 5, "jitter_reports": 2,
                                "ignore_macs": [MAC2], "log_file": "/tmp/from-file.log"}))
    code, _, _ = box.run("run", "--config", str(path), "--poll-interval", "3")
    assert code == 0
    config = box.daemons[0].config
    assert config.poll_interval == 3.0            # flag wins
    assert config.jitter_reports == 2             # file value kept
    assert config.ignore_macs == [MAC2]
    assert config.log_file == "/tmp/from-file.log"


def test_run_refuses_when_another_instance_is_running(box):
    box.daemon_error = AlreadyRunning("padspeaker is already running (pid 99)")
    code, _, err = box.run("run")
    assert code == 4 and "already running" in err


def test_run_reports_missing_prerequisites(box):
    box.tools = {"pactl": "/usr/bin/pactl"}
    code, _, err = box.run("run")
    assert code == 1 and "parec" in err and "check" in err
    assert box.daemons == []
    box.tools = {"pactl": "/x", "parec": "/y"}
    box.sbc = False
    code, _, err = box.run("run")
    assert code == 1 and "libsbc" in err


def test_run_unexpected_error_is_a_clean_failure(box):
    box.daemon_error = RuntimeError("boom")
    code, _, err = box.run("run")
    assert code == 1 and "boom" in err and "Traceback" not in err


def test_run_verbosity_flags(box):
    import logging
    box.run("run", "-v")
    assert logging.getLogger("padspeaker").level == logging.DEBUG
    box.run("run", "-q")
    assert logging.getLogger("padspeaker").level == logging.WARNING
    box.run("run")
    assert logging.getLogger("padspeaker").level == logging.INFO


# --- module entry point -----------------------------------------------------------------

def test_python_dash_m_works():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-m", "padspeaker", "--version"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, cwd=os.path.dirname(os.path.dirname(
                                os.path.abspath(__file__))))
    assert result.returncode == 0
    assert result.stdout.strip() == "padspeaker " + __version__


def test_keyboard_interrupt_is_a_clean_exit(box):
    box.daemon_error = KeyboardInterrupt()
    code, _, err = box.run("run")
    assert code == 130


# --- extra pad ids ------------------------------------------------------------------

from padspeaker.pads import OtherDevice  # noqa: E402

CLONE_ID = "1234:ABCD"


def clone_pad(mac=MAC2, node=9):
    return Pad("/dev/hidraw%d" % node, mac, "Clone Pad", "0005:" + CLONE_ID, None, True)


def with_extra_discovery(box, pads=None):
    """Discovery that only finds the clone when its id was passed in."""
    calls = []

    def discover_extra(ids):
        calls.append(list(ids))
        found = list(box.pads)
        if CLONE_ID in ids:
            found.append(pads[0] if pads else clone_pad())
        return found
    box.env.discover_extra = discover_extra
    return calls


def test_run_accepts_extra_pad_id_flags_and_the_config_key(box, tmp_path):
    code, _, err = box.run("run", "--extra-pad-id", "1234:abcd", "--extra-pad-id", "2dc8:3106")
    assert code == 0, err
    assert box.daemons[-1].config.extra_pad_ids == ["1234:ABCD", "2DC8:3106"]
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"extra_pad_ids": ["1111:2222"]}))
    box.run("run", "--config", str(cfg))
    assert box.daemons[-1].config.extra_pad_ids == ["1111:2222"]
    box.run("run", "--config", str(cfg), "--extra-pad-id", "3333:4444")        # flags override the file
    assert box.daemons[-1].config.extra_pad_ids == ["3333:4444"]


@pytest.mark.parametrize("value", ["nonsense", "1234", "1234:zzzz", "0005:1234:abcd", "12345:abcd"])
def test_a_bad_extra_pad_id_is_a_plain_usage_error(box, value):
    for command in ("run", "list", "check", "tone"):
        code, _, err = box.run(command, "--extra-pad-id", value)
        assert code == 2 and "VVVV:PPPP" in err
        assert "Traceback" not in err


def test_list_shows_every_recognised_id_and_where_it_came_from(box):
    calls = with_extra_discovery(box)
    code, out, _ = box.run("list", "--extra-pad-id", "1234:abcd")
    assert code == 0
    section = out.split("Recognised pad ids")[1].split("\n\n")[0]
    for builtin in ("054C:05C4", "054C:09CC", "054C:0BA0"):
        line = [l for l in section.splitlines() if builtin in l][0]
        assert "built in" in line
    clone_line = [l for l in section.splitlines() if CLONE_ID in l][0]
    assert "extra_pad_ids" in clone_line and "built in" not in clone_line
    assert calls == [[CLONE_ID]]
    assert "Clone Pad" in out and MAC2 in out
    assert "from extra_pad_ids" in [l for l in out.splitlines() if "Clone Pad" in l][0]


def test_list_without_extra_ids_uses_plain_discovery(box):
    calls = with_extra_discovery(box)
    code, out, _ = box.run("list")
    assert code == 0 and calls == []
    assert "Recognised pad ids" in out and "extra_pad_ids" not in out.split("Recognised pad ids")[1].split("\n\n")[0]


def test_list_json_has_the_ids_and_each_pads_source(box):
    with_extra_discovery(box)
    code, out, _ = box.run("list", "--json", "--extra-pad-id", CLONE_ID)
    data = json.loads(out)
    assert data["pad_ids"] == [{"id": "054C:05C4", "source": "built-in"}, {"id": "054C:09CC", "source": "built-in"},
                               {"id": "054C:0BA0", "source": "built-in"}, {"id": CLONE_ID, "source": "config"}]
    sources = {p["mac"]: p["id_source"] for p in data["pads"]}
    assert sources == {MAC1: "built-in", MAC2: "config"}


def test_tone_can_target_a_pad_that_only_the_extra_id_finds(box):
    with_extra_discovery(box)
    code, _, err = box.run("tone", "--pad", MAC2, "--seconds", "0.2")
    assert code == 1 and "no connected pad has the address" in err          # without the id: not found
    code, _, err = box.run("tone", "--pad", MAC2, "--seconds", "0.2", "--extra-pad-id", CLONE_ID)
    assert code == 0, err
    assert box.devices


def other(name="Other Pad", pad_id="2DC8:3106", mac="aa:bb:cc:dd:ee:07", node=3):
    return OtherDevice("/dev/hidraw%d" % node, mac, name, "0005:" + pad_id, pad_id)


def test_check_hints_at_unrecognised_bluetooth_gamepads_without_touching_them(box):
    asked = []
    box.env.unrecognised = lambda ids: asked.append(list(ids)) or [other()]
    code, out, _ = box.run("check")
    assert code == 0                                                    # a hint, never a failure
    line = [l for l in out.splitlines() if "other gamepads" in l][0]
    assert line.startswith("[WARN") and "Other Pad" in line and "2DC8:3106" in line and "aa:bb:cc:dd:ee:07" in line
    assert "extra_pad_ids" in out and "--extra-pad-id" in out
    assert "nothing was sent" in out.lower()
    assert box.devices == [] and asked == [[]]                          # no device was opened


def test_check_hint_also_shows_when_no_pad_is_connected(box):
    box.pads = []
    box.env.unrecognised = lambda ids: [other(), other("Third Pad", "1111:2222", "", 4)]
    code, out, _ = box.run("check")
    assert code == 0
    assert "Other Pad" in out and "Third Pad" in out and "1111:2222" in out
    assert any(l.startswith("[WARN") and "pad " in l for l in out.splitlines())      # the no-pad warning is still there


def test_check_has_no_hint_row_when_everything_is_recognised(box):
    box.env.unrecognised = lambda ids: []
    _, out, _ = box.run("check")
    assert "other gamepads" not in out


def test_check_passes_the_extra_ids_on(box):
    asked = []
    box.env.unrecognised = lambda ids: asked.append(list(ids)) or []
    with_extra_discovery(box)
    box.run("check", "--extra-pad-id", CLONE_ID)
    assert asked == [[CLONE_ID]]


def test_check_survives_a_failing_scan(box):
    def boom(ids):
        raise OSError("sysfs gone")
    box.env.unrecognised = boom
    code, out, _ = box.run("check")
    assert code == 0 and "Traceback" not in out
