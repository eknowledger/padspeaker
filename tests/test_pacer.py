"""Tests for padspeaker.pacer: the jitter buffer and the report clock."""
import pytest

from padspeaker.pacer import Pacer, ReportClock

REPORT = 2048  # 4 frames x 128 samples x 4 bytes


def ramp(n_bytes, start=0):
    """n_bytes of a repeating byte pattern, so ordering mistakes are visible."""
    return bytes((start + i) % 251 for i in range(n_bytes))


def reports(n, start=0):
    return ramp(n * REPORT, start)


# --- construction ------------------------------------------------------------

def test_defaults_and_report_size():
    pacer = Pacer()
    assert pacer.report_bytes == 2048
    assert pacer.stats.buffered_bytes == 0


@pytest.mark.parametrize("kwargs", [
    dict(frames_per_report=0),
    dict(target_reports=0),
    dict(target_reports=5, max_reports=4),
    dict(max_reports=0),
])
def test_invalid_configuration_rejected(kwargs):
    with pytest.raises(ValueError):
        Pacer(**kwargs)


def test_custom_frames_per_report_changes_report_size():
    pacer = Pacer(frames_per_report=2)
    assert pacer.report_bytes == 1024


# --- prefill -----------------------------------------------------------------

def test_no_report_until_target_reports_buffered():
    pacer = Pacer(target_reports=3)
    pacer.push_pcm(reports(2))
    assert pacer.pop_report_pcm() is None
    assert pacer.stats.underruns == 0  # prefilling is not an underrun
    pacer.push_pcm(reports(1, start=7))
    first = pacer.pop_report_pcm()
    assert first == reports(2)[:REPORT]


def test_prefill_counts_whole_reports_not_partial_data():
    pacer = Pacer(target_reports=2)
    pacer.push_pcm(ramp(2 * REPORT - 4))
    assert pacer.pop_report_pcm() is None
    pacer.push_pcm(ramp(4))
    assert pacer.pop_report_pcm() is not None


def test_after_start_no_new_prefill_is_needed():
    pacer = Pacer(target_reports=3)
    pacer.push_pcm(reports(3))
    assert pacer.pop_report_pcm() is not None
    pacer.push_pcm(reports(1))  # only 3 buffered again after this: 2 + 1
    assert pacer.pop_report_pcm() is not None
    assert pacer.pop_report_pcm() is not None
    assert pacer.pop_report_pcm() is not None
    assert pacer.stats.underruns == 0


# --- ordering and chunking ---------------------------------------------------

def _drain(pacer, count):
    return b"".join(pacer.pop_report_pcm() for _ in range(count))


@pytest.mark.parametrize("chunk", [1, 2, 3, 4, 5, 7, 255, 511, 512, 513, 2047, 2048, 2049, 5000])
def test_arbitrary_chunk_sizes_preserve_the_stream(chunk):
    pacer = Pacer(target_reports=3, max_reports=8)
    data = ramp(5 * REPORT)
    for i in range(0, len(data), chunk):
        pacer.push_pcm(data[i:i + chunk])
        # keep the buffer under max so nothing is dropped in this test
        assert pacer.stats.overruns == 0 or chunk > REPORT * 8
    assert _drain(pacer, 5) == data
    assert pacer.stats.underruns == 0


def test_one_byte_chunks():
    pacer = Pacer(target_reports=1)
    data = ramp(REPORT)
    for b in data:
        pacer.push_pcm(bytes([b]))
    assert pacer.pop_report_pcm() == data


def test_huge_chunk_is_split_into_reports():
    pacer = Pacer(target_reports=3, max_reports=8)
    data = ramp(8 * REPORT)
    pacer.push_pcm(data)
    assert pacer.stats.overruns == 0
    assert _drain(pacer, 8) == data


def test_every_report_is_exactly_2048_bytes():
    pacer = Pacer(target_reports=1)
    pacer.push_pcm(ramp(REPORT + 1234))
    for _ in range(5):  # includes underruns
        out = pacer.pop_report_pcm()
        assert len(out) == REPORT


def test_empty_push_is_a_noop():
    pacer = Pacer()
    pacer.push_pcm(b"")
    assert pacer.stats.buffered_bytes == 0


def test_accepts_bytearray_and_memoryview():
    pacer = Pacer(target_reports=1)
    pacer.push_pcm(bytearray(ramp(1000)))
    pacer.push_pcm(memoryview(ramp(1048, start=1000)))
    assert pacer.pop_report_pcm() == ramp(REPORT)


def test_push_rejects_non_bytes():
    with pytest.raises(TypeError):
        Pacer().push_pcm("abc")  # type: ignore[arg-type]


def test_pushed_data_is_copied_not_aliased():
    pacer = Pacer(target_reports=1)
    src = bytearray(ramp(REPORT))
    pacer.push_pcm(src)
    src[:] = b"\xff" * REPORT
    assert pacer.pop_report_pcm() == ramp(REPORT)


# --- underrun ----------------------------------------------------------------

def test_underrun_returns_silence_and_is_counted():
    pacer = Pacer(target_reports=1)
    pacer.push_pcm(reports(1))
    assert pacer.pop_report_pcm() == reports(1)
    silence = pacer.pop_report_pcm()
    assert silence == b"\x00" * REPORT
    assert pacer.stats.underruns == 1
    pacer.pop_report_pcm()
    assert pacer.stats.underruns == 2


def test_underrun_never_replays_old_audio():
    pacer = Pacer(target_reports=1)
    pacer.push_pcm(b"\x55" * REPORT)
    assert pacer.pop_report_pcm() == b"\x55" * REPORT
    for _ in range(3):
        assert pacer.pop_report_pcm() == b"\x00" * REPORT


def test_partial_data_at_underrun_is_kept_for_the_next_report():
    pacer = Pacer(target_reports=1)
    pacer.push_pcm(reports(1))
    pacer.pop_report_pcm()
    pacer.push_pcm(ramp(1000))
    assert pacer.pop_report_pcm() == b"\x00" * REPORT  # not a full report yet
    assert pacer.stats.underruns == 1
    assert pacer.stats.buffered_bytes == 1000
    pacer.push_pcm(ramp(1048, start=1000))
    assert pacer.pop_report_pcm() == ramp(REPORT)


def test_recovers_after_underrun_without_extra_prefill():
    pacer = Pacer(target_reports=3)
    pacer.push_pcm(reports(3))
    _drain(pacer, 3)
    assert pacer.pop_report_pcm() == b"\x00" * REPORT
    pacer.push_pcm(reports(1, start=9))
    assert pacer.pop_report_pcm() == reports(1, start=9)


# --- overrun -----------------------------------------------------------------

def test_overrun_drops_oldest_and_keeps_newest():
    pacer = Pacer(target_reports=1, max_reports=3)
    data = reports(5)
    pacer.push_pcm(data)
    assert pacer.stats.overruns == 1
    assert pacer.stats.dropped_bytes == 2 * REPORT
    assert pacer.stats.buffered_bytes == 3 * REPORT
    assert _drain(pacer, 3) == data[2 * REPORT:]


def test_overrun_incremental_pushes():
    pacer = Pacer(target_reports=1, max_reports=2)
    pacer.push_pcm(reports(2))
    assert pacer.stats.overruns == 0
    newest = ramp(REPORT, start=100)
    pacer.push_pcm(newest)
    assert pacer.stats.overruns == 1
    assert pacer.stats.dropped_bytes == REPORT
    out = _drain(pacer, 2)
    assert out == reports(2)[REPORT:] + newest


def test_overrun_keeps_stereo_sample_alignment():
    pacer = Pacer(target_reports=1, max_reports=2)
    original = ramp(2 * REPORT + 4)
    pacer.push_pcm(original[:2 * REPORT + 3])   # 3 stray bytes of a sample
    # 3 bytes too many: the drop is rounded up to a whole stereo sample
    assert pacer.stats.dropped_bytes == 4
    pacer.push_pcm(original[2 * REPORT + 3:])
    assert pacer.stats.overruns == 1
    out = pacer.pop_report_pcm()
    assert out == original[4:4 + REPORT]


def test_no_overrun_exactly_at_max():
    pacer = Pacer(target_reports=1, max_reports=3)
    pacer.push_pcm(reports(3))
    assert pacer.stats.overruns == 0


# --- alignment ---------------------------------------------------------------

def test_partial_sample_waits_for_its_remaining_bytes():
    pacer = Pacer(target_reports=1)
    data = ramp(REPORT + 2)
    pacer.push_pcm(data)
    assert pacer.pop_report_pcm() == data[:REPORT]
    assert pacer.stats.buffered_bytes == 2


def test_buffered_ms_counts_whole_samples():
    pacer = Pacer()
    pacer.push_pcm(ramp(REPORT))
    assert pacer.stats.buffered_ms == pytest.approx(16.0)
    pacer.push_pcm(ramp(3))  # partial sample does not add time
    assert pacer.stats.buffered_ms == pytest.approx(16.0)


# --- stats and reset ---------------------------------------------------------

def test_stats_reports_progress():
    pacer = Pacer(target_reports=2)
    assert pacer.stats.started is False
    pacer.push_pcm(reports(2))
    assert pacer.stats.buffered_ms == pytest.approx(32.0)
    pacer.pop_report_pcm()
    s = pacer.stats
    assert s.started is True
    assert s.buffered_ms == pytest.approx(16.0)
    assert s.reports_out == 1


def test_stats_is_a_snapshot():
    pacer = Pacer(target_reports=1)
    snap = pacer.stats
    pacer.push_pcm(reports(1))
    assert snap.buffered_bytes == 0


def test_reset_clears_buffer_counters_and_prefill():
    pacer = Pacer(target_reports=2, max_reports=3)
    pacer.push_pcm(reports(5))
    pacer.pop_report_pcm()
    _drain(pacer, 2)
    pacer.pop_report_pcm()  # underrun
    pacer.reset()
    s = pacer.stats
    assert (s.underruns, s.overruns, s.dropped_bytes, s.buffered_bytes, s.reports_out) == (0, 0, 0, 0, 0)
    assert s.started is False
    pacer.push_pcm(reports(1))
    assert pacer.pop_report_pcm() is None  # prefilling again


# --- ReportClock -------------------------------------------------------------

class FakeTime:
    """A controllable clock; sleep() advances it, optionally oversleeping."""

    def __init__(self, start=1000.0, oversleep=0.0):
        self.now = start
        self.oversleep = oversleep
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds + self.oversleep


def make_clock(ft, **kwargs):
    return ReportClock(clock=ft.clock, sleep=ft.sleep, **kwargs)


def test_first_report_is_due_immediately():
    ft = FakeTime()
    rc = make_clock(ft)
    rc.wait_next()
    assert ft.sleeps == []
    assert ft.now == 1000.0


def test_reports_are_scheduled_every_16_ms_from_t0():
    ft = FakeTime()
    rc = make_clock(ft)
    times = []
    for _ in range(6):
        rc.wait_next()
        times.append(round(ft.now - 1000.0, 6))
    assert times == [0.0, 0.016, 0.032, 0.048, 0.064, 0.08]


def test_work_time_is_subtracted_from_the_sleep():
    ft = FakeTime()
    rc = make_clock(ft)
    rc.wait_next()
    ft.now += 0.010  # 10 ms of work
    rc.wait_next()
    assert ft.sleeps == [pytest.approx(0.006)]


def test_no_drift_with_oversleep():
    ft = FakeTime(oversleep=0.002)
    rc = make_clock(ft)
    for _ in range(100):
        rc.wait_next()
    # absolute deadlines: after 100 waits we are at most one oversleep past 99 * 16 ms
    assert ft.now - 1000.0 < 99 * 0.016 + 0.004
    assert rc.resyncs == 0


def test_small_lag_catches_up_without_sleeping():
    ft = FakeTime()
    rc = make_clock(ft)
    rc.wait_next()
    ft.now += 0.060  # 60 ms stall: report 1 due at 16 ms, so 44 ms late
    lag = rc.wait_next()
    assert lag == pytest.approx(0.044)
    assert ft.sleeps == []
    assert rc.resyncs == 0
    # still catching up: reports 2 and 3 are overdue too, then it settles
    rc.wait_next()
    rc.wait_next()
    assert ft.sleeps == []
    rc.wait_next()  # report 4 is due at 64 ms, now = 60 ms
    assert ft.sleeps == [pytest.approx(0.004)]


def test_large_lag_resyncs_instead_of_bursting():
    ft = FakeTime()
    rc = make_clock(ft)
    rc.wait_next()
    ft.now += 0.500  # half a second stall
    lag = rc.wait_next()
    assert lag > 0.1
    assert rc.resyncs == 1
    assert ft.sleeps == []
    # after the resync the schedule restarts at "now": next report is 16 ms later
    t = ft.now
    rc.wait_next()
    assert ft.now == pytest.approx(t + 0.016)
    assert rc.resyncs == 1


def test_lag_just_inside_threshold_does_not_resync():
    ft = FakeTime()
    rc = make_clock(ft)
    rc.wait_next()
    ft.now += 0.016 + 0.099  # just inside max_lag
    rc.wait_next()
    assert rc.resyncs == 0


def test_custom_interval_and_max_lag():
    ft = FakeTime()
    rc = make_clock(ft, interval=0.020, max_lag=0.05)
    rc.wait_next()
    rc.wait_next()
    assert ft.now - 1000.0 == pytest.approx(0.020)
    ft.now += 0.2
    rc.wait_next()
    assert rc.resyncs == 1


def test_reset_restarts_the_schedule():
    ft = FakeTime()
    rc = make_clock(ft)
    rc.wait_next()
    rc.wait_next()
    ft.now += 5.0  # idle gap, e.g. the stream paused
    rc.reset()
    rc.wait_next()
    assert rc.resyncs == 0  # a deliberate reset is not a resync
    assert ft.sleeps == [pytest.approx(0.016)]  # only the first wait slept
    t = ft.now
    rc.wait_next()
    assert ft.now == pytest.approx(t + 0.016)


def test_invalid_clock_parameters_rejected():
    ft = FakeTime()
    with pytest.raises(ValueError):
        make_clock(ft, interval=0)
    with pytest.raises(ValueError):
        make_clock(ft, max_lag=-1)


def test_defaults_use_real_time_functions():
    import time
    rc = ReportClock()
    assert rc.interval == 0.016
    assert rc.max_lag == 0.1
    start = time.monotonic()
    rc.wait_next()
    rc.wait_next()
    assert 0.010 < time.monotonic() - start < 0.5


# --- ReportClock lead ----------------------------------------------------------

def test_lead_sends_the_first_reports_early_then_stays_ahead():
    ft = FakeTime()
    rc = make_clock(ft, lead=0.030)
    times = []
    for _ in range(6):
        rc.wait_next()
        times.append(round(ft.now - 1000.0, 6))
    # two reports go out at once, then one every 16 ms, 30 ms ahead of nominal
    assert times == [0.0, 0.0, 0.002, 0.018, 0.034, 0.05]


def test_lead_does_not_count_as_lateness_or_trigger_resync():
    ft = FakeTime()
    rc = make_clock(ft, lead=0.030)
    lags = [rc.wait_next() for _ in range(5)]
    assert lags == [0.0] * 5
    assert rc.resyncs == 0


def test_lead_is_restored_after_a_resync():
    ft = FakeTime()
    rc = make_clock(ft, lead=0.030)
    rc.wait_next()
    ft.now += 0.5
    rc.wait_next()
    assert rc.resyncs == 1
    start = ft.now
    rc.wait_next()
    rc.wait_next()
    assert ft.now == pytest.approx(start + 0.002)


def test_lead_must_be_non_negative_and_within_max_lag():
    ft = FakeTime()
    with pytest.raises(ValueError):
        make_clock(ft, lead=-0.001)
    with pytest.raises(ValueError):
        make_clock(ft, lead=0.2, max_lag=0.1)
