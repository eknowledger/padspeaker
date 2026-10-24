"""Stream one sink's audio to one pad: the idle / streaming state machine.

A :class:`PadStreamer` owns everything that happens for a single pad:

* **IDLE.** Nothing is open.  At most every 500 ms it asks the audio server for
  the default sink; when that is the pad's virtual sink, streaming starts.
* **STREAMING.** The pad's hidraw node is open for writing, ``<sink>.monitor`` is
  being captured, PCM goes through a jitter buffer (:class:`~padspeaker.pacer.Pacer`),
  is SBC encoded four frames at a time and sent as one audio report every 16 ms
  (:class:`~padspeaker.pacer.ReportClock`).  The sink volume is mirrored to the
  pad's speaker volume.  When the sink stops being the default, the pad
  disappears, a write fails or the capture ends, everything is closed again, the
  lightbar is restored and the streamer goes back to IDLE.

Threading model
---------------
The state machine itself is single threaded and driven by :meth:`PadStreamer.step`,
which does one small unit of work and returns how long the caller may wait before
calling it again.  All time comes from injected ``clock`` and ``sleep``
functions, so tests drive it with a simulated clock and no threads.

In production the daemon runs :meth:`PadStreamer.run` in one thread per pad.  A
thread per pad (rather than stepping every pad from the daemon loop) is used
because streaming has to hold a 16 ms rhythm using blocking sleeps and pipe
reads, while the daemon loop makes slow ``pactl`` calls, polls sysfs and writes
the status file; sharing one thread would let either stall the other.  Only the
pad that is the default sink streams, so in practice there is one busy thread.
A lock serialises :meth:`step` and :meth:`shutdown`, and :meth:`snapshot` only
reads plain numbers, so the daemon can show status from its own thread.

The periodic ``pactl`` queries run in the streaming thread and may take a few
tens of milliseconds.  That is absorbed: ``parec`` keeps buffering in its pipe,
the report clock catches up without bursting (it resynchronises after 100 ms)
and the jitter buffer drops the oldest audio if it overflows.

Volume
------
:func:`speaker_volume` maps the sink volume to the pad's ``0..0x4F`` range.
"""
from __future__ import annotations

import errno
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .capture import Capture
from .leds import LedState, read_led_state, restore_led_state
from .pacer import Pacer, ReportClock
from .pads import Pad
from .protocol import (
    FRAMES_PER_REPORT,
    MAX_SPEAKER_VOLUME,
    PCM_BYTES_PER_FRAME,
    REPORT_SECONDS,
    FrameCounter,
    build_audio_report,
    build_lightbar_report,
    build_volume_report,
)
from .sbc import SbcEncoder
from .sinks import SinkError

__all__ = [
    "IDLE",
    "STREAMING",
    "PAD_GONE_ERRNOS",
    "HID_LOST_ERRNOS",
    "StreamerConfig",
    "StreamerStats",
    "PadStreamer",
    "HidrawDevice",
    "open_hidraw",
    "speaker_volume",
    "is_silent",
]

log = logging.getLogger("padspeaker.streamer")

IDLE = "idle"
STREAMING = "streaming"

PAD_GONE_ERRNOS = frozenset({errno.EIO, errno.ENODEV, errno.ENOTCONN, errno.EBADF})
"""``write()`` errors that mean the pad (or its hidraw node) is gone."""

HID_LOST_ERRNOS = frozenset({errno.ENODEV, errno.EIO})
"""``write()`` errors that mean the HID device vanished while the Bluetooth link may be up."""

VOLUME_GAMMA = 0.6
"""Exponent of the volume curve, see :func:`speaker_volume`."""

_READ_CHUNK = PCM_BYTES_PER_FRAME      # 512 bytes: 4 ms, fine grained for the jitter buffer
_MAX_READS_PER_FILL = 128              # about half a second of audio per drain


@dataclass
class StreamerConfig:
    """Tunables of a :class:`PadStreamer` (the defaults are the tested values)."""

    jitter_reports: int = 3
    """Reports kept in the jitter buffer in steady state (3 x 16 ms = 48 ms)."""
    lead_reports: int = 2
    """Reports sent ahead of real time, filling the pad's own buffer.

    The hardware-verified proof of concept ran about 30 ms ahead; a schedule
    that never leads leaves the pad with no cushion against Bluetooth jitter.
    Set to 0 for a strictly real-time schedule.  Adds ``lead_reports * 16 ms``
    of latency.
    """
    max_reports: int = 8
    """Jitter buffer capacity; older audio beyond this is dropped."""
    speaker_volume_max: int = MAX_SPEAKER_VOLUME
    """Cap for the pad speaker volume, 0 to 0x4F."""
    volume_gamma: float = VOLUME_GAMMA
    silence_seconds: float = 3.0
    """Seconds of silence after which audio reports stop being sent."""
    silence_threshold: int = 8
    """Peak sample value (of 32767) at or below which PCM counts as silence."""
    default_check_interval: float = 0.5
    volume_interval: float = 0.25
    backoff_min: float = 0.5
    backoff_max: float = 8.0
    stable_seconds: float = 5.0
    """A session this long resets the restart backoff when it ends in an error."""
    prefill_poll: float = 0.02
    """How long one capture read may block while the jitter buffer is filling."""
    keep_lightbar: bool = True
    """Re-send the lightbar colour while streaming.

    On the pads tested (DS4 v2 clones) audio reports alone switch the lightbar
    off; sending the colour again in a state report about twice a second keeps
    it on.
    """
    lightbar_fallback: Tuple[int, int, int] = (0, 0, 64)
    """Colour used when the pad's colour cannot be read from the kernel (blue)."""
    lightbar_interval: float = 0.5
    """Seconds between lightbar reports."""
    lightbar_reread_interval: float = 1.0
    """The kernel LED class is read again at most this often, to follow changes."""
    start_on_running: bool = True
    """Also start when the pad's sink is RUNNING, not only when it is the default.

    That is what happens when sound reaches the pad through a combined output.
    """
    stop_debounce: float = 2.0
    """Seconds the sink must be neither default nor RUNNING before streaming stops."""
    disable_sniff: bool = True
    """Keep the pad's Bluetooth link out of sniff mode (before starting, then periodically).

    A link in sniff mode made BlueZ fail to deliver HID messages about 14 s into
    streaming, after which the HID device vanished (see :mod:`padspeaker.linkpolicy`).
    """
    sniff_interval: float = 30.0
    """BlueZ can set the link policy again, so it is re-applied this often while streaming."""
    disconnect_on_hid_loss: bool = True
    """After a write fails with ENODEV or EIO, disconnect the pad cleanly through Bluetooth."""


@dataclass
class StreamerStats:
    """Counters shown by ``status``; all are totals since the streamer was created."""

    reports_sent: int = 0
    silent_reports_skipped: int = 0
    underruns: int = 0
    overruns: int = 0
    write_errors: int = 0
    starts: int = 0
    restarts: int = 0
    start_failures: int = 0
    clock_resyncs: int = 0
    volume_updates: int = 0
    lightbar_updates: int = 0
    last_error: Optional[str] = None


def speaker_volume(percent: Optional[int], muted: bool,
                   max_volume: int = MAX_SPEAKER_VOLUME,
                   gamma: float = VOLUME_GAMMA) -> int:
    """Map a sink volume to the pad speaker volume (``0..max_volume``).

    The curve is ``round(max_volume * (percent / 100) ** gamma)`` with
    ``gamma = 0.6``.  Perceived loudness is closer to logarithmic than linear
    in the control value, and the pad's tiny speaker is quiet in its lower
    range, so a power curve below 1 spends more of the slider on audible levels.
    Details:

    * a muted sink, ``percent`` of 0 or None gives 0;
    * any non-zero volume gives at least 1 (a low volume is never rounded to mute);
    * 100 % and above give ``max_volume`` (volumes above 100 % are not amplified);
    * ``max_volume`` is clamped to the pad's absolute limit of 0x4F.
    """
    cap = max(0, min(int(max_volume), MAX_SPEAKER_VOLUME))
    if muted or percent is None or percent <= 0 or cap == 0:
        return 0
    level = min(float(percent), 100.0) / 100.0
    return max(1, min(cap, int(round(cap * level ** gamma))))


def is_silent(pcm: bytes, threshold: int = 8) -> bool:
    """Return True if every s16le sample of ``pcm`` is within ``+-threshold``."""
    if pcm.count(0) == len(pcm):
        return True
    usable = len(pcm) // 2 * 2
    samples = struct.unpack("<%dh" % (usable // 2), pcm[:usable])
    return max(samples) <= threshold and min(samples) >= -threshold


class HidrawDevice:
    """A pad's ``/dev/hidrawN`` node opened write-only.

    Each :meth:`write` sends one complete report (report id and CRC included).
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._fd: Optional[int] = os.open(path, os.O_WRONLY)

    def write(self, data: bytes) -> int:
        """Write one report; raises :class:`OSError` on failure."""
        if self._fd is None:
            raise OSError(errno.EBADF, "device is closed")
        return os.write(self._fd, data)

    def close(self) -> None:
        """Close the node.  Safe to call more than once."""
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)


def open_hidraw(path: str) -> HidrawDevice:
    """Open ``path`` for writing (``O_WRONLY``)."""
    return HidrawDevice(path)


class _WriteFailed(Exception):
    """Internal: a write to the pad failed; ``cause`` is the :class:`OSError`."""

    def __init__(self, cause: OSError) -> None:
        super().__init__(str(cause))
        self.cause = cause


_QUERY_FAILED = object()


class PadStreamer:
    """Per-pad state machine; see the module docstring.

    Args:
        pad: the pad to stream to.
        sink_name: its virtual sink.
        sinks: object with ``default_sink()``, ``sink_volume(name)`` and
            ``sink_muted(name)`` (a :class:`~padspeaker.sinks.SinkManager`).
        config: tunables.
        fallback: called as ``fallback(previous_default)`` when the pad vanishes
            (or the daemon is shutting down) while its sink is the default, so
            the default can be moved back to a working output.  ``previous_default``
            is the default sink seen before the user picked this pad, or None.
        runtime_dir: ``XDG_RUNTIME_DIR`` for ``parec`` (default capture only).
        clock, sleep: monotonic time and sleep functions (injectable).
        open_pad: ``open_pad(path)`` returning an object with ``write(bytes)``
            and ``close()``.
        capture_factory: ``capture_factory(sink_name)`` returning an object with
            ``start()``, ``read(n, timeout)`` and ``close()``.
        encoder_factory: returns an object with ``encode(pcm)``, ``reset()``
            and ``close()``.
        read_leds, restore_leds: lightbar save and restore functions.
        link: object with ``disable_sniff(mac)`` and ``disconnect(mac)`` (a
            :class:`~padspeaker.linkpolicy.BluetoothLink`); None does nothing.
        combo_lookup: returns ``{combo sink name: slave sinks}`` for the combined
            outputs that exist; used to follow the volume of a combo that feeds
            this pad and to not mistake a combo for the user's previous output.
    """

    def __init__(
        self,
        pad: Pad,
        sink_name: str,
        sinks: Any,
        *,
        config: Optional[StreamerConfig] = None,
        fallback: Optional[Callable[[Optional[str]], None]] = None,
        runtime_dir: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        open_pad: Callable[[str], Any] = open_hidraw,
        capture_factory: Optional[Callable[[str], Any]] = None,
        encoder_factory: Callable[[], Any] = SbcEncoder,
        read_leds: Callable[[Pad], Optional[LedState]] = read_led_state,
        restore_leds: Callable[[Pad, LedState], bool] = restore_led_state,
        link: Optional[Any] = None,
        combo_lookup: Optional[Callable[[], Mapping[str, Sequence[str]]]] = None,
    ) -> None:
        self.pad = pad
        self.sink_name = sink_name
        self.config = config or StreamerConfig()
        self.stats = StreamerStats()
        self._sinks = sinks
        self._fallback = fallback
        self._clock = clock
        self._open_pad = open_pad
        self._capture_factory = capture_factory or (
            lambda sink: Capture(sink, runtime_dir=runtime_dir))
        self._encoder_factory = encoder_factory
        self._read_leds = read_leds
        self._restore_leds = restore_leds
        self._link = link
        self._combo_lookup = combo_lookup
        self._label = "%s (%s)" % (pad.mac, sink_name)

        prefill = self.config.jitter_reports + self.config.lead_reports
        self._pacer = Pacer(FRAMES_PER_REPORT, prefill, max(self.config.max_reports, prefill + 3))
        self._report_clock = ReportClock(lead=self.config.lead_reports * REPORT_SECONDS,
                                         clock=clock, sleep=sleep)
        self._prefill_bytes = prefill * self._pacer.report_bytes
        self._lock = threading.RLock()
        self._closed = False

        self._state = IDLE
        self._retry_at = 0.0
        self._backoff = self.config.backoff_min
        self._had_failure = False
        self._next_check = 0.0
        self._last_other_default: Optional[str] = None
        self._last_default: Optional[str] = None
        self._via: Optional[str] = None
        self._inactive_since: Optional[float] = None
        self._clear_session()

    # -- public -----------------------------------------------------------------

    @property
    def state(self) -> str:
        """``"idle"`` or ``"streaming"``."""
        return self._state

    def step(self) -> float:
        """Do one unit of work and return seconds the caller may wait before the next.

        While streaming, one call sleeps until the next report is due and sends
        it, so it returns 0.  While idle it returns the time until something
        could change.  Never raises: every error ends the session cleanly and is
        counted in :attr:`stats`.
        """
        with self._lock:
            if self._closed:
                return 1.0
            try:
                now = self._clock()
                if self._state == IDLE:
                    return self._step_idle(now)
                return self._step_streaming(now)
            except _WriteFailed as failure:
                self._on_write_error(failure.cause)
            except Exception as exc:  # defensive: a streamer must never kill the daemon
                if self._state == STREAMING:
                    log.exception("%s: unexpected error while streaming", self._label)
                    self._end_session("unexpected error: %s" % exc, error=True, text=str(exc))
                else:
                    log.exception("%s: unexpected error", self._label)
                    self.stats.last_error = str(exc)
                    self._schedule_retry(self._clock())
            return 0.0

    def run(self, stop: threading.Event) -> None:
        """Call :meth:`step` until ``stop`` is set (the body of a streamer thread)."""
        while not stop.is_set():
            delay = self.step()
            if delay > 0:
                stop.wait(min(delay, 0.25))

    def shutdown(self, restore_default: bool = False) -> None:
        """Stop streaming, restore the lightbar and make the streamer inert.

        Args:
            restore_default: if the pad's sink is still the default sink, ask
                the ``fallback`` callable to move the default away (used when
                the pad vanished or the daemon exits, so sound does not go
                silent).
        """
        with self._lock:
            was_streaming = self._state == STREAMING
            if was_streaming:
                self._end_session("shutdown", error=False)
            if restore_default and not self._closed:
                self._restore_default(assume_default=was_streaming)
            self._closed = True

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON friendly view of the state and counters."""
        stats = self.stats
        return {
            "state": self._state,
            "mac": self.pad.mac,
            "hidraw": self.pad.hidraw_path,
            "sink": self.sink_name,
            "silent": self._paused,
            "via": self._via,
            "volume": self._pad_volume,
            "volume_source": self._volume_source() if self._state == STREAMING else None,
            "stats": {
                "reports_sent": stats.reports_sent,
                "silent_reports_skipped": stats.silent_reports_skipped,
                "underruns": stats.underruns,
                "overruns": stats.overruns,
                "write_errors": stats.write_errors,
                "starts": stats.starts,
                "restarts": stats.restarts,
                "start_failures": stats.start_failures,
                "clock_resyncs": stats.clock_resyncs,
                "volume_updates": stats.volume_updates,
                "lightbar_updates": stats.lightbar_updates,
                "last_error": stats.last_error,
            },
        }

    # -- idle -------------------------------------------------------------------

    def _step_idle(self, now: float) -> float:
        interval = self.config.default_check_interval
        if now < self._retry_at:
            return min(self._retry_at - now, interval)
        if now < self._next_check:
            return self._next_check - now
        self._next_check = now + interval
        default = self._query_default()
        if default is _QUERY_FAILED:
            return interval
        self._last_default = default
        if default == self.sink_name:
            self._via = "default"
        else:
            self._remember_other(default)
            if self._sink_active(default) is not True:
                return interval
            self._via = "running"
        self._start(now)
        return 0.0 if self._state == STREAMING else interval

    def _combos(self) -> Mapping[str, Sequence[str]]:
        if self._combo_lookup is None:
            return {}
        try:
            return self._combo_lookup()
        except Exception as exc:
            log.debug("%s: combo lookup failed: %s", self._label, exc)
            return {}

    def _remember_other(self, default: Optional[str]) -> None:
        """Remember the default as the user's previous output, unless it is one of ours."""
        if default and default != self.sink_name and default not in self._combos():
            self._last_other_default = default

    def _sink_active(self, default: Optional[str]) -> Optional[bool]:
        """Is sound meant for this pad? True / False, or None when it cannot be told.

        Yes when the pad's sink is the default sink, or (``start_on_running``)
        when its state is RUNNING, which is what a combined output makes it.
        """
        if default == self.sink_name:
            return True
        if not self.config.start_on_running or not hasattr(self._sinks, "sink_state"):
            return False
        try:
            return self._sinks.sink_state(self.sink_name) == "RUNNING"
        except SinkError as exc:
            log.debug("%s: cannot read the sink state: %s", self._label, exc)
            return None

    def _query_default(self) -> Any:
        try:
            return self._sinks.default_sink()
        except SinkError as exc:
            log.debug("%s: cannot read the default sink: %s", self._label, exc)
            return _QUERY_FAILED

    # -- starting ---------------------------------------------------------------

    def _clear_session(self) -> None:
        self._handle: Any = None
        self._capture: Any = None
        self._encoder: Any = None
        self._saved_led: Optional[LedState] = None
        self._counter = FrameCounter()
        self._started_at = 0.0
        self._last_audible = 0.0
        self._paused = False
        self._pad_volume: Optional[int] = None
        self._next_volume_check = 0.0
        self._light_rgb: Tuple[int, int, int] = tuple(self.config.lightbar_fallback)  # type: ignore
        self._next_light_send = 0.0
        self._next_light_read = 0.0
        self._next_sniff = 0.0
        self._previous_default: Optional[str] = None
        self._inactive_since = None
        self._seen_underruns = 0
        self._seen_overruns = 0

    def _start(self, now: float) -> None:
        stats = self.stats
        stats.starts += 1
        if self._had_failure:
            stats.restarts += 1
        self._previous_default = self._last_other_default
        log.info("%s: starting to stream (previous default: %s)",
                 self._label, self._previous_default)
        self._saved_led = self._safe_read_leds()
        self._ensure_no_sniff()
        try:
            self._encoder = self._encoder_factory()
            self._handle = self._open_pad(self.pad.hidraw_path)
            self._capture = self._capture_factory(self.sink_name)
            self._capture.start()
            self._pad_volume = None
            self._next_volume_check = now
            self._update_volume(now)
        except _WriteFailed as failure:
            self._abort_start(now, failure.cause)
            self.stats.write_errors += 1
            self._after_pad_error(failure.cause)
            return
        except OSError as exc:
            self._abort_start(now, exc)
            if exc.errno in PAD_GONE_ERRNOS or exc.errno == errno.ENOENT:
                self._restore_default(assume_default=True)
            return
        except Exception as exc:
            self._abort_start(now, exc)
            return
        self._state = STREAMING
        self._had_failure = False
        self._started_at = now
        self._last_audible = now
        self._paused = False
        self._counter.reset()
        self._pacer_reset()
        self._report_clock.reset()
        self._next_check = now + self.config.default_check_interval
        saved = self._saved_led
        if saved is not None:
            self._light_rgb = (saved.r, saved.g, saved.b)
        else:
            self._light_rgb = tuple(self.config.lightbar_fallback)  # type: ignore
        self._next_light_send = now
        self._next_light_read = now + self.config.lightbar_reread_interval
        self._next_sniff = self._clock() + self.config.sniff_interval

    def _abort_start(self, now: float, exc: BaseException) -> None:
        self.stats.start_failures += 1
        self.stats.last_error = str(exc)
        log.warning("%s: cannot start streaming: %s", self._label, exc)
        self._release_resources()
        self._schedule_retry(now)

    # -- streaming --------------------------------------------------------------

    def _step_streaming(self, now: float) -> float:
        config = self.config
        if now >= self._next_check:
            self._next_check = now + config.default_check_interval
            default = self._query_default()
            if default is not _QUERY_FAILED:
                self._last_default = default
                self._remember_other(default)
                active = self._sink_active(default)
                if active is True:
                    self._inactive_since = None
                    self._via = "default" if default == self.sink_name else "running"
                elif active is False:
                    if self._inactive_since is None:
                        self._inactive_since = now
                    elif now - self._inactive_since >= config.stop_debounce:
                        log.info("%s: no sound is meant for this pad any more "
                                 "(default is %s), stopping", self._label, default)
                        self._end_session("sink no longer in use", error=False)
                        return 0.0
        if now >= self._next_volume_check:
            self._update_volume(now)
        if now >= self._next_sniff:
            self._next_sniff = now + self.config.sniff_interval
            self._ensure_no_sniff()

        pacer = self._pacer
        if not pacer.stats.started:
            self._fill(config.prefill_poll)
            if pacer.stats.buffered_bytes < self._prefill_bytes:
                self._keep_light()
                return 0.0
            self._report_clock.reset()
        self._report_clock.wait_next()
        self.stats.clock_resyncs = self._report_clock.resyncs
        self._fill(0.0)
        pcm = pacer.pop_report_pcm()
        if pcm is None:
            return 0.0

        was_paused = self._paused
        underran = self._account_pacer(was_paused)
        t = self._clock()
        if is_silent(pcm, config.silence_threshold):
            if not was_paused and t - self._last_audible > config.silence_seconds:
                log.info("%s: silence for %.0f s, pausing audio reports",
                         self._label, config.silence_seconds)
                self._paused = True
        else:
            self._last_audible = t
            if was_paused:
                log.info("%s: sound is back, resuming audio reports", self._label)
                self._paused = False
                self._encoder.reset()
        if self._paused:
            self.stats.silent_reports_skipped += 1
            if underran:
                # Nothing is arriving (the monitor may be suspended): drop the
                # empty buffer so the next sound is primed like a fresh start.
                self._pacer_reset()
            self._keep_light()
            return 0.0

        frames: List[bytes] = [
            self._encoder.encode(pcm[i * PCM_BYTES_PER_FRAME:(i + 1) * PCM_BYTES_PER_FRAME])
            for i in range(FRAMES_PER_REPORT)
        ]
        self._write(build_audio_report(self._counter.next(), frames))
        self.stats.reports_sent += 1
        self._keep_light()
        return 0.0

    def _keep_light(self) -> None:
        """Re-send the lightbar colour when due (called right after an audio report).

        Sending it in the slack after an audio report keeps it out of the way of
        the next deadline; the report clock uses absolute deadlines, so the time
        spent here is simply taken from the following sleep.
        """
        config = self.config
        if not config.keep_lightbar:
            return
        now = self._clock()
        if now < self._next_light_send:
            return
        if now >= self._next_light_read:
            self._next_light_read = now + config.lightbar_reread_interval
            state = self._safe_read_leds()
            if state is not None:
                self._light_rgb = (state.r, state.g, state.b)
        self._next_light_send = now + config.lightbar_interval
        self._write(build_lightbar_report(*self._light_rgb))
        self.stats.lightbar_updates += 1

    def _fill(self, first_timeout: float) -> None:
        """Move captured PCM into the jitter buffer; only the first read may block."""
        timeout = first_timeout
        for _ in range(_MAX_READS_PER_FILL):
            data = self._capture.read(_READ_CHUNK, timeout)
            if not data:
                return
            self._pacer.push_pcm(data)
            timeout = 0.0

    def _account_pacer(self, was_paused: bool) -> bool:
        """Fold the pacer's new underruns and overruns into :attr:`stats`.

        Underruns while paused are not counted (silence is expected then).
        Returns True when the last pop was an underrun.
        """
        snap = self._pacer.stats
        new_under = snap.underruns - self._seen_underruns
        new_over = snap.overruns - self._seen_overruns
        self._seen_underruns = snap.underruns
        self._seen_overruns = snap.overruns
        if not was_paused:
            self.stats.underruns += new_under
        self.stats.overruns += new_over
        return new_under > 0

    def _pacer_reset(self) -> None:
        self._account_pacer(True)
        self._pacer.reset()
        self._seen_underruns = 0
        self._seen_overruns = 0

    def _volume_source(self) -> str:
        """The sink whose volume and mute the pad speaker follows.

        The default sink when it is a combined output that includes this pad's
        sink (that is the control the user is turning, for example the volume
        keys); in every other case the pad's own sink.
        """
        default = self._last_default
        if default and default != self.sink_name:
            slaves = self._combos().get(default)
            if slaves and self.sink_name in slaves:
                return default
        return self.sink_name

    def _update_volume(self, now: float) -> None:
        self._next_volume_check = now + self.config.volume_interval
        source = self._volume_source()
        try:
            percent = self._sinks.sink_volume(source)
            muted = self._sinks.sink_muted(source)
        except SinkError as exc:
            log.debug("%s: cannot read the sink volume: %s", self._label, exc)
            return
        volume = speaker_volume(percent, muted, self.config.speaker_volume_max,
                                self.config.volume_gamma)
        if volume == self._pad_volume:
            return
        self._write(build_volume_report(volume))
        self._pad_volume = volume
        self.stats.volume_updates += 1
        log.debug("%s: speaker volume %d (sink %s%%%s)", self._label, volume,
                  percent, ", muted" if muted else "")

    def _write(self, report: bytes) -> None:
        try:
            written = self._handle.write(report)
        except OSError as exc:
            raise _WriteFailed(exc)
        if written is not None and written != len(report):
            raise _WriteFailed(OSError(errno.EIO, "short write (%d of %d bytes)"
                                       % (written, len(report))))

    # -- stopping ---------------------------------------------------------------

    def _on_write_error(self, exc: OSError) -> None:
        self.stats.write_errors += 1
        log.warning("%s: writing to the pad failed: %s", self._label, exc)
        self._end_session("write failed: %s" % exc, error=True, text=str(exc))
        self._after_pad_error(exc)

    def _after_pad_error(self, exc: OSError) -> None:
        if exc.errno in PAD_GONE_ERRNOS:
            self._restore_default(assume_default=True)
        if exc.errno in HID_LOST_ERRNOS and self.config.disconnect_on_hid_loss:
            self._disconnect_link()

    def _ensure_no_sniff(self) -> None:
        """Turn sniff mode off on the pad's link (never raises, logs only changes)."""
        if self._link is None or not self.config.disable_sniff:
            return
        try:
            self._link.disable_sniff(self.pad.mac)
        except Exception as exc:
            log.debug("%s: link policy call failed: %s", self._label, exc)

    def _disconnect_link(self) -> None:
        """The HID node is gone but the link may be up: disconnect the pad cleanly."""
        if self._link is None:
            return
        try:
            self._link.disconnect(self.pad.mac)
        except Exception as exc:
            log.warning("%s: Bluetooth disconnect failed: %s", self._label, exc)

    def _end_session(self, reason: str, error: bool, text: Optional[str] = None) -> None:
        """Close everything, restore the lightbar and return to IDLE."""
        log.info("%s: stopped (%s)", self._label, reason)
        now = self._clock()
        lasted = now - self._started_at
        self._pacer_reset()
        self._release_resources()
        self._report_clock.reset()
        self._state = IDLE
        self._paused = False
        if error:
            self.stats.last_error = text or reason
            if lasted >= self.config.stable_seconds:
                self._backoff = self.config.backoff_min
            self._schedule_retry(now)
        else:
            self._backoff = self.config.backoff_min
            self._retry_at = 0.0
            self._had_failure = False

    def _release_resources(self) -> None:
        """Close handle, capture and encoder, then restore the lightbar.

        Every step is guarded so that the lightbar restore always runs.
        """
        handle, self._handle = self._handle, None
        capture, self._capture = self._capture, None
        encoder, self._encoder = self._encoder, None
        saved, self._saved_led = self._saved_led, None
        try:
            for what, obj in (("pad", handle), ("capture", capture), ("encoder", encoder)):
                if obj is None:
                    continue
                try:
                    obj.close()
                except Exception as exc:
                    log.debug("%s: closing the %s failed: %s", self._label, what, exc)
        finally:
            if saved is not None:
                try:
                    self._restore_leds(self.pad, saved)
                except Exception as exc:
                    log.warning("%s: restoring the lightbar failed: %s", self._label, exc)

    def _schedule_retry(self, now: float) -> None:
        self._had_failure = True
        self._retry_at = now + self._backoff
        self._backoff = min(self._backoff * 2, self.config.backoff_max)

    def _safe_read_leds(self) -> Optional[LedState]:
        try:
            return self._read_leds(self.pad)
        except Exception as exc:
            log.debug("%s: cannot read the lightbar: %s", self._label, exc)
            return None

    def _restore_default(self, assume_default: bool) -> None:
        """Ask ``fallback`` to move the default away from this pad's sink.

        Only done when the sink really is the default (or, if the audio server
        cannot be asked, when ``assume_default`` says it probably is).
        """
        if self._fallback is None:
            return
        default = self._query_default()
        if default is _QUERY_FAILED:
            is_default = assume_default
        else:
            is_default = default == self.sink_name
        if not is_default:
            return
        previous = self._previous_default or self._last_other_default
        log.info("%s: restoring the default sink to %s", self._label, previous)
        try:
            self._fallback(previous)
        except Exception as exc:
            log.warning("%s: could not restore the default sink: %s", self._label, exc)
