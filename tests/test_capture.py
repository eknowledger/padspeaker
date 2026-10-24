"""Tests for padspeaker.capture with a fake Popen and one real child process."""
import os
import stat
import subprocess
import sys
import threading
import time

import pytest

from padspeaker.capture import Capture, CaptureEnded, CaptureError


class FakeProc:
    """Minimal Popen stand-in whose stdout is a real pipe."""

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        r, w = os.pipe()
        self.stdout = os.fdopen(r, "rb", buffering=0)
        self._w = w
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.waited = False
        self.ignore_terminate = False

    # test helpers
    def feed(self, data):
        os.write(self._w, data)

    def die(self, code=1):
        self.returncode = code
        if self._w is not None:
            os.close(self._w)
            self._w = None

    # Popen API
    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.ignore_terminate:
            self.die(-15)

    def kill(self):
        self.killed = True
        self.die(-9)

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.waited = True
        return self.returncode


class Factory:
    def __init__(self):
        self.procs = []

    def __call__(self, argv, **kwargs):
        proc = FakeProc(argv, **kwargs)
        self.procs.append(proc)
        return proc


def test_start_builds_argv_and_env():
    f = Factory()
    cap = Capture("padspeaker_1122", runtime_dir="/var/run", popen=f)
    cap.start()
    proc = f.procs[0]
    assert proc.argv == ["parec", "--device=padspeaker_1122.monitor", "--format=s16le",
                         "--rate=32000", "--channels=2", "--latency-msec=16"]
    assert proc.kwargs["stdout"] == subprocess.PIPE
    assert proc.kwargs["env"]["XDG_RUNTIME_DIR"] == "/var/run"
    assert "shell" not in proc.kwargs or not proc.kwargs["shell"]
    cap.close()


def test_parec_command_is_injectable():
    f = Factory()
    cap = Capture("s", parec="/opt/parec", popen=f)
    cap.start()
    assert f.procs[0].argv[0] == "/opt/parec"
    cap.close()


def test_read_returns_exact_chunk():
    f = Factory()
    with Capture("s", popen=f) as cap:
        f.procs[0].feed(b"a" * 1500)
        assert cap.read(512, timeout=1) == b"a" * 512
        assert cap.read(512, timeout=1) == b"a" * 512
        assert cap.read(512, timeout=0.05) == b""      # only 476 left


def test_partial_data_is_kept_until_complete():
    f = Factory()
    with Capture("s", popen=f) as cap:
        f.procs[0].feed(b"ab")
        assert cap.read(4, timeout=0.05) == b""
        f.procs[0].feed(b"cd")
        assert cap.read(4, timeout=1) == b"abcd"


def test_read_timeout_returns_empty_quickly():
    f = Factory()
    with Capture("s", popen=f) as cap:
        t0 = time.monotonic()
        assert cap.read(512, timeout=0.05) == b""
        assert time.monotonic() - t0 < 1


def test_read_waits_for_late_data():
    f = Factory()
    with Capture("s", popen=f) as cap:
        threading.Timer(0.05, lambda: f.procs[0].feed(b"z" * 8)).start()
        assert cap.read(8, timeout=2) == b"z" * 8


def test_read_raises_when_process_dies():
    f = Factory()
    with Capture("s", popen=f) as cap:
        f.procs[0].die(3)
        with pytest.raises(CaptureEnded) as info:
            cap.read(512, timeout=1)
        assert "3" in str(info.value)


def test_buffered_data_is_delivered_before_end():
    f = Factory()
    with Capture("s", popen=f) as cap:
        f.procs[0].feed(b"q" * 512)
        f.procs[0].die(0)
        assert cap.read(512, timeout=1) == b"q" * 512
        with pytest.raises(CaptureEnded):
            cap.read(512, timeout=1)


def test_read_before_start_or_after_close_raises():
    f = Factory()
    cap = Capture("s", popen=f)
    with pytest.raises(CaptureError):
        cap.read(1)
    cap.start()
    cap.close()
    with pytest.raises(CaptureError):
        cap.read(1)


def test_close_terminates_and_reaps():
    f = Factory()
    cap = Capture("s", popen=f)
    cap.start()
    proc = f.procs[0]
    cap.close()
    assert proc.terminated and proc.waited and not proc.killed
    assert proc.stdout.closed
    cap.close()                                       # idempotent


def test_close_kills_when_terminate_is_ignored():
    f = Factory()
    cap = Capture("s", popen=f, terminate_timeout=0.01)
    cap.start()
    proc = f.procs[0]
    proc.ignore_terminate = True
    cap.close()
    assert proc.terminated and proc.killed and proc.waited


def test_close_after_process_already_dead_still_reaps():
    f = Factory()
    cap = Capture("s", popen=f)
    cap.start()
    proc = f.procs[0]
    proc.die(1)
    cap.close()
    assert proc.waited and not proc.terminated


def test_context_manager_closes_on_exception():
    f = Factory()
    with pytest.raises(ValueError):
        with Capture("s", popen=f):
            raise ValueError
    assert f.procs[0].terminated


def test_start_failure_is_capture_error():
    def boom(argv, **kw):
        raise FileNotFoundError(2, "no parec")
    with pytest.raises(CaptureError, match="parec"):
        Capture("s", popen=boom).start()


def test_start_twice_is_an_error():
    f = Factory()
    cap = Capture("s", popen=f)
    cap.start()
    with pytest.raises(CaptureError):
        cap.start()
    cap.close()


def _script(tmp_path, body):
    path = tmp_path / "fake_parec"
    path.write_text("#!%s\nimport sys, time\n%s\n" % (sys.executable, body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_real_process_streams_and_is_reaped(tmp_path):
    script = _script(tmp_path, "while True:\n"
                               "    sys.stdout.buffer.write(b'\\x01' * 512)\n"
                               "    sys.stdout.buffer.flush()\n"
                               "    time.sleep(0.01)")
    cap = Capture("s", parec=script)
    cap.start()
    proc = cap._proc
    assert cap.read(512, timeout=5) == b"\x01" * 512
    cap.close()
    assert proc.returncode is not None               # reaped, no zombie


def test_real_process_exit_raises_capture_ended(tmp_path):
    script = _script(tmp_path, "sys.exit(7)")
    with Capture("s", parec=script) as cap:
        with pytest.raises(CaptureEnded, match="7"):
            cap.read(512, timeout=5)
