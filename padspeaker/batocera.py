"""Keep Batocera's saved audio output in step with the daemon's fall-back.

Batocera's Audio output menu stores the chosen sink name in
``/userdata/system/batocera.conf`` as ``audio.device=<sink>``.  When a pad
disappears while its sink is the default output, the daemon moves the default
back to a real sink; this module also puts the *saved* value back, so the menu
does not keep showing a raw name for a sink that no longer exists.

The file is never edited directly: reading and writing go through Batocera's
own tools, ``batocera-settings-get`` and ``batocera-settings-set``, which keep
the file consistent.  On a system without those tools everything here is a
silent no-op.  Nothing raises.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Callable, Optional, Sequence, Union

__all__ = ["restore_saved_audio_device", "tool_available"]

log = logging.getLogger("padspeaker.batocera")

SETTING = "audio.device"
GET_TOOL = "batocera-settings-get"
SET_TOOL = "batocera-settings-set"
DEFAULT_TIMEOUT = 5.0

Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess"]


def _default_runner(argv: Sequence[str], timeout: float) -> "subprocess.CompletedProcess":
    return subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=timeout, check=False)


def tool_available(which: Callable[[str], Optional[str]] = shutil.which) -> bool:
    """True on a system with Batocera's settings tools."""
    return bool(which(GET_TOOL)) and bool(which(SET_TOOL))


def restore_saved_audio_device(
    fallback: str,
    prefix: Union[str, Sequence[str]] = "padspeaker_",
    runner: Optional[Runner] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Set the saved ``audio.device`` to ``fallback`` if it names one of our sinks.

    Args:
        fallback: the real sink the daemon just made the default.
        prefix: prefix of our sink names, or several (pad sinks and combined
            outputs); only a saved value starting with one of them is replaced
            (anything else is the user's choice and is left alone).
        runner: ``runner(argv, timeout)``; default :func:`subprocess.run`.
        which: ``shutil.which`` replacement.

    Returns:
        True when the saved value was changed.
    """
    prefixes = (prefix,) if isinstance(prefix, str) else tuple(prefix)
    if not fallback or fallback.startswith("-") or fallback.startswith(prefixes):
        return False
    if not tool_available(which):
        return False
    run = runner or _default_runner
    try:
        got = run([GET_TOOL, SETTING], timeout)
        saved = (got.stdout or "").strip() if got.returncode == 0 else ""
        if not saved.startswith(prefixes):
            return False
        result = run([SET_TOOL, SETTING, fallback], timeout)
        if result.returncode != 0:
            log.warning("could not set %s to %s: %s", SETTING, fallback,
                        (result.stderr or "").strip() or result.returncode)
            return False
    except Exception as exc:
        log.warning("could not update the saved %s: %s", SETTING, exc)
        return False
    log.info("saved %s changed from %s to %s", SETTING, saved, fallback)
    return True
