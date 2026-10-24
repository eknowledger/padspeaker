"""Bluetooth link housekeeping: keep the pad's ACL link out of sniff mode.

Why: with audio flowing, a pad whose link is in *sniff* mode (link policy
``RSWITCH SNIFF``) can make BlueZ fail to push a HID message through in time.
``bluetoothd`` then logs ``hidp_send_message() BT socket write error: Resource
temporarily unavailable (11)`` about 14 seconds into streaming, and the HID
device disappears while the ACL link stays up.  Removing ``SNIFF`` from the
link's policy (``hcitool lp <MAC> RSWITCH``) fixed it on the tested hardware.
BlueZ can set the policy again, so the daemon repeats the call while streaming.

Also here: :meth:`BluetoothLink.disconnect`, which runs ``bluetoothctl
disconnect`` when the HID node vanished but the link may still be up, so the
pad is cleanly disconnected (one press of the PS button reconnects it) instead
of being left "connected" but unusable.

Nothing in this module raises: every failure becomes a return value and a log
line.  Commands run as argument lists, never through a shell, with a timeout,
and the process runner is injectable for tests.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence, Set

__all__ = ["BluetoothLink", "SniffResult", "disable_sniff"]

log = logging.getLogger("padspeaker.linkpolicy")

#: ``runner(argv, timeout)`` returns an object with ``returncode``, ``stdout``
#: and ``stderr`` (text), like :class:`subprocess.CompletedProcess`.
Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess"]

DEFAULT_TIMEOUT = 5.0
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_POLICY_RE = re.compile(r"Link policy settings:(.*)", re.IGNORECASE)


@dataclass(frozen=True)
class SniffResult:
    """Outcome of :meth:`BluetoothLink.disable_sniff`."""

    ok: bool
    """True when the link policy is known to be free of SNIFF afterwards."""
    changed: bool
    """True when this call removed SNIFF."""
    detail: str
    """A short human readable description (also the reason for a failure)."""


def _default_runner(argv: Sequence[str], timeout: float) -> "subprocess.CompletedProcess":
    return subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=timeout, check=False)


class _ToolMissing(Exception):
    pass


class BluetoothLink:
    """Link policy and disconnect helper, shared by the daemon, streamers and ``tone``.

    Args:
        runner: process runner (default :func:`subprocess.run`).
        clock: monotonic clock for the disconnect rate limit.
        hcitool, bluetoothctl: names or paths of the tools.
        timeout: seconds allowed for each command.
        disconnect_min_interval: minimum seconds between disconnects of one pad.
        which: ``shutil.which`` replacement (only used by :meth:`tools`).
    """

    def __init__(self, runner: Optional[Runner] = None,
                 clock: Callable[[], float] = time.monotonic,
                 hcitool: str = "hcitool", bluetoothctl: str = "bluetoothctl",
                 timeout: float = DEFAULT_TIMEOUT, disconnect_min_interval: float = 60.0,
                 which: Callable[[str], Optional[str]] = shutil.which) -> None:
        self._runner = runner or _default_runner
        self._clock = clock
        self._hcitool = hcitool
        self._bluetoothctl = bluetoothctl
        self._timeout = timeout
        self._min_interval = disconnect_min_interval
        self._which = which
        self._missing_warned: Set[str] = set()
        self._last_failure: Dict[str, str] = {}
        self._last_disconnect: Dict[str, float] = {}

    def tools(self) -> Dict[str, bool]:
        """Which of ``hcitool`` and ``bluetoothctl`` are installed."""
        return {"hcitool": bool(self._which(self._hcitool)),
                "bluetoothctl": bool(self._which(self._bluetoothctl))}

    # -- running commands -----------------------------------------------------

    def _run(self, argv: Sequence[str]) -> "subprocess.CompletedProcess":
        """Run a command; raise :class:`_ToolMissing` or ``OSError`` / ``SubprocessError``."""
        if argv[0] in self._missing_warned:
            raise _ToolMissing(argv[0])      # known to be absent, do not try again
        try:
            return self._runner(argv, self._timeout)
        except FileNotFoundError:
            tool = argv[0]
            if tool not in self._missing_warned:
                self._missing_warned.add(tool)
                log.warning("%s was not found, so %s will not be done (install the BlueZ "
                            "tools, or ignore this)", tool,
                            "the sniff mode fix" if tool == self._hcitool
                            else "the clean disconnect")
            raise _ToolMissing(tool)

    def _read_policy(self, mac: str) -> Optional[Set[str]]:
        """Return the link policy flags (upper case), or None if they cannot be read."""
        result = self._run([self._hcitool, "lp", mac])
        if result.returncode != 0:
            return None
        match = _POLICY_RE.search(result.stdout or "")
        if not match:
            return None
        return set(match.group(1).upper().split())

    # -- sniff ------------------------------------------------------------------

    def disable_sniff(self, mac: str) -> SniffResult:
        """Make sure the link to ``mac`` does not allow sniff mode.

        Reads the policy; when it contains ``SNIFF`` (or cannot be read) runs
        ``hcitool lp <MAC> RSWITCH`` and reads it back to verify.  Logs at INFO
        only when it really changed something.  Never raises.
        """
        mac = (mac or "").lower()
        if not _MAC_RE.match(mac):
            return SniffResult(False, False, "not a Bluetooth address: %r" % (mac,))
        try:
            return self._disable_sniff(mac)
        except _ToolMissing:
            return SniffResult(False, False, "%s is not installed" % self._hcitool)
        except Exception as exc:  # timeouts, permission errors, anything odd
            return self._failed(mac, "hcitool failed: %s" % (exc,))

    def _disable_sniff(self, mac: str) -> SniffResult:
        before = self._read_policy(mac)
        if before is not None and "SNIFF" not in before:
            self._last_failure.pop(mac, None)
            return SniffResult(True, False, "link policy %s" % " ".join(sorted(before)))
        set_result = self._run([self._hcitool, "lp", mac, "RSWITCH"])
        after = self._read_policy(mac)
        if after is None or "SNIFF" in after:
            reason = (set_result.stderr or "").strip() if set_result.returncode != 0 else ""
            state = "unknown" if after is None else "still " + " ".join(sorted(after))
            return self._failed(mac, "could not turn off sniff mode (%s)%s"
                                % (state, ": " + reason if reason else ""))
        self._last_failure.pop(mac, None)
        log.info("%s: turned off sniff mode on the Bluetooth link (link policy is now %s)",
                 mac, " ".join(sorted(after)))
        return SniffResult(True, True, "link policy %s" % " ".join(sorted(after)))

    def _failed(self, mac: str, detail: str) -> SniffResult:
        # Warn when the reason is new for this pad, otherwise stay quiet: the
        # streamer retries every 30 seconds.
        if self._last_failure.get(mac) != detail:
            self._last_failure[mac] = detail
            log.warning("%s: %s. Streaming may drop after a while; if it does, run "
                        "'hcitool lp %s RSWITCH' by hand.", mac, detail, mac)
        else:
            log.debug("%s: %s", mac, detail)
        return SniffResult(False, False, detail)

    # -- disconnect ---------------------------------------------------------------

    def disconnect(self, mac: str) -> bool:
        """Run ``bluetoothctl disconnect <MAC>`` (at most once per interval per pad).

        Returns True if the command ran and succeeded.  Never raises.
        """
        mac = (mac or "").lower()
        if not _MAC_RE.match(mac):
            return False
        now = self._clock()
        last = self._last_disconnect.get(mac)
        if last is not None and now - last < self._min_interval:
            log.debug("%s: not disconnecting again so soon", mac)
            return False
        self._last_disconnect[mac] = now
        try:
            result = self._run([self._bluetoothctl, "disconnect", mac])
        except _ToolMissing:
            return False
        except Exception as exc:
            log.warning("%s: bluetoothctl disconnect failed: %s", mac, exc)
            return False
        if result.returncode != 0:
            log.warning("%s: bluetoothctl disconnect failed: %s", mac,
                        (result.stderr or result.stdout or "").strip() or result.returncode)
            return False
        log.info("%s: disconnected the pad after it lost its HID device; press the PS button "
                 "to reconnect it", mac)
        return True


def disable_sniff(mac: str, runner: Optional[Runner] = None) -> SniffResult:
    """Remove sniff mode from the link to ``mac`` (see :meth:`BluetoothLink.disable_sniff`)."""
    return BluetoothLink(runner=runner).disable_sniff(mac)
