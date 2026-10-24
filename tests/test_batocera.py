"""Tests for padspeaker.batocera with a fake settings tool (no Batocera needed)."""
import logging
import subprocess

from padspeaker.batocera import restore_saved_audio_device


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeTool:
    def __init__(self, saved="padspeaker_1122", present=True):
        self.saved = saved
        self.present = present
        self.calls = []
        self.set_rc = 0
        self.raises = None

    def which(self, name):
        return "/usr/bin/" + name if self.present else None

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if self.raises:
            raise self.raises
        if argv[0] == "batocera-settings-get":
            assert argv[1:] == ["audio.device"]
            return Result(0 if self.saved is not None else 1, (self.saved or "") + "\n")
        assert argv[0] == "batocera-settings-set"
        if self.set_rc == 0:
            self.saved = argv[2]
        return Result(self.set_rc, "", "" if self.set_rc == 0 else "boom")

    def sets(self):
        return [c for c, _ in self.calls if c[0] == "batocera-settings-set"]


def call(tool, fallback="alsa_output.hdmi", **kw):
    return restore_saved_audio_device(fallback, runner=tool, which=tool.which, **kw)


def test_replaces_a_saved_value_that_is_one_of_our_sinks():
    tool = FakeTool("padspeaker_1122")
    assert call(tool) is True
    assert tool.sets() == [["batocera-settings-set", "audio.device", "alsa_output.hdmi"]]
    assert tool.saved == "alsa_output.hdmi"
    assert all(t > 0 for _, t in tool.calls)


def test_leaves_other_saved_values_alone():
    for saved in ("alsa_output.analog", "", None, "my_padspeaker_1122"):
        tool = FakeTool(saved)
        assert call(tool) is False
        assert tool.sets() == []


def test_custom_prefix():
    tool = FakeTool("pad_1122")
    assert call(tool, prefix="pad_") is True
    tool = FakeTool("padspeaker_1122")
    assert call(tool, prefix="pad_") is False


def test_absent_tool_is_silent(caplog):
    tool = FakeTool(present=False)
    with caplog.at_level(logging.DEBUG, logger="padspeaker"):
        assert call(tool) is False
    assert tool.calls == []
    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []


def test_never_falls_back_to_one_of_our_own_sinks():
    tool = FakeTool("padspeaker_1122")
    assert call(tool, fallback="padspeaker_eeff") is False
    assert call(tool, fallback="") is False
    assert call(tool, fallback="-rf") is False
    assert tool.sets() == []


def test_failures_never_raise():
    tool = FakeTool("padspeaker_1122")
    tool.set_rc = 1
    assert call(tool) is False
    for exc in (subprocess.TimeoutExpired("x", 1), PermissionError(13, "no"),
                FileNotFoundError(2, "gone"), RuntimeError("odd")):
        tool = FakeTool("padspeaker_1122")
        tool.raises = exc
        assert call(tool) is False


def test_logs_at_info_when_it_changes_something(caplog):
    tool = FakeTool("padspeaker_1122")
    with caplog.at_level(logging.INFO, logger="padspeaker"):
        call(tool)
    assert any("audio.device" in r.getMessage() for r in caplog.records)


def test_several_prefixes_cover_the_combined_outputs():
    prefixes = ("padspeaker_", "padspeaker_all", "padspeaker_tv_")
    for saved in ("padspeaker_1122", "padspeaker_all", "padspeaker_tv_1122", "padspeaker_tv_all"):
        tool = FakeTool(saved)
        assert call(tool, prefix=prefixes) is True, saved
    tool = FakeTool("alsa_output.analog")
    assert call(tool, prefix=prefixes) is False
    # a fallback that is itself one of ours is refused, whichever prefix it has
    tool = FakeTool("padspeaker_tv_all")
    assert call(tool, fallback="padspeaker_all", prefix=prefixes) is False
    assert tool.sets() == []


def test_a_saved_choice_from_the_pre_rename_version_is_reset():
    """Batocera may still hold audio.device=ds4_speaker_xxxx from before the rename."""
    from padspeaker.combos import our_prefixes
    for saved in ("ds4_speaker_1122", "ds4_speakers_all", "ds4_tv_1122", "ds4_tv_all"):
        tool = FakeTool(saved)
        assert call(tool, prefix=our_prefixes()) is True, saved
        assert tool.saved == "alsa_output.hdmi"
