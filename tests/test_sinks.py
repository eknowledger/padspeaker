"""Tests for padspeaker.sinks with a fake pactl runner."""
import subprocess

import pytest

from padspeaker.sinks import (Sink, SinkError, SinkManager, parse_sinks_raw,
                              quote_sink_properties)

LIST_RAW = (
    'sink="3" name="alsa_output.pci-0000_36_00.1.hdmi-stereo" state="RUNNING" volume="85" '
    'mute="no" description="HD-Audio Generic Digital Stereo (HDMI)"\n'
    'sink="7" description="Controller speaker (Wireless Controller 1122)" mute="yes" '
    'name="padspeaker_1122" volume="40"\n'
    'sink="8" name="odd" description="He said \\"hi\\" name=\\"x\\" volume=\\"1\\"" volume="12"\n'
    '\n'
    'garbage line without tokens\n'
)

MODULES = (
    "1\tmodule-always-sink\t\n"
    "536870925\tmodule-null-sink\tsink_name=padspeaker_1122 "
    "sink_properties=\"device.description='x y'\"\n"
    "536870926\tmodule-null-sink\tsink_name=\"other\"\n"
)


class FakeRunner:
    """Records calls and answers from a table keyed by the pactl sub-command."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, argv, env, timeout):
        self.calls.append((list(argv), dict(env), timeout))
        key = " ".join(argv[1:3]) if argv[1] in ("list", "get-sink-volume") else argv[1]
        reply = self.replies.get(key, "")
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, tuple):
            code, out, err = reply
        else:
            code, out, err = 0, reply, ""
        return subprocess.CompletedProcess(argv, code, out, err)


def manager(replies=None, **kw):
    runner = FakeRunner(replies)
    return SinkManager(runner=runner, runtime_dir="/run/test", **kw), runner


def test_parse_sinks_raw_handles_awkward_lines():
    sinks = parse_sinks_raw(LIST_RAW)
    assert [s.name for s in sinks] == ["alsa_output.pci-0000_36_00.1.hdmi-stereo",
                                       "padspeaker_1122", "odd"]
    assert sinks[0] == Sink("alsa_output.pci-0000_36_00.1.hdmi-stereo",
                            "HD-Audio Generic Digital Stereo (HDMI)", 85, False)
    assert sinks[1] == Sink("padspeaker_1122",
                            "Controller speaker (Wireless Controller 1122)", 40, True)
    assert sinks[2].description == 'He said "hi" name="x" volume="1"'
    assert sinks[2].volume_percent == 12


def test_parse_sinks_raw_missing_volume_is_none():
    (sink,) = parse_sinks_raw('sink="1" name="a" description="b"\n')
    assert sink.volume_percent is None and sink.muted is False


def test_list_sinks_uses_pactl_argv_and_env():
    m, runner = manager({"list sinks-raw": LIST_RAW})
    assert len(m.list_sinks()) == 3
    argv, env, timeout = runner.calls[0]
    assert argv == ["pactl", "list", "sinks-raw"]
    assert env["XDG_RUNTIME_DIR"] == "/run/test"
    assert env["LC_ALL"] == "C"
    assert timeout > 0


def test_pactl_command_is_injectable():
    m, runner = manager({"info": "Default Sink: x\n"}, pactl="/opt/bin/pactl")
    m.default_sink()
    assert runner.calls[0][0][0] == "/opt/bin/pactl"


def test_runtime_dir_none_keeps_environment(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/from/env")
    runner = FakeRunner({"info": "Default Sink: x\n"})
    SinkManager(runner=runner).default_sink()
    assert runner.calls[0][1]["XDG_RUNTIME_DIR"] == "/from/env"


def test_default_sink():
    m, _ = manager({"info": "Server String: x\nDefault Sink: some.sink-name\nDefault Source: y\n"})
    assert m.default_sink() == "some.sink-name"


def test_default_sink_absent():
    m, _ = manager({"info": "Server Name: PipeWire\nDefault Sink: \n"})
    assert m.default_sink() is None


def test_set_default_sink():
    m, runner = manager()
    m.set_default_sink("abc")
    assert runner.calls[0][0] == ["pactl", "set-default-sink", "abc"]


def test_sink_volume_percent_and_mute():
    out = ("Volume: front-left: 55705 /  85% / -4.20 dB,   front-right: 52428 /  80% / -5.81 dB\n"
           "        balance -0.03\n")
    m, runner = manager({"get-sink-volume ds4": out, "get-sink-mute": "Mute: yes\n"})
    assert m.sink_volume("ds4") == 85          # highest channel
    assert m.sink_muted("ds4") is True
    assert runner.calls[1][0] == ["pactl", "get-sink-mute", "ds4"]


def test_sink_volume_above_100():
    m, _ = manager({"get-sink-volume s": "Volume: mono: 90000 / 137% / 8.00 dB\n"})
    assert m.sink_volume("s") == 137


def test_sink_volume_unparseable_raises():
    m, _ = manager({"get-sink-volume s": "nonsense\n"})
    with pytest.raises(SinkError):
        m.sink_volume("s")


def test_sink_muted_no():
    m, _ = manager({"get-sink-mute": "Mute: no\n"})
    assert m.sink_muted("s") is False


def test_quote_sink_properties_plain():
    assert quote_sink_properties("Controller speaker (X 1122)") == \
        "sink_properties=\"device.description='Controller speaker (X 1122)'\""


def test_quote_sink_properties_escapes_quotes_and_backslashes():
    got = quote_sink_properties("it's \"a\" back\\slash")
    assert got == ("sink_properties=\"device.description="
                   "'it\\\\'s \\\"a\\\" back\\\\\\\\slash'\"")


def test_quote_rejects_control_characters():
    with pytest.raises(SinkError):
        quote_sink_properties("bad\nname")


def test_create_null_sink_loads_module_and_returns_id():
    m, runner = manager({"list sinks-raw": LIST_RAW, "load-module": "42\n"})
    assert m.create_null_sink("padspeaker_aaaa", "Controller speaker (Pad aaaa)") == 42
    argv = runner.calls[-1][0]
    assert argv[:3] == ["pactl", "load-module", "module-null-sink"]
    assert "sink_name=padspeaker_aaaa" in argv
    assert quote_sink_properties("Controller speaker (Pad aaaa)") in argv


def test_create_null_sink_is_idempotent():
    m, runner = manager({"list sinks-raw": LIST_RAW, "list modules": MODULES})
    assert m.create_null_sink("padspeaker_1122", "whatever") == 536870925
    assert not any(c[0][1] == "load-module" for c in runner.calls)


def test_create_null_sink_existing_without_module_raises():
    m, _ = manager({"list sinks-raw": LIST_RAW, "list modules": "1\tmodule-always-sink\t\n"})
    with pytest.raises(SinkError):
        m.create_null_sink("padspeaker_1122", "x")


def test_create_null_sink_bad_module_id():
    m, _ = manager({"list sinks-raw": "", "load-module": "oops\n"})
    with pytest.raises(SinkError):
        m.create_null_sink("n", "d")


def test_create_null_sink_rejects_bad_name():
    m, runner = manager()
    for bad in ("", "has space", "a=b", "q'uote", "x\ny"):
        with pytest.raises(SinkError):
            m.create_null_sink(bad, "d")
    assert runner.calls == []


def test_remove_null_sink_by_id():
    m, runner = manager()
    m.remove_null_sink(42)
    assert runner.calls[0][0] == ["pactl", "unload-module", "42"]


def test_remove_null_sink_by_name_looks_up_module():
    m, runner = manager({"list modules": MODULES})
    m.remove_null_sink("padspeaker_1122")
    assert runner.calls[-1][0] == ["pactl", "unload-module", "536870925"]


def test_remove_null_sink_by_quoted_name():
    m, runner = manager({"list modules": MODULES})
    m.remove_null_sink("other")
    assert runner.calls[-1][0] == ["pactl", "unload-module", "536870926"]


def test_remove_null_sink_unknown_name_is_noop():
    m, runner = manager({"list modules": MODULES})
    m.remove_null_sink("nope")
    assert all(c[0][1] != "unload-module" for c in runner.calls)


def test_nonzero_exit_raises_sink_error_with_stderr():
    m, _ = manager({"info": (1, "", "Connection refused")})
    with pytest.raises(SinkError, match="Connection refused"):
        m.default_sink()


def test_timeout_raises_sink_error():
    m, _ = manager({"info": subprocess.TimeoutExpired(["pactl"], 5)})
    with pytest.raises(SinkError, match="timed out"):
        m.default_sink()


def test_missing_binary_raises_sink_error():
    m, _ = manager({"info": FileNotFoundError(2, "No such file")})
    with pytest.raises(SinkError, match="pactl"):
        m.default_sink()


def test_default_runner_passes_timeout_and_env(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "Default Sink: z\n", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    m = SinkManager(runtime_dir="/var/run", timeout=3.5)
    assert m.default_sink() == "z"
    assert seen["timeout"] == 3.5
    assert seen["env"]["XDG_RUNTIME_DIR"] == "/var/run"
    assert seen["argv"] == ["pactl", "info"]
    assert not seen.get("shell")


# --- sink state and combined sinks ------------------------------------------------------

SHORT_SINKS = (
    "55\talsa_output.pci-0000_00_1f.3.analog-stereo\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
    "56\tpadspeaker_1122\tPipeWire\ts16le 2ch 32000Hz\tRUNNING\n"
    "57\tpadspeaker_eeff\tPipeWire\ts16le 2ch 32000Hz\tIDLE\n"
    "garbage\n"
)

COMBINE_MODULES = (
    "10\tmodule-null-sink\tsink_name=padspeaker_1122\n"
    "11\tmodule-combine-sink\tsink_name=padspeaker_all slaves=padspeaker_1122,padspeaker_eeff\n"
    "12\tmodule-combine-sink\tsink_name=\"padspeaker_tv_1122\" slaves=tv,padspeaker_1122\n"
)


def test_sink_states_parses_short_listing():
    m, runner = manager({"list short": SHORT_SINKS})
    assert m.sink_states() == {"alsa_output.pci-0000_00_1f.3.analog-stereo": "SUSPENDED",
                               "padspeaker_1122": "RUNNING", "padspeaker_eeff": "IDLE"}
    assert runner.calls[0][0] == ["pactl", "list", "short", "sinks"]


def test_sink_state_of_one_sink():
    m, _ = manager({"list short": SHORT_SINKS})
    assert m.sink_state("padspeaker_1122") == "RUNNING"
    assert m.sink_state("missing") is None


def test_sink_state_errors_are_sink_errors():
    m, _ = manager({"list short": (1, "", "no server")})
    with pytest.raises(SinkError):
        m.sink_state("x")


def test_create_combine_sink_builds_slaves_argument():
    m, runner = manager({"list sinks-raw": "", "load-module": "77\n"})
    assert m.create_combine_sink("padspeaker_tv_1122", "TV + controller speaker (Pad 1122)",
                                 ["tv", "padspeaker_1122"]) == 77
    argv = runner.calls[-1][0]
    assert argv[:3] == ["pactl", "load-module", "module-combine-sink"]
    assert "sink_name=padspeaker_tv_1122" in argv
    assert "slaves=tv,padspeaker_1122" in argv
    assert quote_sink_properties("TV + controller speaker (Pad 1122)") in argv


def test_create_combine_sink_is_idempotent():
    sinks = 'sink="9" name="padspeaker_all" description="x" volume="100" mute="no"\n'
    m, runner = manager({"list sinks-raw": sinks, "list modules": COMBINE_MODULES})
    assert m.create_combine_sink("padspeaker_all", "d", ["a", "b"]) == 11
    assert not any(c[0][1] == "load-module" for c in runner.calls)


@pytest.mark.parametrize("slaves", [[], ["only one name with space"], ["a,b"], ["ok", "bad=1"], [""]])
def test_create_combine_sink_validates_slaves(slaves):
    m, runner = manager()
    with pytest.raises(SinkError):
        m.create_combine_sink("padspeaker_all", "d", slaves)
    assert runner.calls == []


def test_remove_by_name_finds_combine_modules_too():
    m, runner = manager({"list modules": COMBINE_MODULES})
    m.remove_null_sink("padspeaker_all")
    assert runner.calls[-1][0] == ["pactl", "unload-module", "11"]
    m.remove_null_sink("padspeaker_tv_1122")
    assert runner.calls[-1][0] == ["pactl", "unload-module", "12"]


# --- full listing: descriptions and properties ------------------------------------------

PACTL_LIST_SINKS = '''Sink #55
\tState: SUSPENDED
\tName: alsa_output.pci-0000_00_1f.3.hdmi-stereo-extra1
\tDescription: HD-Audio Generic Digital Stereo (HDMI 2)
\tDriver: PipeWire
\tSample Specification: s32le 2ch 48000Hz
\tMute: no
\tVolume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB
\tProperties:
\t\talsa.card = "0"
\t\talsa.card_name = "HDA Intel PCH"
\t\talsa.name = "Example Display"
\t\tdevice.description = "HD-Audio Generic"
\t\tnode.nick = "Example Display"
\t\tnode.name = "alsa_output.pci-0000_00_1f.3.hdmi-stereo-extra1"
\tPorts:
\t\thdmi-output-1: HDMI / DisplayPort 2 (type: HDMI, priority: 5900, available)
\tActive Port: hdmi-output-1

Sink #56
\tState: SUSPENDED
\tName: alsa_output.pci-0000_00_1f.3.analog-stereo
\tDescription: HD-Audio Generic Analog Stereo
\tProperties:
\t\talsa.name = "ALC892 Analog"
\t\tdevice.description = "HD-Audio Generic"
\t\tnode.nick = "HDMI"
\tPorts:
\t\tanalog-output-speaker: Speakers (type: Speaker)

Sink #57
\tState: IDLE
\tName: alsa_output.usb-Focusrite_Scarlett_2i2-00.analog-stereo
\tDescription: Scarlett 2i2 USB Analog Stereo
\tProperties:
\t\talsa.card_name = "Scarlett 2i2 USB"
\t\tdevice.description = "Scarlett 2i2 USB"
\t\tnode.nick = "USB Audio"
\t\talsa.name = "USB Audio"

Sink #58
\tState: RUNNING
\tName: bare_sink
\tDescription: A sink without properties
\tVolume: front-left: 65536 / 100% / 0.00 dB

Sink #59
\tName: quoted
\tProperties:
\t\tnode.nick = "He said \\"hi\\" \\\\ ok"
\t\tnot a property line
'''


def test_parse_sink_details_reads_names_descriptions_and_properties():
    from padspeaker.sinks import parse_sink_details
    details = parse_sink_details(PACTL_LIST_SINKS)
    assert list(details) == ["alsa_output.pci-0000_00_1f.3.hdmi-stereo-extra1",
                             "alsa_output.pci-0000_00_1f.3.analog-stereo",
                             "alsa_output.usb-Focusrite_Scarlett_2i2-00.analog-stereo",
                             "bare_sink", "quoted"]
    hdmi = details["alsa_output.pci-0000_00_1f.3.hdmi-stereo-extra1"]
    assert hdmi.description == "HD-Audio Generic Digital Stereo (HDMI 2)"
    assert hdmi.properties["node.nick"] == "Example Display"
    assert hdmi.properties["alsa.name"] == "Example Display"
    assert hdmi.properties["device.description"] == "HD-Audio Generic"
    assert "Ports" not in hdmi.properties and "alsa.card" in hdmi.properties
    assert "hdmi-output-1" not in " ".join(hdmi.properties)          # port lines are not properties


def test_parse_sink_details_tolerates_missing_keys_and_odd_lines():
    from padspeaker.sinks import parse_sink_details
    details = parse_sink_details(PACTL_LIST_SINKS)
    bare = details["bare_sink"]
    assert bare.description == "A sink without properties" and bare.properties == {}
    quoted = details["quoted"]
    assert quoted.description == "" and quoted.properties == {"node.nick": 'He said "hi" \\ ok'}
    assert parse_sink_details("") == {}
    assert parse_sink_details("garbage\nSink #1\n\tState: IDLE\n") == {}      # no name: skipped


def test_sink_details_runs_the_full_listing_with_the_pactl_environment():
    m, runner = manager({"list sinks": PACTL_LIST_SINKS})
    details = m.sink_details()
    assert len(details) == 5
    argv, env, timeout = runner.calls[0]
    assert argv == ["pactl", "list", "sinks"]
    assert env["LC_ALL"] == "C" and env["XDG_RUNTIME_DIR"] == "/run/test" and timeout > 0


def test_sink_properties_of_one_sink():
    m, _ = manager({"list sinks": PACTL_LIST_SINKS})
    assert m.sink_properties("alsa_output.pci-0000_00_1f.3.hdmi-stereo-extra1")["node.nick"] == "Example Display"
    assert m.sink_properties("bare_sink") == {}
    assert m.sink_properties("missing") == {}


def test_sink_details_errors_are_sink_errors():
    m, _ = manager({"list sinks": (1, "", "no server")})
    with pytest.raises(SinkError):
        m.sink_details()
