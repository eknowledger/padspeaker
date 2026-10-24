"""The padspeaker daemon: one virtual sink and one streamer per connected pad.

:class:`Daemon` polls for pads (once per second by default).  For every pad it
is allowed to touch it creates a virtual sink (``padspeaker_<mac tail>``) and a
:class:`~padspeaker.streamer.PadStreamer` that runs in its own thread.  When a
pad disappears the streamer is stopped, the default sink is moved away from the
pad's sink if needed, and the sink is removed, so the audio output menu never
lists a speaker that is not there.

Around that core this module provides the operational parts:

* :class:`DaemonConfig` and :func:`load_config_file` (a small JSON file);
* :class:`InstanceLock`, a ``flock`` based single instance guard;
* :func:`write_status_file`, atomic JSON snapshots for ``status``;
* :func:`setup_logging`, stderr plus an optional rotating log file.

Configuration file
------------------
A JSON object; every key is optional.  Command line flags override the file.

``log_file``
    Path of a rotating log file (default: log to stderr only).
``status_file``
    Where the status snapshot is written (default: ``status.json`` in a private
    state directory, see below; ``null`` disables it, which also disables the
    ``status`` command).
``poll_interval``
    Seconds between pad scans, 0.1 to 60 (default 1).
``jitter_reports``
    Reports buffered before sending, 1 to 8 (default 3, i.e. 48 ms of latency).
``speaker_volume_max``
    Cap for the pad speaker volume, 0 to 79 (default 79, the pad's maximum).
``sink_name_prefix``
    Prefix of the virtual sink names (default ``padspeaker_``).
``keep_lightbar``
    ``true`` (default) re-sends the lightbar colour about twice a second while
    streaming, because audio reports alone switch the light off on the pads
    tested; ``false`` turns that off.
``lightbar_fallback``
    ``[red, green, blue]`` (each 0 to 255, default ``[0, 0, 64]``, a dim blue)
    used when the pad's own colour cannot be read from the kernel.
``disable_sniff``
    ``true`` (default): keep each pad's Bluetooth link out of sniff mode (when a
    pad connects, before streaming starts, and every 30 s while streaming).
    Sniff mode made BlueZ drop the pad's HID device about 14 s into streaming.
``disconnect_on_hid_loss``
    ``true`` (default): when writing to a pad fails with ENODEV or EIO, run
    ``bluetoothctl disconnect`` once so the pad is cleanly disconnected and one
    press of the PS button reconnects it.
``sync_batocera_audio_device``
    When the daemon moves the default output away from a vanished pad, also set
    Batocera's saved ``audio.device`` (through ``batocera-settings-set``) if it
    still names one of our sinks.  ``null`` (default) means: only where those
    tools exist; ``true`` behaves the same; ``false`` never.
``combine_pads``, ``combine_tv_pad``, ``combine_tv_all``
    All ``true`` by default.  Create the combined outputs ``padspeaker_all``
    (every pad, with 2 or more pads), ``padspeaker_tv_<tail>`` (the main output plus one pad) and
    ``padspeaker_tv_all`` (the main output plus every pad, with 2 or more pads); ``false`` turns
    a group off.
``tv_sink``
    Name of the main output (often a TV) that the ``padspeaker_tv_*`` combos play to
    together with the pads.  ``null`` (default): the default output when the
    daemon first sees a pad, if it is not one of ours; chosen again when that
    sink disappears.  Without one, those combos are skipped.  (The key names
    keep "tv" for compatibility; the output may be a monitor, headphones or a
    sound bar.)
``tv_label``
    Wording for that output in the combined outputs' menu text, for example
    ``"TV"`` gives "TV + controller speaker (...)".  ``null`` (default): the
    output's own name, taken from its ``node.nick`` or ``alsa.name`` (a TV or
    monitor reports its name this way: "Example Display (HDMI 2) + controller speaker
    (...)"), ignoring generic values such as "HDMI"; failing that its
    description from the audio server ("HD-Audio Generic Digital Stereo (HDMI
    2) + ...").  Quotes and
    control characters are removed and the text is cut to 60 characters.  The
    sink names (``padspeaker_tv_<tail>``, ``padspeaker_tv_all``) never change, so saved
    selections stay valid.
``tv_delay_ms``
    Planned, not implemented yet: delay for the main-output leg of those combos (it
    is ahead of Bluetooth by roughly 100 to 150 ms).  Accepted (0 to 2000)
    but has no effect; the daemon logs a warning when it is not 0.
``start_on_running``
    ``true`` (default): a pad also starts streaming when its sink is RUNNING
    (sound reaches it through a combined output), not only when it is the
    default output.  Streaming stops when it is neither for about 2 seconds.
``only_macs``
    Allow-list of pad addresses; when not empty, other pads are left alone.
``ignore_macs``
    Pad addresses that are never touched (no sink, no writes).
``extra_pad_ids``
    Extra ``VVVV:PPPP`` hex vendor and product ids (Bluetooth only) to treat as
    DS4-compatible pads, for clones that report other ids (default: none).
``lock_file``, ``runtime_dir``
    Single instance lock path, and the ``XDG_RUNTIME_DIR`` to use for ``pactl``
    and ``parec`` when the daemon is started without one (as at boot).

State directory
---------------
Unless ``lock_file`` and ``status_file`` are both given, they live in a private
directory: ``<runtime_dir>/padspeaker`` if ``runtime_dir`` is set, else
``$XDG_RUNTIME_DIR/padspeaker`` if that variable names a directory you own,
else ``/run/padspeaker`` when running as root.  Anywhere else the daemon
refuses to start rather than use a shared directory such as ``/tmp``.  The
directory is created with mode 0700; an existing one must be a real directory
(not a symlink) owned by you and not writable by others.  The lock file is
opened without following symlinks and must be a regular file you own.
"""
from __future__ import annotations

import fcntl
import json
import logging
import logging.handlers
import os
import re
import secrets
import signal
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from . import __version__
from .batocera import restore_saved_audio_device
from .combos import (ComboFlags, ComboManager, PadSink, is_ours, our_prefixes,
                     resolve_label)
from .linkpolicy import BluetoothLink
from .pads import PadWatcher, Pad, mac_tail, normalize_pad_id
from .protocol import MAX_SPEAKER_VOLUME
from .sinks import Sink, SinkError, SinkManager
from .streamer import PadStreamer, StreamerConfig

__all__ = [
    "ConfigError",
    "AlreadyRunning",
    "DaemonConfig",
    "InstanceLock",
    "ThreadRunner",
    "Daemon",
    "StateDirError",
    "AUTO",
    "default_state_dir",
    "default_status_file",
    "default_lock_file",
    "secure_state_dir",
    "load_config_file",
    "normalize_mac",
    "setup_logging",
    "write_status_file",
]

log = logging.getLogger("padspeaker.daemon")

LABEL_REFRESH = 30.0
"""Seconds after which the main output's properties are read again (for its label)."""

OBSERVE_INTERVAL = 2.0
"""Seconds between looks at the audio server for the combined outputs."""

STATUS_INTERVAL = 2.0
"""Seconds between status file refreshes (a change is written at once)."""

_SINK_RETRY_FIRST = 1.0
_SINK_RETRY_MAX = 30.0
_SINK_LOG_INTERVAL = 60.0
_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
_MAC_HEX_RE = re.compile(r"^[0-9a-f]{12}$")
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_MAX_BYTES = 1024 * 1024
_LOG_BACKUPS = 3
_OUR_HANDLER = "_padspeaker_handler"


class ConfigError(ValueError):
    """The configuration is invalid; the message says what to change."""


class AlreadyRunning(RuntimeError):
    """Another instance holds the lock; the message names its pid when known."""


# --- paths ---------------------------------------------------------------------

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_STATE_DIR_NAME = "padspeaker"
_ROOT_STATE_DIR = "/run/padspeaker"
_EXPLICIT_PATH_HINT = ("pass --runtime-dir, or --lock-file and --status-file, to say where "
                       "they should live")


class StateDirError(ConfigError):
    """No safe place for the lock and status files; the message says how to fix it."""


def default_state_dir(runtime_dir: Optional[str] = None) -> str:
    """Return the directory for the lock and status files (it is not created here).

    In order of preference: ``<runtime_dir>/padspeaker`` when a runtime
    directory is given, ``$XDG_RUNTIME_DIR/padspeaker`` when that variable
    names a directory owned by the current user, and ``/run/padspeaker``
    when running as root.  There is deliberately no fallback to a shared
    directory such as ``/tmp``.

    Raises:
        StateDirError: if none of these applies.
    """
    if runtime_dir:
        return os.path.join(runtime_dir, _STATE_DIR_NAME)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        try:
            info = os.stat(xdg)
        except OSError:
            info = None
        if info is not None and stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid():
            return os.path.join(xdg, _STATE_DIR_NAME)
    if os.geteuid() == 0:
        return _ROOT_STATE_DIR
    raise StateDirError(
        "there is no private runtime directory to keep the lock and status files in "
        "(XDG_RUNTIME_DIR is not set to a directory you own, and you are not root); "
        + _EXPLICIT_PATH_HINT)


def default_status_file(runtime_dir: Optional[str] = None) -> str:
    """Default location of the status snapshot (see :func:`default_state_dir`)."""
    return os.path.join(default_state_dir(runtime_dir), "status.json")


def default_lock_file(runtime_dir: Optional[str] = None) -> str:
    """Default location of the single instance lock (see :func:`default_state_dir`)."""
    return os.path.join(default_state_dir(runtime_dir), "daemon.lock")


def secure_state_dir(path: str) -> None:
    """Create ``path`` (mode 0700) or check that an existing one is safe to use.

    An existing path must be a real directory (not a symlink) owned by the
    current user with no group or other write permission.

    Raises:
        StateDirError: if the directory cannot be created or is not safe.
    """
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise StateDirError("cannot create the directory %s: %s; %s"
                            % (path, exc.strerror or exc, _EXPLICIT_PATH_HINT))
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise StateDirError("%s exists but is not a plain directory (a symbolic link?); "
                            "refusing to use it" % path)
    if info.st_uid != os.geteuid():
        raise StateDirError("%s is owned by another user; refusing to use it" % path)
    if info.st_mode & 0o022:
        raise StateDirError("%s can be written by other users; refusing to use it "
                            "(chmod go-w, or remove it)" % path)


# --- configuration ---------------------------------------------------------------

def normalize_mac(value: Any) -> str:
    """Return ``value`` as a lower-case ``aa:bb:cc:dd:ee:ff`` address.

    Accepts ``:`` or ``-`` separators or none at all.

    Raises:
        ConfigError: if it is not a Bluetooth address.
    """
    digits = re.sub(r"[:\-]", "", str(value)).lower()
    if not _MAC_HEX_RE.match(digits):
        raise ConfigError("%r is not a valid MAC address; use six pairs of hex digits "
                          "such as aa:bb:cc:dd:11:22" % (value,))
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def _number(data: Dict[str, Any], key: str, low: float, high: float, integer: bool) -> Any:
    value = data[key]
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if ok and integer and float(value) != int(value):
        ok = False
    if not ok or not low <= value <= high:
        kind = "a whole number" if integer else "a number"
        raise ConfigError("%s must be %s from %g to %g, got %r" % (key, kind, low, high, value))
    return int(value) if integer else float(value)


MAX_EXTRA_PAD_IDS = 64


def _pad_ids(value: Any) -> List[str]:
    """Validate ``extra_pad_ids``: a list of ``VVVV:PPPP`` hex ids, upper-cased, without repeats."""
    hint = ("extra_pad_ids must be a list of 'VVVV:PPPP' vendor and product ids in hex, such as "
            "[\"054C:09CC\"] (Bluetooth pads only)")
    if not isinstance(value, list):
        raise ConfigError("%s, got %r" % (hint, value))
    if len(value) > MAX_EXTRA_PAD_IDS:
        raise ConfigError("extra_pad_ids can hold at most %d ids, got %d" % (MAX_EXTRA_PAD_IDS, len(value)))
    ids: List[str] = []
    for item in value:
        try:
            pad_id = normalize_pad_id(item)
        except ValueError:
            raise ConfigError("%s, got %r" % (hint, item))
        if pad_id not in ids:
            ids.append(pad_id)
    return ids


class _Auto:
    """Marker for "use the default location" (distinct from None, which disables)."""

    def __repr__(self) -> str:
        return "AUTO"


AUTO = _Auto()


@dataclass
class DaemonConfig:
    """Settings of the daemon; see the module docstring for the file format.

    ``status_file`` and ``lock_file`` are :data:`AUTO` unless set: the default
    place is chosen by :func:`default_state_dir`.  ``status_file=None`` turns
    the status file off; ``lock_file=None`` turns the single instance guard off.
    """

    log_file: Optional[str] = None
    status_file: Union[str, None, _Auto] = AUTO
    lock_file: Union[str, None, _Auto] = AUTO
    runtime_dir: Optional[str] = None
    poll_interval: float = 1.0
    jitter_reports: int = 3
    speaker_volume_max: int = MAX_SPEAKER_VOLUME
    sink_name_prefix: str = "padspeaker_"
    only_macs: List[str] = field(default_factory=list)
    ignore_macs: List[str] = field(default_factory=list)
    extra_pad_ids: List[str] = field(default_factory=list)
    keep_lightbar: bool = True
    lightbar_fallback: List[int] = field(default_factory=lambda: [0, 0, 64])
    disable_sniff: bool = True
    disconnect_on_hid_loss: bool = True
    sync_batocera_audio_device: Optional[bool] = None
    tv_sink: Optional[str] = None
    combine_pads: bool = True
    combine_tv_pad: bool = True
    combine_tv_all: bool = True
    tv_delay_ms: int = 0
    tv_label: Optional[str] = None
    start_on_running: bool = True

    _KEYS = ("log_file", "status_file", "lock_file", "runtime_dir", "poll_interval",
             "jitter_reports", "speaker_volume_max", "sink_name_prefix",
             "only_macs", "ignore_macs", "extra_pad_ids", "keep_lightbar", "lightbar_fallback",
             "disable_sniff",
             "disconnect_on_hid_loss", "sync_batocera_audio_device", "tv_sink",
             "tv_label", "combine_pads", "combine_tv_pad", "combine_tv_all", "tv_delay_ms",
             "start_on_running")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DaemonConfig":
        """Build a validated config from a dict (unknown keys are an error).

        Raises:
            ConfigError: with a plain-language message.
        """
        unknown = sorted(set(data) - set(cls._KEYS))
        if unknown:
            raise ConfigError("unknown setting %s; valid settings are: %s"
                              % (", ".join(repr(k) for k in unknown), ", ".join(cls._KEYS)))
        config = cls()
        for key in ("log_file", "status_file", "lock_file", "runtime_dir"):
            if key in data:
                value = data[key]
                if value is not None and not isinstance(value, str):
                    raise ConfigError("%s must be a path (text) or null, got %r" % (key, value))
                setattr(config, key, value or None)
        if "poll_interval" in data:
            config.poll_interval = _number(data, "poll_interval", 0.1, 60, False)
        if "jitter_reports" in data:
            config.jitter_reports = _number(data, "jitter_reports", 1, 8, True)
        if "speaker_volume_max" in data:
            config.speaker_volume_max = _number(data, "speaker_volume_max", 0,
                                                MAX_SPEAKER_VOLUME, True)
        if "sink_name_prefix" in data:
            prefix = data["sink_name_prefix"]
            if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
                raise ConfigError("sink_name_prefix must be 1 to 40 letters, digits, '_', '.' "
                                  "or '-', got %r" % (prefix,))
            config.sink_name_prefix = prefix
        if "keep_lightbar" in data:
            if not isinstance(data["keep_lightbar"], bool):
                raise ConfigError("keep_lightbar must be true or false, got %r"
                                  % (data["keep_lightbar"],))
            config.keep_lightbar = data["keep_lightbar"]
        for key in ("disable_sniff", "disconnect_on_hid_loss", "combine_pads", "combine_tv_pad",
                    "combine_tv_all", "start_on_running"):
            if key in data:
                if not isinstance(data[key], bool):
                    raise ConfigError("%s must be true or false, got %r" % (key, data[key]))
                setattr(config, key, data[key])
        if "sync_batocera_audio_device" in data:
            value = data["sync_batocera_audio_device"]
            if value is not None and not isinstance(value, bool):
                raise ConfigError("sync_batocera_audio_device must be true, false or null "
                                  "(null: only where Batocera's tools exist), got %r" % (value,))
            config.sync_batocera_audio_device = value
        if "tv_sink" in data:
            value = data["tv_sink"]
            if value is not None and not (isinstance(value, str) and _PREFIX_RE.match(value)):
                raise ConfigError("tv_sink must be the name of a sink (see 'pactl list short "
                                  "sinks') or null for automatic, got %r" % (value,))
            config.tv_sink = value
        if "tv_label" in data:
            value = data["tv_label"]
            if value is not None and not isinstance(value, str):
                raise ConfigError("tv_label must be text such as \"TV\" or null for the "
                                  "output's own name, got %r" % (value,))
            config.tv_label = value or None
        if "tv_delay_ms" in data:
            config.tv_delay_ms = _number(data, "tv_delay_ms", 0, 2000, True)
        if "lightbar_fallback" in data:
            colour = data["lightbar_fallback"]
            if (not isinstance(colour, list) or len(colour) != 3 or not all(
                    isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255
                    for c in colour)):
                raise ConfigError("lightbar_fallback must be three numbers from 0 to 255 "
                                  "(red, green, blue) such as [0, 0, 64], got %r" % (colour,))
            config.lightbar_fallback = list(colour)
        for key in ("only_macs", "ignore_macs"):
            if key in data:
                value = data[key]
                if not isinstance(value, list):
                    raise ConfigError("%s must be a list of MAC addresses, got %r" % (key, value))
                setattr(config, key, [normalize_mac(v) for v in value])
        if "extra_pad_ids" in data:
            config.extra_pad_ids = _pad_ids(data["extra_pad_ids"])
        return config

    def allows(self, mac: str) -> bool:
        """Return True if the daemon may touch the pad with this address."""
        mac = mac.lower()
        if mac in self.ignore_macs:
            return False
        return not self.only_macs or mac in self.only_macs

    def status_path(self) -> Optional[str]:
        """The status file path with the default applied (None when disabled)."""
        if self.status_file is AUTO:
            return default_status_file(self.runtime_dir)
        return self.status_file  # type: ignore[return-value]

    def lock_path(self) -> Optional[str]:
        """The lock file path with the default applied (None when disabled)."""
        if self.lock_file is AUTO:
            return default_lock_file(self.runtime_dir)
        return self.lock_file  # type: ignore[return-value]

    def uses_default_state_dir(self) -> bool:
        """True if the lock or status file lives in the default state directory."""
        return self.status_file is AUTO or self.lock_file is AUTO


def load_config_file(path: str) -> Dict[str, Any]:
    """Read a JSON configuration file into a dict.

    Raises:
        ConfigError: if the file cannot be read or is not a JSON object.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ConfigError("cannot read the configuration file %s: %s"
                          % (path, exc.strerror or exc))
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError("the configuration file %s is not valid JSON (%s)" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("the configuration file %s must contain a JSON object "
                          "such as {\"poll_interval\": 2}" % path)
    return data


# --- single instance lock ----------------------------------------------------------

class InstanceLock:
    """Exclusive ``flock`` on a file; the holder's pid is written into it.

    The lock disappears with the process, so a crash never leaves a stale lock.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        """Take the lock.

        Raises:
            AlreadyRunning: if another process holds it.
        """
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC, 0o600)
        except OSError as exc:
            raise StateDirError("cannot open the lock file %s: %s (a symbolic link there is "
                                "refused on purpose)" % (self.path, exc.strerror or exc))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            os.close(fd)
            raise StateDirError("the lock file %s is not a regular file owned by you; "
                                "refusing to use it" % self.path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                holder = os.read(fd, 32).decode("ascii", "replace").strip()
            except OSError:
                holder = ""
            os.close(fd)
            who = " (pid %s)" % holder if holder.isdigit() else ""
            raise AlreadyRunning("padspeaker is already running%s; lock file %s. "
                                 "Stop that instance first." % (who, self.path))
        os.ftruncate(fd, 0)
        os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
        self._fd = fd

    def release(self) -> None:
        """Drop the lock.  Safe to call more than once."""
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


# --- status file ---------------------------------------------------------------------

def write_status_file(path: str, data: Dict[str, Any]) -> None:
    """Write ``data`` as JSON to ``path`` atomically (temp file, then rename).

    The temporary file is created next to ``path`` with a random name, with
    ``O_EXCL | O_NOFOLLOW`` so a pre-planted file or symlink is never written
    through.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = os.path.join(directory, ".status-%s.tmp" % secrets.token_hex(8))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- logging ---------------------------------------------------------------------------

def setup_logging(verbosity: int = 0, log_file: Optional[str] = None,
                  stream: Any = None) -> logging.Logger:
    """Configure the ``padspeaker`` logger and return it.

    Args:
        verbosity: ``-1`` warnings only, ``0`` info (default), ``1`` or more debug.
        log_file: optional file with rotation (1 MiB, three backups).  If it
            cannot be opened a warning is logged and stderr is used alone.
        stream: stream for the console handler (default ``sys.stderr``).

    Calling it again replaces the handlers installed by the previous call.
    """
    logger = logging.getLogger("padspeaker")
    for handler in list(logger.handlers):
        if getattr(handler, _OUR_HANDLER, False):
            logger.removeHandler(handler)
            handler.close()
    level = logging.WARNING if verbosity < 0 else logging.DEBUG if verbosity > 0 else logging.INFO
    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter(_LOG_FORMAT)
    console = logging.StreamHandler(stream or sys.stderr)
    console.setFormatter(formatter)
    setattr(console, _OUR_HANDLER, True)
    logger.addHandler(console)
    if log_file:
        try:
            directory = os.path.dirname(log_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8")
        except OSError as exc:
            logger.warning("cannot write the log file %s (%s); logging to the console only",
                           log_file, exc.strerror or exc)
        else:
            handler.setFormatter(formatter)
            setattr(handler, _OUR_HANDLER, True)
            logger.addHandler(handler)
    return logger


# --- streamer thread -----------------------------------------------------------------------

class ThreadRunner:
    """Runs ``streamer.run(stop_event)`` in a daemon thread."""

    def __init__(self, streamer: Any, join_timeout: float = 10.0) -> None:
        self._streamer = streamer
        self._join_timeout = join_timeout
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def alive(self) -> bool:
        """True while the thread runs."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the thread."""
        self._thread = threading.Thread(target=self._streamer.run, args=(self._stop,),
                                        name="streamer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to finish and wait for it.  Safe to call more than once."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(self._join_timeout)
            if thread.is_alive():
                log.warning("a streamer thread did not stop within %.0f s", self._join_timeout)


# --- daemon -----------------------------------------------------------------------------------

class _Slot:
    """Everything the daemon holds for one pad."""

    def __init__(self, pad: Pad, sink_name: str) -> None:
        self.pad = pad
        self.sink_name = sink_name
        self.module: Optional[int] = None
        self.streamer: Any = None
        self.runner: Any = None
        self.error: Optional[str] = None
        self.retry_at = 0.0
        self.delay = _SINK_RETRY_FIRST
        self.first_failure: Optional[float] = None
        self.last_logged: Optional[float] = None

    @property
    def key(self) -> Any:
        return (self.pad.mac, self.pad.hidraw_path)

    @property
    def ready(self) -> bool:
        return self.runner is not None


class Daemon:
    """Owns the pads, their sinks and their streamers.

    Args:
        config: validated settings.
        sinks: a :class:`~padspeaker.sinks.SinkManager` (or a fake).
        watcher: pad discovery; default scans sysfs.
        streamer_factory: ``(pad, sink_name, fallback) -> streamer``; the
            default builds a :class:`~padspeaker.streamer.PadStreamer`.
        runner_factory: ``(streamer) -> runner`` with ``start()`` and ``stop()``;
            the default runs each streamer in a thread.
        link: Bluetooth helper with ``disable_sniff(mac)`` and ``disconnect(mac)``
            (default :class:`~padspeaker.linkpolicy.BluetoothLink`).
        batocera_restore: ``(fallback_sink, our_prefixes) -> bool`` that fixes Batocera's
            saved audio output (default :func:`~padspeaker.batocera.restore_saved_audio_device`).
        clock, wall_clock: monotonic and wall time functions.
    """

    def __init__(
        self,
        config: DaemonConfig,
        *,
        sinks: Optional[Any] = None,
        watcher: Optional[PadWatcher] = None,
        streamer_factory: Optional[Callable[[Pad, str, Callable[[Optional[str]], None]], Any]] = None,
        runner_factory: Optional[Callable[[Any], Any]] = None,
        link: Optional[Any] = None,
        batocera_restore: Optional[Callable[[str, Sequence[str]], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._sinks = sinks if sinks is not None else SinkManager(runtime_dir=config.runtime_dir)
        self._watcher = watcher if watcher is not None else PadWatcher(
            extra_ids=config.extra_pad_ids)
        self._streamer_factory = streamer_factory or self._default_streamer
        self._runner_factory = runner_factory or ThreadRunner
        self._link = link if link is not None else BluetoothLink()
        self._batocera_restore = batocera_restore or (
            lambda fallback, prefix: restore_saved_audio_device(fallback, prefix))
        self._clock = clock
        self._wall = wall_clock
        self._combos = ComboManager(
            self._sinks, ComboFlags(config.combine_pads, config.combine_tv_pad,
                                    config.combine_tv_all),
            fallback=self._fallback, previous_default=lambda: self._prev_default,
            pad_prefix=config.sink_name_prefix)
        self._prev_default: Optional[str] = None
        self._tv_sink: Optional[str] = None
        self._tv_warned = False
        self._tv_warned_missing = False
        self._tv_cache: Optional[Tuple[str, str, float, Dict[str, str]]] = None
        self._combos_dirty = True
        self._next_observe = 0.0
        self._status_path = config.status_path()
        self._lock_path = config.lock_path()
        self._stop = threading.Event()
        self._slots: Dict[Any, _Slot] = {}
        self._ignored: List[str] = []
        self._started_wall = wall_clock()
        self._next_status = 0.0
        self._status_failed = False
        self._stale_checked = False
        self._shut_down = False

    # -- public -------------------------------------------------------------------

    def request_stop(self) -> None:
        """Ask :meth:`run` to finish (safe to call from a signal handler)."""
        self._stop.set()

    def run(self, install_signals: bool = True) -> int:
        """Run until stopped; return the process exit code (0).

        Takes the single instance lock first.

        Raises:
            AlreadyRunning: if another instance is running; nothing was touched.
            StateDirError: if the default state directory is not safe to use.
        """
        if self.config.uses_default_state_dir():
            secure_state_dir(default_state_dir(self.config.runtime_dir))
        lock = InstanceLock(self._lock_path) if self._lock_path else None
        if lock is not None:
            lock.acquire()
        previous = self._install_signals() if install_signals else {}
        try:
            log.info("padspeaker %s started (poll every %.1f s)",
                     __version__, self.config.poll_interval)
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self.config.poll_interval)
        finally:
            try:
                self.shutdown()
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
                if lock is not None:
                    lock.release()
        return 0

    def tick(self) -> None:
        """One pass of the main loop: scan pads, retry sinks, refresh the status."""
        changed = False
        try:
            added, removed = self._watcher.poll()
        except Exception as exc:
            log.warning("pad scan failed: %s", exc)
            added, removed = [], []
        for pad in removed:
            changed |= self._remove_pad(pad)
        for pad in added:
            changed |= self._add_pad(pad)
        now = self._clock()
        for slot in list(self._slots.values()):
            if not slot.ready and now >= slot.retry_at:
                changed |= self._setup_slot(slot)
        if self._combos_enabled() and (self._combos_dirty or now >= self._next_observe):
            self._observe(now)
        if not self._stale_checked:
            self._remove_stale_sinks()
        if changed or now >= self._next_status:
            self._write_status()

    def shutdown(self) -> None:
        """Stop all streamers, restore lightbars, remove the sinks, drop the status file."""
        if self._shut_down:
            return
        self._shut_down = True
        log.info("shutting down")
        self._combos.remove_all()
        for slot in list(self._slots.values()):
            self._teardown(slot)
        self._slots.clear()
        if self._status_path:
            try:
                os.unlink(self._status_path)
            except OSError:
                pass

    # -- pads ---------------------------------------------------------------------

    def _add_pad(self, pad: Pad) -> bool:
        if not self.config.allows(pad.mac):
            log.info("pad %s (%s) is not allowed by the configuration, leaving it alone",
                     pad.mac, pad.hidraw_path)
            if pad.mac not in self._ignored:
                self._ignored.append(pad.mac)
            return True
        log.info("pad connected: %s %s (%s)", pad.name, pad.mac, pad.hidraw_path)
        if pad.extra_id:
            log.info("%s was recognised through extra_pad_ids (id %s); it is not a known Sony id",
                     pad.mac, pad.hid_id.split(":", 1)[-1])
        slot = _Slot(pad, self._sink_name(pad))
        self._slots[slot.key] = slot
        if self.config.disable_sniff:
            try:
                self._link.disable_sniff(pad.mac)
            except Exception as exc:
                log.debug("link policy call for %s failed: %s", pad.mac, exc)
        self._setup_slot(slot)
        return True

    def _remove_pad(self, pad: Pad) -> bool:
        slot = self._slots.pop((pad.mac, pad.hidraw_path), None)
        if slot is None:
            if pad.mac in self._ignored and not any(
                    p.mac == pad.mac for p in self._watcher.pads):
                self._ignored.remove(pad.mac)
                return True
            return False
        log.info("pad disconnected: %s %s", pad.name, pad.mac)
        if self._combos_enabled():
            # Take the combined outputs that include this pad away first (moving
            # the default off one of them if needed), while its sink still exists.
            self._observe(self._clock())
        self._teardown(slot)
        self._combos_dirty = True
        return True

    def _sink_name(self, pad: Pad) -> str:
        name = self.config.sink_name_prefix + mac_tail(pad.mac)
        if any(s.sink_name == name for s in self._slots.values()):
            name = self.config.sink_name_prefix + pad.mac.replace(":", "")
        return name

    def _setup_slot(self, slot: _Slot) -> bool:
        """Create the slot's sink and streamer; on failure schedule a retry."""
        now = self._clock()
        try:
            if slot.module is None:
                description = "Controller speaker (%s %s)" % (slot.pad.name, mac_tail(slot.pad.mac))
                slot.module = self._sinks.create_null_sink(slot.sink_name, description)
                log.info("created sink %s", slot.sink_name)
            slot.streamer = self._streamer_factory(slot.pad, slot.sink_name, self._fallback)
            slot.runner = self._runner_factory(slot.streamer)
            slot.runner.start()
        except Exception as exc:
            self._sink_failed(slot, exc, now)
            return True
        slot.error = None
        self._combos_dirty = True
        return True

    def _sink_failed(self, slot: _Slot, exc: BaseException, now: float) -> None:
        slot.error = str(exc)
        slot.streamer = None
        slot.runner = None
        if slot.first_failure is None:
            slot.first_failure = now
        waited = now - slot.first_failure
        if slot.last_logged is None or now - slot.last_logged >= _SINK_LOG_INTERVAL:
            slot.last_logged = now
            log.warning("cannot set up the speaker sink for %s: %s. The audio server may "
                        "not be ready yet; retrying (waiting for %.0f s so far)",
                        slot.pad.mac, exc, waited)
        else:
            log.debug("sink setup for %s failed again: %s", slot.pad.mac, exc)
        slot.retry_at = now + slot.delay
        slot.delay = min(slot.delay * 2, _SINK_RETRY_MAX)

    def _teardown(self, slot: _Slot) -> None:
        """Stop the streamer, restore the default sink if needed, remove the sink."""
        if slot.runner is not None:
            try:
                slot.runner.stop()
            except Exception as exc:
                log.warning("stopping the streamer of %s failed: %s", slot.pad.mac, exc)
        if slot.streamer is not None:
            try:
                slot.streamer.shutdown(restore_default=True)
            except Exception as exc:
                log.warning("shutting down the streamer of %s failed: %s", slot.pad.mac, exc)
        if slot.module is not None:
            try:
                self._sinks.remove_null_sink(slot.module)
                log.info("removed sink %s", slot.sink_name)
            except Exception as exc:
                log.warning("cannot remove sink %s: %s", slot.sink_name, exc)
        slot.runner = None
        slot.streamer = None
        slot.module = None

    # -- sinks --------------------------------------------------------------------

    def _fallback(self, previous: Optional[str]) -> None:
        """Make a real output the default again (called when a pad's sink must go).

        Prefers ``previous`` (the default before the pad was chosen); otherwise
        the first sink that is not one of ours.
        """
        try:
            candidates: List[Sink] = [
                s for s in self._sinks.list_sinks()
                if not is_ours(s.name, self.config.sink_name_prefix)]
            names = [s.name for s in candidates]
            target = previous if previous in names else (names[0] if names else None)
            if target is None:
                log.warning("no other audio output is available to fall back to")
                return
            self._sinks.set_default_sink(target)
            log.info("default sink set back to %s", target)
            if self.config.sync_batocera_audio_device is not False:
                try:
                    self._batocera_restore(target, our_prefixes(self.config.sink_name_prefix))
                except Exception as exc:
                    log.warning("could not update Batocera's saved audio output: %s", exc)
        except Exception as exc:
            log.warning("could not restore the default sink: %s", exc)

    def _remove_stale_sinks(self) -> None:
        """Remove sinks of ours (pads and combos) left behind by an earlier run."""
        try:
            existing = self._sinks.list_sinks()
        except Exception as exc:
            log.debug("cannot list sinks to look for stale ones: %s", exc)
            return
        self._stale_checked = True
        wanted = {s.sink_name for s in self._slots.values()} | set(self._combos.slaves_of())
        stale = [s.name for s in existing
                 if is_ours(s.name, self.config.sink_name_prefix) and s.name not in wanted]
        if not stale:
            return
        try:
            default = self._sinks.default_sink()
        except Exception:
            default = None
        for name in stale:
            log.info("removing stale sink %s", name)
            if default == name:
                self._fallback(self._prev_default)
            try:
                self._sinks.remove_null_sink(name)
            except Exception as exc:
                log.warning("cannot remove stale sink %s: %s", name, exc)

    # -- combined outputs -----------------------------------------------------------

    def _combos_enabled(self) -> bool:
        c = self.config
        return c.combine_pads or c.combine_tv_pad or c.combine_tv_all

    def _observe(self, now: float) -> None:
        """Look at the audio server and bring the combined outputs up to date.

        Remembers the user's current non-ours default (for restoring it later),
        chooses the main output sink and asks the combo manager to rebuild what changed.
        """
        self._next_observe = now + OBSERVE_INTERVAL
        try:
            listed = list(self._sinks.list_sinks())
            names = [s.name for s in listed]
        except Exception as exc:
            log.debug("cannot list sinks: %s", exc)
            self._combos_dirty = True
            return
        try:
            default = self._sinks.default_sink()
        except Exception:
            default = None
        prefix = self.config.sink_name_prefix
        if default and default in names and not is_ours(default, prefix):
            self._prev_default = default
        tv = self._choose_tv(names, default)
        pads = [PadSink(s.pad.mac, s.pad.name, s.sink_name)
                for s in self._slots.values() if s.module is not None]
        if self.config.tv_delay_ms and not self._tv_warned:
            self._tv_warned = True
            log.warning("tv_delay_ms is set but not implemented yet; it has no effect")
        descriptions = {s.name: s.description for s in listed}
        label = resolve_label(self.config.tv_label, tv, descriptions,
                              self._tv_properties(tv, descriptions.get(tv, ""), now))
        ok = self._combos.update(pads, tv, names, tv_label=label)
        self._combos_dirty = not ok
        if not ok:
            self._next_observe = now + 2.0

    def _tv_properties(self, tv: Optional[str], description: str,
                       now: float) -> Optional[Dict[str, Dict[str, str]]]:
        """Properties (``node.nick``...) of the main output, for labelling it.

        One ``pactl list sinks`` per rebuild cycle: it is read again only when the
        main output or its description changes, and otherwise every
        ``LABEL_REFRESH`` seconds (a different TV on the same port keeps the
        same sink name).  Not read at all when ``tv_label`` is configured.
        """
        if not tv or self.config.tv_label or not hasattr(self._sinks, "sink_details"):
            return None
        cache = self._tv_cache
        if cache is not None and cache[0] == tv and cache[1] == description \
                and now - cache[2] < LABEL_REFRESH:
            return {tv: cache[3]}
        props: Dict[str, str] = cache[3] if cache is not None and cache[0] == tv else {}
        try:
            details = self._sinks.sink_details().get(tv)
            props = dict(details.properties) if details else {}
        except Exception as exc:
            log.debug("cannot read the properties of %s: %s", tv, exc)
        self._tv_cache = (tv, description, now, props)
        return {tv: props}

    def _choose_tv(self, names: Sequence[str], default: Optional[str]) -> Optional[str]:
        """The main output sink: the configured one, else the first non-ours default seen."""
        prefix = self.config.sink_name_prefix
        configured = self.config.tv_sink
        if configured:
            if configured in names and not is_ours(configured, prefix):
                return configured
            if not self._tv_warned_missing:
                self._tv_warned_missing = True
                log.warning("the configured tv_sink %r does not exist (or is one of ours); "
                            "the combined outputs with the main output are skipped", configured)
            return None
        self._tv_warned_missing = False
        if self._tv_sink is not None and self._tv_sink in names:
            return self._tv_sink
        if self._tv_sink is not None:
            log.info("the main output %s disappeared", self._tv_sink)
        self._tv_sink = None
        if default and default in names and not is_ours(default, prefix) \
                and not default.endswith(".monitor"):
            self._tv_sink = default
            log.info("using %s as the main output for the combined outputs", default)
        return self._tv_sink

    def _default_streamer(self, pad: Pad, sink_name: str,
                          fallback: Callable[[Optional[str]], None]) -> PadStreamer:
        config = StreamerConfig(jitter_reports=self.config.jitter_reports,
                                speaker_volume_max=self.config.speaker_volume_max,
                                keep_lightbar=self.config.keep_lightbar,
                                start_on_running=self.config.start_on_running,
                                disable_sniff=self.config.disable_sniff,
                                disconnect_on_hid_loss=self.config.disconnect_on_hid_loss,
                                lightbar_fallback=tuple(self.config.lightbar_fallback))
        return PadStreamer(pad, sink_name, self._sinks, config=config, fallback=fallback,
                           runtime_dir=self.config.runtime_dir, link=self._link,
                           combo_lookup=self._combos.slaves_of)

    # -- status -------------------------------------------------------------------

    def _write_status(self) -> None:
        now = self._clock()
        self._next_status = now + STATUS_INTERVAL
        path = self._status_path
        if not path:
            return
        pads = []
        for slot in self._slots.values():
            streamer = None
            if slot.streamer is not None:
                try:
                    streamer = slot.streamer.snapshot()
                except Exception as exc:
                    streamer = {"error": str(exc)}
            pads.append({
                "mac": slot.pad.mac,
                "name": slot.pad.name,
                "hidraw": slot.pad.hidraw_path,
                "sink": slot.sink_name,
                "sink_ready": slot.ready,
                "sink_error": slot.error,
                "streamer": streamer,
            })
        data = {
            "version": __version__,
            "pid": os.getpid(),
            "state": "running",
            "started_at": self._started_wall,
            "updated_at": self._wall(),
            "pads": pads,
            "tv_sink": self.config.tv_sink or self._tv_sink,
            "combos": self._combos.snapshot(),
            "ignored": list(self._ignored),
        }
        try:
            write_status_file(path, data)
            self._status_failed = False
        except OSError as exc:
            if not self._status_failed:
                log.warning("cannot write the status file %s: %s", path, exc.strerror or exc)
            self._status_failed = True

    # -- signals ------------------------------------------------------------------

    def _install_signals(self) -> Dict[int, Any]:
        previous: Dict[int, Any] = {}
        if threading.current_thread() is not threading.main_thread():
            return previous

        def handler(signum: int, frame: Any) -> None:
            log.info("received signal %d, stopping", signum)
            self.request_stop()

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, handler)
        return previous
