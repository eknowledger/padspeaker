"""Tests for padspeaker.leds using a fake LED class directory."""
import os

import pytest

from padspeaker.leds import LedState, preserved_lightbar, read_led_state, restore_led_state
from padspeaker.pads import Pad


def make_led_dir(tmp_path, values=(10, 20, 30), prefix="input20"):
    led_dir = tmp_path / "leds"
    led_dir.mkdir()
    for colour, value in zip(("red", "green", "blue"), values):
        d = led_dir / ("%s:%s" % (prefix, colour))
        d.mkdir()
        (d / "brightness").write_text("%d\n" % value)
        (d / "max_brightness").write_text("255\n")
    g = led_dir / ("%s:global" % prefix)
    g.mkdir()
    (g / "brightness").write_text("255\n")
    return led_dir


def make_pad(led_dir):
    return Pad("/dev/hidraw3", "aa:bb:cc:dd:11:22", "Wireless Controller",
               "0005:054C:09CC", str(led_dir) if led_dir is not None else None)


def read_brightness(led_dir, colour, prefix="input20"):
    return int((led_dir / ("%s:%s" % (prefix, colour)) / "brightness").read_text())


def test_read_state(tmp_path):
    pad = make_pad(make_led_dir(tmp_path))
    assert read_led_state(pad) == LedState(10, 20, 30)


def test_read_state_none_without_led_dir():
    assert read_led_state(make_pad(None)) is None


def test_read_state_none_when_a_colour_is_missing(tmp_path):
    led_dir = make_led_dir(tmp_path)
    os.remove(str(led_dir / "input20:green" / "brightness"))
    assert read_led_state(make_pad(led_dir)) is None


def test_read_state_none_on_garbage(tmp_path):
    led_dir = make_led_dir(tmp_path)
    (led_dir / "input20:red" / "brightness").write_text("banana\n")
    assert read_led_state(make_pad(led_dir)) is None


def test_read_state_none_when_directory_vanished(tmp_path):
    assert read_led_state(make_pad(tmp_path / "gone")) is None


def test_restore_writes_values(tmp_path):
    led_dir = make_led_dir(tmp_path, values=(0, 0, 0))
    assert restore_led_state(make_pad(led_dir), LedState(1, 2, 255)) is True
    assert [read_brightness(led_dir, c) for c in ("red", "green", "blue")] == [1, 2, 255]


def test_restore_clamps_values(tmp_path):
    led_dir = make_led_dir(tmp_path, values=(0, 0, 0))
    assert restore_led_state(make_pad(led_dir), LedState(-5, 999, 7))
    assert [read_brightness(led_dir, c) for c in ("red", "green", "blue")] == [0, 255, 7]


def test_restore_does_not_touch_global(tmp_path):
    led_dir = make_led_dir(tmp_path)
    restore_led_state(make_pad(led_dir), LedState(1, 2, 3))
    assert (led_dir / "input20:global" / "brightness").read_text() == "255\n"


def test_restore_false_without_led_dir():
    assert restore_led_state(make_pad(None), LedState(1, 2, 3)) is False


def test_restore_false_when_missing(tmp_path):
    assert restore_led_state(make_pad(tmp_path / "gone"), LedState(1, 2, 3)) is False


def test_restore_false_on_oserror(tmp_path, monkeypatch):
    led_dir = make_led_dir(tmp_path)

    def deny(*args, **kwargs):
        raise PermissionError(13, "denied")
    monkeypatch.setattr("builtins.open", deny)
    assert restore_led_state(make_pad(led_dir), LedState(1, 2, 3)) is False


def test_preserved_lightbar_restores_after_block(tmp_path):
    led_dir = make_led_dir(tmp_path, values=(10, 20, 30))
    with preserved_lightbar(make_pad(led_dir)) as saved:
        assert saved == LedState(10, 20, 30)
        for c in ("red", "green", "blue"):
            (led_dir / ("input20:%s" % c) / "brightness").write_text("0\n")
    assert [read_brightness(led_dir, c) for c in ("red", "green", "blue")] == [10, 20, 30]


def test_preserved_lightbar_restores_on_exception(tmp_path):
    led_dir = make_led_dir(tmp_path, values=(10, 20, 30))
    with pytest.raises(RuntimeError):
        with preserved_lightbar(make_pad(led_dir)):
            (led_dir / "input20:red" / "brightness").write_text("0\n")
            raise RuntimeError("boom")
    assert read_brightness(led_dir, "red") == 10


def test_preserved_lightbar_without_leds_is_noop():
    with preserved_lightbar(make_pad(None)) as saved:
        assert saved is None
