"""Jitter buffering and send scheduling for the 16 ms report stream.

Two small, independent, I/O-free pieces:

* :class:`Pacer` turns an irregular stream of PCM chunks (any size, s16le
  stereo at 32 kHz) into fixed 2048-byte blocks, one per audio report. It
  prefills before the first block, answers underruns with silence (old audio is
  never replayed) and answers overruns by dropping the oldest audio.
* :class:`ReportClock` tells the sender when each report is due: at
  ``t0 + n * 16 ms`` with absolute deadlines, so sleep jitter does not
  accumulate. After a long stall it resynchronises instead of sending a burst.

Neither class reads the system clock on its own; :class:`ReportClock` takes its
time and sleep functions as parameters so it can be tested with a fake clock.
"""
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .protocol import (
    FRAMES_PER_REPORT,
    PCM_BYTES_PER_FRAME,
    REPORT_SECONDS,
    SAMPLE_RATE,
)

__all__ = ["Pacer", "PacerStats", "ReportClock"]

_BYTES_PER_SAMPLE = 4  # 16 bit stereo


@dataclass(frozen=True)
class PacerStats:
    """Snapshot of a :class:`Pacer`'s counters."""

    underruns: int
    """Reports answered with silence because not enough audio was buffered."""
    overruns: int
    """Pushes that overflowed the buffer and caused the oldest audio to be dropped."""
    dropped_bytes: int
    """Total bytes of PCM discarded by overruns."""
    buffered_bytes: int
    """Bytes currently held, including any trailing partial sample."""
    buffered_ms: float
    """Duration of the whole stereo samples currently held, in milliseconds."""
    reports_out: int
    """Reports handed out so far, silence included."""
    started: bool
    """True once the initial prefill completed."""


class Pacer:
    """Jitter buffer between a PCM source and the 16 ms report ticks.

    Args:
        frames_per_report: SBC frames per report (4). One report consumes
            ``frames_per_report * 512`` bytes of PCM.
        target_reports: reports that must be buffered before the first one is
            released (the prefill, which sets the steady-state latency).
        max_reports: capacity; audio beyond this is dropped oldest-first.

    The buffer is a plain byte stream, so chunks of any size (even one byte) may
    be pushed. Bytes are only ever dropped in whole stereo samples (4 bytes), so
    left and right channels never swap.
    """

    def __init__(
        self,
        frames_per_report: int = FRAMES_PER_REPORT,
        target_reports: int = 3,
        max_reports: int = 8,
    ) -> None:
        if frames_per_report < 1:
            raise ValueError("frames_per_report must be at least 1")
        if target_reports < 1:
            raise ValueError("target_reports must be at least 1")
        if max_reports < target_reports:
            raise ValueError("max_reports must be at least target_reports")
        self.report_bytes = frames_per_report * PCM_BYTES_PER_FRAME
        self._target_bytes = target_reports * self.report_bytes
        self._max_bytes = max_reports * self.report_bytes
        self._buffer = bytearray()
        self._silence = bytes(self.report_bytes)
        self.reset()

    def reset(self) -> None:
        """Empty the buffer, clear all counters and require a new prefill."""
        self._buffer.clear()
        self._started = False
        self._underruns = 0
        self._overruns = 0
        self._dropped_bytes = 0
        self._reports_out = 0

    def push_pcm(self, data: bytes) -> None:
        """Append PCM (s16le stereo, 32 kHz) of any length to the buffer.

        If the buffer then exceeds capacity, the oldest samples are dropped and
        one overrun is counted.

        Raises:
            TypeError: if ``data`` is not bytes-like.
        """
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("PCM must be bytes-like, got %s" % type(data).__name__)
        if not len(data):
            return
        self._buffer += data
        excess = len(self._buffer) - self._max_bytes
        if excess > 0:
            drop = -(-excess // _BYTES_PER_SAMPLE) * _BYTES_PER_SAMPLE  # whole samples
            del self._buffer[:drop]
            self._overruns += 1
            self._dropped_bytes += drop

    def pop_report_pcm(self) -> Optional[bytes]:
        """Return the PCM for the next report, or None while prefilling.

        Before the first report, returns None until ``target_reports`` full
        reports are buffered. Afterwards it always returns exactly
        ``report_bytes`` bytes: real audio when a whole report is buffered,
        otherwise silence (counted as an underrun). A partial report stays in the
        buffer and is played once completed; audio is never repeated.
        """
        if not self._started:
            if len(self._buffer) < self._target_bytes:
                return None
            self._started = True
        self._reports_out += 1
        if len(self._buffer) >= self.report_bytes:
            out = bytes(self._buffer[:self.report_bytes])
            del self._buffer[:self.report_bytes]
            return out
        self._underruns += 1
        return self._silence

    @property
    def stats(self) -> PacerStats:
        """A snapshot of the counters and the current fill level."""
        buffered = len(self._buffer)
        return PacerStats(
            underruns=self._underruns,
            overruns=self._overruns,
            dropped_bytes=self._dropped_bytes,
            buffered_bytes=buffered,
            buffered_ms=(buffered // _BYTES_PER_SAMPLE) * 1000.0 / SAMPLE_RATE,
            reports_out=self._reports_out,
            started=self._started,
        )


class ReportClock:
    """Schedules report sends at ``t0 + n * interval``.

    Call :meth:`wait_next` once before sending each report. The first call
    returns immediately and fixes ``t0``. Deadlines are absolute, so a late
    wake-up is compensated by a shorter next sleep. If a report is more than
    ``max_lag`` seconds late (a stall, a suspended process) the schedule is
    restarted from the current time rather than sending a burst to catch up.

    Args:
        interval: seconds between reports (default 16 ms).
        max_lag: lateness beyond which the schedule is resynchronised.
        lead: seconds by which the whole schedule runs ahead of nominal time.
            The first reports then go out back to back and later ones stay
            ``lead`` early, which fills the receiver's own buffer (the
            proof-of-concept ran 30 ms ahead). Must not exceed ``max_lag``.
        clock: monotonic time function in seconds.
        sleep: sleep function in seconds.
    """

    def __init__(
        self,
        interval: float = REPORT_SECONDS,
        max_lag: float = 0.1,
        lead: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        if max_lag < 0:
            raise ValueError("max_lag must not be negative")
        if lead < 0 or lead > max_lag:
            raise ValueError("lead must be between 0 and max_lag")
        self.interval = interval
        self.max_lag = max_lag
        self.lead = lead
        self._clock = clock
        self._sleep = sleep
        self._t0: Optional[float] = None
        self._count = 0
        self.resyncs = 0
        """Number of times the schedule was restarted after a long stall."""

    def reset(self) -> None:
        """Forget the schedule; the next :meth:`wait_next` starts a new one."""
        self._t0 = None
        self._count = 0

    def wait_next(self) -> float:
        """Block until the next report is due and return how late it already was.

        Returns:
            Seconds past the nominal deadline (ignoring ``lead``) when called (0.0 if it had to sleep). When
            this exceeds ``max_lag`` the schedule was restarted and
            :attr:`resyncs` was incremented; the caller should send one report
            and carry on.
        """
        now = self._clock()
        if self._t0 is None:
            self._t0 = now
            self._count = 0
        nominal = self._t0 + self._count * self.interval
        lag = now - nominal
        if lag > self.max_lag:
            self.resyncs += 1
            self._t0 = now
            self._count = 0
        else:
            wait = nominal - self.lead - now
            if wait > 0:
                self._sleep(wait)
        self._count += 1
        return max(lag, 0.0)
