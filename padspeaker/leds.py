"""Save and restore a pad's lightbar through the kernel LED class.

While audio is streamed the pad may switch its lightbar off, because output
reports that carry audio do not carry the colour.  The kernel ``hid-playstation``
driver exposes the lightbar as LED class devices (``<prefix>:red``, ``:green``
and ``:blue`` with a ``brightness`` file each).  Writing the current values back
makes the driver send its own state to the pad again, which brings the light
back.  None of the functions here raise on I/O problems; they report failure
through their return value.
"""
from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from typing import Iterator, Optional

from .pads import Pad

_COLOURS = ("red", "green", "blue")
_MAX_BRIGHTNESS = 255


@dataclass(frozen=True)
class LedState:
    """Lightbar colour as kernel LED brightness values (0 to 255)."""

    r: int
    g: int
    b: int


def _brightness_paths(led_dir: str) -> Optional[list]:
    """Return the three brightness file paths (red, green, blue) or None."""
    try:
        names = os.listdir(led_dir)
    except OSError:
        return None
    paths = []
    for colour in _COLOURS:
        matches = sorted(n for n in names if n.endswith(":" + colour))
        if not matches:
            return None
        paths.append(os.path.join(led_dir, matches[0], "brightness"))
    return paths


def read_led_state(pad: Pad) -> Optional[LedState]:
    """Read the pad's current lightbar colour, or None if it cannot be read."""
    if not pad.led_dir:
        return None
    paths = _brightness_paths(pad.led_dir)
    if paths is None:
        return None
    values = []
    try:
        for path in paths:
            with open(path, "r") as handle:
                values.append(int(handle.read().strip()))
    except (OSError, ValueError):
        return None
    return LedState(*values)


def restore_led_state(pad: Pad, state: LedState) -> bool:
    """Write ``state`` to the kernel LED files so the driver re-sends it.

    Values are clamped to 0..255.  Returns True when all three colours were
    written, False on any failure (no LEDs, missing files, permission denied).
    """
    if not pad.led_dir:
        return False
    paths = _brightness_paths(pad.led_dir)
    if paths is None:
        return False
    try:
        for path, value in zip(paths, (state.r, state.g, state.b)):
            value = max(0, min(_MAX_BRIGHTNESS, int(value)))
            with open(path, "w") as handle:
                handle.write("%d\n" % value)
    except (OSError, ValueError):
        return False
    return True


@contextlib.contextmanager
def preserved_lightbar(pad: Pad) -> Iterator[Optional[LedState]]:
    """Context manager: remember the lightbar on entry, restore it on exit.

    Yields the saved :class:`LedState` (None when the lightbar is unreadable,
    in which case nothing is restored).  Restoration also happens when the
    block raises, and never raises itself.
    """
    saved = read_led_state(pad)
    try:
        yield saved
    finally:
        if saved is not None:
            restore_led_state(pad, saved)
