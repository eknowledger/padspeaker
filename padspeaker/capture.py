"""Capture the audio of a sink's monitor source through ``parec``.

The streamer reads fixed-size PCM chunks (32 kHz, signed 16 bit little endian,
stereo) from this module.  ``parec`` is started as a child process with an
argument list (no shell) and its standard output is read through a pipe with
``select`` so :meth:`Capture.read` can time out instead of blocking forever.
"""
from __future__ import annotations

import os
import select
import subprocess
import time
from typing import Any, Callable, List, Optional

from .sinks import pulse_env

SAMPLE_RATE = 32000
CHANNELS = 2
LATENCY_MSEC = 16


class CaptureError(RuntimeError):
    """Raised when capture cannot be started or is used in the wrong state."""


class CaptureEnded(CaptureError):
    """Raised by :meth:`Capture.read` when the capture process has gone away."""


class Capture:
    """Read raw PCM from ``<sink>.monitor`` through a ``parec`` subprocess.

    Args:
        sink: Name of the sink whose monitor is recorded.
        runtime_dir: Value for ``XDG_RUNTIME_DIR`` in the child environment
            (None keeps the inherited one).
        parec: Name or path of the ``parec`` command.
        popen: Factory with the :class:`subprocess.Popen` call signature.
        terminate_timeout: Seconds to wait after SIGTERM before SIGKILL.

    Use as a context manager (``with Capture(...) as cap``) or call
    :meth:`start` and :meth:`close` explicitly.
    """

    def __init__(self, sink: str, runtime_dir: Optional[str] = None, parec: str = "parec",
                 popen: Callable[..., Any] = subprocess.Popen,
                 terminate_timeout: float = 2.0) -> None:
        self.sink = sink
        self._runtime_dir = runtime_dir
        self._parec = parec
        self._popen = popen
        self._terminate_timeout = terminate_timeout
        self._proc: Optional[Any] = None
        self._fd: Optional[int] = None
        self._buffer = bytearray()
        self._closed = False

    def argv(self) -> List[str]:
        """Return the command line that is run."""
        return [self._parec, "--device=%s.monitor" % self.sink, "--format=s16le",
                "--rate=%d" % SAMPLE_RATE, "--channels=%d" % CHANNELS,
                "--latency-msec=%d" % LATENCY_MSEC]

    def start(self) -> None:
        """Start ``parec``.  Raises :class:`CaptureError` if it cannot run."""
        if self._proc is not None or self._closed:
            raise CaptureError("capture already started")
        try:
            proc = self._popen(self.argv(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               stdin=subprocess.DEVNULL, bufsize=0,
                               env=pulse_env(self._runtime_dir))
        except OSError as exc:
            raise CaptureError("cannot run %s: %s" % (self._parec, exc))
        self._proc = proc
        self._fd = proc.stdout.fileno()

    def read(self, n_bytes: int, timeout: float = 0.1) -> bytes:
        """Return exactly ``n_bytes`` of PCM, or ``b''`` if they do not arrive in time.

        Partial data is kept for the next call, so chunks never lose bytes.

        Raises:
            CaptureEnded: the ``parec`` process exited (buffered data that
                completes a chunk is still delivered first).
            CaptureError: capture was not started or has been closed.
        """
        if self._proc is None or self._fd is None:
            raise CaptureError("capture is not running")
        deadline = time.monotonic() + max(0.0, timeout)
        while len(self._buffer) < n_bytes:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([self._fd], [], [], max(0.0, remaining))
            if not ready:
                return b""
            chunk = os.read(self._fd, max(4096, n_bytes - len(self._buffer)))
            if not chunk:
                raise CaptureEnded("%s exited with status %s" % (self._parec, self._reap()))
            self._buffer.extend(chunk)
        data = bytes(self._buffer[:n_bytes])
        del self._buffer[:n_bytes]
        return data

    def _reap(self) -> Any:
        """Collect the exit status of a process whose pipe reached EOF."""
        proc = self._proc
        try:
            return proc.wait(timeout=self._terminate_timeout)
        except subprocess.TimeoutExpired:
            return proc.poll()

    def close(self) -> None:
        """Stop ``parec`` and reap it.  Safe to call more than once."""
        proc, self._proc, self._fd = self._proc, None, None
        self._closed = True
        self._buffer.clear()
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=self._terminate_timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=self._terminate_timeout)
            else:
                proc.wait(timeout=self._terminate_timeout)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass

    def __enter__(self) -> "Capture":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
