"""Tests for padspeaker.streamer: the per-pad state machine against fakes.

Time is simulated: a fake clock whose ``sleep`` advances it, a fake capture that
produces PCM at the real rate of the simulated clock, a fake pad that records
every write with its timestamp.  No hardware, threads or pactl are involved.
"""
import errno
import struct
import threading
import time

import pytest

from padspeaker.capture import CaptureEnded
from padspeaker.leds import LedState
from padspeaker.pads import Pad
from padspeaker.protocol import (AUDIO_REPORT_LEN, MAX_SPEAKER_VOLUME, STATE_REPORT_LEN,
                                 is_audio_report, is_state_report, parse_audio_report)
from padspeaker.sinks import SinkError
from padspeaker.streamer import (IDLE, STREAMING, PadStreamer, StreamerConfig,
                                 is_silent, speaker_volume)

SINK = "padspeaker_1122"
OTHER = "alsa_output.hdmi"
BYTES_PER_SECOND = 32000 * 4


# --- fakes -------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


class FakeSinks:
    def __init__(self):
        self.default = OTHER
        self.volume = 100
        self.muted = False
        self.states = {}                  # sink name -> state string
        self.other_volumes = {}           # volumes of sinks other than the pad's own
        self.other_muted = {}
        self.default_calls = 0
        self.volume_calls = 0
        self.state_calls = 0
        self.fail_default = False
        self.fail_volume = False
        self.fail_state = False

    def default_sink(self):
        self.default_calls += 1
        if self.fail_default:
            raise SinkError("pactl exploded")
        return self.default

    def sink_state(self, name):
        self.state_calls += 1
        if self.fail_state:
            raise SinkError("no list")
        return self.states.get(name, "SUSPENDED")

    def sink_volume(self, name):
        self.volume_calls += 1
        if self.fail_volume:
            raise SinkError("no volume")
        return self.volume if name == SINK else self.other_volumes[name]

    def sink_muted(self, name):
        if self.fail_volume:
            raise SinkError("no mute")
        return self.muted if name == SINK else self.other_muted.get(name, False)


class FakePadDevice:
    def __init__(self, clock):
        self.clock = clock
        self.writes = []          # (time, bytes)
        self.closed = False
        self.fail_with = None     # OSError raised by the next write
        self.fail_after = None    # number of successful writes before failing
        self.short_write = False
        self.close_raises = False
        self.fail_on_lightbar = None   # OSError raised by lightbar-only reports
        self.state_cost = 0.0          # seconds a state report takes to write

    def write(self, data):
        if self.fail_with is not None and (self.fail_after is None
                                           or len(self.writes) >= self.fail_after):
            raise self.fail_with
        if is_state_report(data):
            if self.fail_on_lightbar is not None and not data[3] & 0x80:
                raise self.fail_on_lightbar
            self.clock.now += self.state_cost
        self.writes.append((self.clock.now, bytes(data)))
        return len(data) - 1 if self.short_write else len(data)

    def close(self):
        self.closed = True
        if self.close_raises:
            raise OSError(errno.EIO, "close failed")

    def audio(self):
        return [(t, d) for t, d in self.writes if is_audio_report(d)]

    def states(self):
        """Volume reports (state reports that carry the volume flag)."""
        return [(t, d) for t, d in self.writes if is_state_report(d) and d[3] & 0x80]

    def lights(self):
        """Lightbar reports (state reports with the lightbar flag only)."""
        return [(t, d) for t, d in self.writes if is_state_report(d) and d[3] == 0x02]


class FakeCapture:
    """Produces PCM in step with the fake clock, like a pipe fed by parec."""

    LOUD = struct.pack("<hh", 3000, -3000)

    def __init__(self, clock, sink):
        self.clock = clock
        self.sink = sink
        self.started = False
        self.closed = False
        self.audible = True
        self.stalled = False
        self.ended = False
        self.reads = 0
        self._avail = 0.0
        self._last = clock.now

    def start(self):
        self.started = True
        self._last = self.clock.now

    def _sync(self):
        elapsed = self.clock.now - self._last
        self._last = self.clock.now
        if not self.stalled:
            self._avail = min(self._avail + elapsed * BYTES_PER_SECOND, 65536.0)

    def read(self, n_bytes, timeout=0.1):
        self.reads += 1
        if self.ended:
            raise CaptureEnded("parec exited")
        self._sync()
        if self._avail < n_bytes and timeout > 0 and not self.stalled:
            wait = min(timeout, (n_bytes - self._avail) / BYTES_PER_SECOND + 1e-9)
            self.clock.now += wait
            self._sync()
        elif self._avail < n_bytes and timeout > 0:
            self.clock.now += timeout
            self._sync()
        if self._avail < n_bytes:
            return b""
        self._avail -= n_bytes
        if self.audible:
            return self.LOUD * (n_bytes // 4)
        return bytes(n_bytes)

    def close(self):
        self.closed = True


class FakeEncoder:
    instances = []

    def __init__(self):
        self.encoded = 0
        self.resets = 0
        self.closed = False
        self.fail = False
        FakeEncoder.instances.append(self)

    def encode(self, pcm):
        assert len(pcm) == 512
        if self.fail:
            raise RuntimeError("encoder blew up")
        self.encoded += 1
        return b"\x9c\x75\x19" + bytes([self.encoded & 0xFF]) * 109

    def reset(self):
        self.resets += 1

    def close(self):
        self.closed = True


class FakeLink:
    """Stands in for linkpolicy.BluetoothLink."""

    def __init__(self, world):
        self.world = world
        self.sniff_calls = []         # (time, mac)
        self.disconnects = []
        self.raises = False
        self.order = []

    def disable_sniff(self, mac):
        self.sniff_calls.append((self.world.clock.now, mac))
        self.order.append(("sniff", len(self.world.pads)))
        if self.raises:
            raise RuntimeError("hcitool exploded")

    def disconnect(self, mac):
        self.disconnects.append(mac)
        if self.raises:
            raise RuntimeError("bluetoothctl exploded")
        return True


class World:
    """Everything a PadStreamer needs, wired together, with run helpers."""

    def __init__(self, **config):
        self.clock = FakeClock()
        self.sinks = FakeSinks()
        self.pads = []            # every FakePadDevice opened
        self.captures = []
        self.capture_sinks = []
        self.open_error = None
        self.leds_saved = []
        self.leds_restored = []
        self.led_value = LedState(10, 20, 30)
        self.fallback_calls = []
        self.open_times = []
        FakeEncoder.instances = []
        self.pad = Pad("/dev/hidraw7", "aa:bb:cc:dd:11:22", "Wireless Controller",
                       "0005:054C:09CC", "/sys/leds")
        self.link = FakeLink(self)
        self.combos = {}                  # combo name -> slave sinks
        self.streamer = PadStreamer(
            self.pad, SINK, self.sinks, link=self.link,
            config=StreamerConfig(**config),
            fallback=self._fallback,
            clock=self.clock, sleep=self.clock.sleep,
            open_pad=self._open_pad,
            capture_factory=self._capture,
            encoder_factory=FakeEncoder,
            read_leds=self._read_leds,
            restore_leds=self._restore_leds,
            combo_lookup=lambda: dict(self.combos),
        )

    # factories
    def _open_pad(self, path):
        self.open_times.append(self.clock.now)
        assert path == "/dev/hidraw7"
        if self.open_error is not None:
            raise self.open_error
        dev = FakePadDevice(self.clock)
        self.pads.append(dev)
        return dev

    def _capture(self, sink):
        self.capture_sinks.append(sink)
        cap = FakeCapture(self.clock, sink)
        self.captures.append(cap)
        return cap

    def _read_leds(self, pad):
        self.leds_saved.append(pad)
        return self.led_value

    def _restore_leds(self, pad, state):
        self.leds_restored.append((pad, state))
        return True

    def _fallback(self, previous):
        # Like the real thing: the default moves away from the pad's sink.
        self.fallback_calls.append(previous)
        self.sinks.default = previous or OTHER

    # helpers
    @property
    def dev(self):
        return self.pads[-1]

    @property
    def cap(self):
        return self.captures[-1]

    def select_pad_sink(self):
        self.sinks.default = SINK

    def run(self, seconds):
        """Drive step() for ``seconds`` of simulated time, honouring its delay hint."""
        end = self.clock.now + seconds
        stuck = 0
        while self.clock.now < end:
            before = self.clock.now
            delay = self.streamer.step()
            if delay > 0:
                self.clock.now += delay
            if self.clock.now == before:
                stuck += 1
                assert stuck < 50, "step() made no progress: busy loop"
            else:
                stuck = 0


@pytest.fixture
def world():
    return World()


# --- volume curve and silence detection --------------------------------------

def test_volume_curve_endpoints_and_cap():
    assert speaker_volume(0, False) == 0
    assert speaker_volume(100, False) == MAX_SPEAKER_VOLUME
    assert speaker_volume(150, False) == MAX_SPEAKER_VOLUME   # no boost above 100 %
    assert speaker_volume(100, False, max_volume=40) == 40    # configurable cap
    assert speaker_volume(100, False, max_volume=500) == MAX_SPEAKER_VOLUME
    assert speaker_volume(50, True) == 0                      # mute
    assert speaker_volume(None, False) == 0


def test_volume_curve_is_monotonic_and_audible_above_zero():
    values = [speaker_volume(p, False) for p in range(0, 131)]
    assert values == sorted(values)
    assert values[1] >= 1           # 1 % is not rounded down to silence
    assert 0 < speaker_volume(50, False) < MAX_SPEAKER_VOLUME
    # gentle curve: the middle of the slider is louder than a linear mapping
    assert speaker_volume(50, False) > MAX_SPEAKER_VOLUME // 2


def test_is_silent():
    assert is_silent(bytes(2048), 8)
    assert is_silent(struct.pack("<hh", 5, -5) * 512, 8)
    assert not is_silent(struct.pack("<hh", 9, 0) * 512, 8)
    assert not is_silent(bytes(2046) + struct.pack("<h", -400), 8)
    assert is_silent(b"", 8)


# --- idle ----------------------------------------------------------------------

def test_idle_when_sink_is_not_default(world):
    world.run(3)
    assert world.streamer.state == IDLE
    assert world.pads == [] and world.captures == []
    assert world.leds_saved == []


def test_default_sink_is_checked_at_most_every_500ms(world):
    world.run(5)
    assert 8 <= world.sinks.default_calls <= 11


def test_idle_step_never_busy_loops(world):
    delays = [world.streamer.step() for _ in range(3)]
    assert all(d > 0 for d in delays[:1])


def test_sink_query_error_in_idle_is_survived(world):
    world.sinks.fail_default = True
    world.run(2)
    assert world.streamer.state == IDLE
    world.sinks.fail_default = False
    world.select_pad_sink()
    world.run(1)
    assert world.streamer.state == STREAMING


# --- start ---------------------------------------------------------------------

def test_start_sequence(world):
    world.select_pad_sink()
    world.run(1)
    s = world.streamer
    assert s.state == STREAMING
    assert world.capture_sinks == [SINK]            # capture() adds ".monitor" itself
    assert world.cap.started
    assert world.leds_saved[0] == world.pad
    # the first write is the volume report: sink at 100 % -> the cap
    first = world.dev.writes[0][1]
    assert is_state_report(first) and len(first) == STATE_REPORT_LEN
    assert first[3] & 0x80 and first[3 + 21] == MAX_SPEAKER_VOLUME
    assert s.stats.starts == 1 and s.stats.restarts == 0


def test_start_applies_sink_volume_and_mute(world):
    world.select_pad_sink()
    world.sinks.volume = 0
    world.run(0.6)
    assert world.dev.states()[0][1][3 + 21] == 0
    world2 = World()
    world2.select_pad_sink()
    world2.sinks.muted = True
    world2.run(0.6)
    assert world2.dev.states()[0][1][3 + 21] == 0


def test_start_with_unreadable_volume_still_streams_and_sets_it_later(world):
    world.select_pad_sink()
    world.sinks.fail_volume = True
    world.run(1)
    assert world.streamer.state == STREAMING
    assert world.dev.states() == []
    world.sinks.fail_volume = False
    world.sinks.volume = 60
    world.run(0.5)
    assert len(world.dev.states()) == 1


# --- streaming -----------------------------------------------------------------

def test_streams_paced_valid_reports(world):
    world.select_pad_sink()
    world.run(2)
    audio = world.dev.audio()
    assert 100 <= len(audio) <= 130            # ~62.5 per second, minus start-up
    times = [t for t, _ in audio]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps[0] == 0.0                       # lead: two reports go out back to back
    assert max(gaps) < 0.02 and min(gaps[2:]) > 0.012
    counters = []
    for _, data in audio:
        assert len(data) == AUDIO_REPORT_LEN
        parsed = parse_audio_report(data)
        assert parsed.crc_ok and parsed.target == 0x02
        assert all(f[:3] == b"\x9c\x75\x19" and len(f) == 112 for f in parsed.frames)
        counters.append(parsed.counter)
    assert counters[0] == 0
    assert all((b - a) % 0x10000 == 4 for a, b in zip(counters, counters[1:]))
    assert world.streamer.stats.reports_sent == len(audio)
    assert FakeEncoder.instances[-1].encoded == 4 * len(audio)


def test_prefill_delays_the_first_report(world):
    world.select_pad_sink()
    world.run(0.3)
    first_audio = world.dev.audio()[0][0]
    started = world.dev.writes[0][0]
    # 3 reports of jitter buffer plus 2 reports of lead are collected first
    assert 0.070 <= first_audio - started <= 0.10


def test_volume_follows_the_sink_and_only_on_change(world):
    world.select_pad_sink()
    world.run(1)
    n = len(world.dev.states())
    world.run(1)
    assert len(world.dev.states()) == n                 # unchanged -> nothing sent
    world.sinks.volume = 40
    changed_at = world.clock.now
    world.run(0.6)
    states = world.dev.states()
    assert len(states) == n + 1
    assert states[-1][1][3 + 21] == speaker_volume(40, False)
    assert states[-1][0] - changed_at <= 0.30           # picked up within ~250 ms


def test_volume_polling_is_rate_limited(world):
    world.select_pad_sink()
    world.run(1)
    calls = world.sinks.volume_calls
    world.run(2)
    assert world.sinks.volume_calls - calls <= 9        # <= 4 per second


def test_mute_sets_volume_zero_then_unmute_restores(world):
    world.select_pad_sink()
    world.run(1)
    world.sinks.muted = True
    world.run(0.5)
    assert world.dev.states()[-1][1][3 + 21] == 0
    world.sinks.muted = False
    world.run(0.5)
    assert world.dev.states()[-1][1][3 + 21] == MAX_SPEAKER_VOLUME


def test_volume_cap_from_config():
    w = World(speaker_volume_max=30)
    w.select_pad_sink()
    w.run(0.6)
    assert w.dev.states()[0][1][3 + 21] == 30


# --- silence -------------------------------------------------------------------

def test_silence_stops_audio_reports_but_keeps_capturing(world):
    world.select_pad_sink()
    world.run(1)
    world.cap.audible = False
    world.run(3.5)                                       # silence passes the 3 s mark
    count = len(world.dev.audio())
    reads = world.cap.reads
    world.run(2)
    assert len(world.dev.audio()) == count               # nothing sent while silent
    assert world.cap.reads > reads                       # but still draining the pipe
    assert world.streamer.state == STREAMING
    assert world.streamer.snapshot()["silent"] is True


def test_silence_shorter_than_three_seconds_keeps_sending(world):
    world.select_pad_sink()
    world.run(1)
    world.cap.audible = False
    world.run(2.5)
    sent = len(world.dev.audio())
    world.run(0.5)
    assert len(world.dev.audio()) > sent + 10
    assert world.streamer.snapshot()["silent"] is False


def test_sound_after_silence_resumes_immediately(world):
    world.select_pad_sink()
    world.run(1)
    world.cap.audible = False
    world.run(5)
    count = len(world.dev.audio())
    enc = FakeEncoder.instances[-1]
    resets = enc.resets
    world.cap.audible = True
    resumed_at = world.clock.now
    world.run(0.3)
    audio = world.dev.audio()
    assert len(audio) > count
    # the jitter buffer holds ~48 ms of old silence ahead of the new sound
    assert audio[count][0] - resumed_at <= 0.12
    assert enc.resets == resets + 1                      # filter history dropped
    counters = [parse_audio_report(d).counter for _, d in audio]
    assert all((b - a) % 0x10000 == 4 for a, b in zip(counters, counters[1:]))
    assert world.streamer.snapshot()["silent"] is False


def test_stalled_capture_while_silent_does_not_inflate_underruns(world):
    world.select_pad_sink()
    world.run(1)
    world.cap.audible = False
    world.run(4)
    world.cap.stalled = True            # monitor suspended: no data at all
    world.run(5)
    assert world.streamer.stats.underruns < 5
    world.cap.stalled = False
    world.cap.audible = True
    sent = len(world.dev.audio())
    world.run(2)
    assert len(world.dev.audio()) > sent + 10
    # re-primed like a fresh start, so no stream of underruns without a cushion
    assert world.streamer.stats.underruns < 3
    assert world.streamer._pacer.stats.buffered_ms >= 30     # the jitter cushion is back


# --- counters --------------------------------------------------------------------

def test_underruns_are_counted_when_capture_starves(world):
    world.select_pad_sink()
    world.run(1)
    world.cap.stalled = True
    world.run(1)
    assert world.streamer.stats.underruns > 0
    assert world.streamer.stats.reports_sent > 0
    world.cap.stalled = False
    world.run(0.5)
    assert world.streamer.state == STREAMING


def test_overruns_are_counted_after_a_stall(world):
    world.select_pad_sink()
    world.run(1)
    # The control path (pactl) blocks for 400 ms; the pipe fills meanwhile.
    world.clock.sleep(0.4)
    world.run(0.2)
    assert world.streamer.stats.overruns >= 1


def test_clock_resync_is_counted(world):
    world.select_pad_sink()
    world.run(1)
    world.clock.sleep(0.5)            # whole process was frozen
    before = len(world.dev.audio())
    world.run(0.1)
    assert world.streamer.stats.clock_resyncs >= 1
    assert len(world.dev.audio()) - before <= 12         # not the 31 reports the stall cost


# --- keeping the lightbar on ------------------------------------------------------

def rgb_of(report):
    return tuple(report[3 + 5:3 + 8])


def test_lightbar_is_resent_about_every_half_second(world):
    world.select_pad_sink()
    world.run(3.2)
    lights = world.dev.lights()
    assert 5 <= len(lights) <= 7
    assert all(d[3] == 0x02 and rgb_of(d) == (10, 20, 30) for _, d in lights)
    gaps = [b[0] - a[0] for a, b in zip(lights, lights[1:])]
    assert min(gaps) >= 0.49 and max(gaps) <= 0.56
    assert lights[0][0] - world.dev.writes[0][0] <= 0.2        # first one right away
    assert world.streamer.stats.lightbar_updates == len(lights)


def test_no_lightbar_reports_while_idle(world):
    world.run(2)
    assert world.pads == []


def test_lightbar_is_kept_during_the_silent_pause(world):
    world.select_pad_sink()
    world.run(1)
    world.cap.audible = False
    world.run(5)                                  # now paused
    assert world.streamer.snapshot()["silent"] is True
    before = len(world.dev.lights())
    audio = len(world.dev.audio())
    world.run(2)
    assert 3 <= len(world.dev.lights()) - before <= 5
    assert len(world.dev.audio()) == audio        # and still no audio sent


def test_lightbar_follows_kernel_changes_but_rereads_at_most_once_a_second(world):
    world.select_pad_sink()
    world.run(0.3)
    reads = len(world.leds_saved)
    world.led_value = LedState(255, 0, 0)         # Batocera recoloured the pad
    world.run(1.3)
    assert rgb_of(world.dev.lights()[-1][1]) == (255, 0, 0)
    world.run(2)
    assert len(world.leds_saved) - reads <= 4      # about one read per second


def test_unreadable_colour_uses_the_blue_fallback():
    w = World()
    w.led_value = None
    w.select_pad_sink()
    w.run(1)
    assert rgb_of(w.dev.lights()[0][1]) == (0, 0, 64)


def test_fallback_colour_is_configurable():
    w = World(lightbar_fallback=(1, 2, 3))
    w.led_value = None
    w.select_pad_sink()
    w.run(1)
    assert rgb_of(w.dev.lights()[0][1]) == (1, 2, 3)


def test_colour_is_kept_when_a_later_reread_fails(world):
    world.select_pad_sink()
    world.run(0.3)
    world.led_value = None
    world.run(2.5)
    assert {rgb_of(d) for _, d in world.dev.lights()} == {(10, 20, 30)}


def test_keep_lightbar_can_be_switched_off():
    w = World(keep_lightbar=False)
    w.select_pad_sink()
    w.run(2)
    assert w.dev.lights() == []
    assert w.streamer.state == STREAMING


def test_lightbar_writes_sit_between_audio_reports_without_delaying_them(world):
    world.select_pad_sink()
    world.run(0.3)
    world.dev.state_cost = 0.005                  # a lightbar write is not free
    world.run(3)
    audio_times = [t for t, _ in world.dev.audio()]
    for t, _ in world.dev.lights()[1:]:
        before = [a for a in audio_times if a <= t]
        after = [a for a in audio_times if a > t]
        assert before and after                   # between two audio reports
    gaps = [b - a for a, b in zip(audio_times[3:], audio_times[4:])]
    assert max(gaps) < 0.025                      # audio cadence held
    assert world.streamer.stats.underruns == 0
    assert world.streamer.snapshot()["stats"]["clock_resyncs"] == 0


def test_lightbar_write_error_is_handled_like_any_write_error(world):
    world.select_pad_sink()
    world.run(0.3)
    world.dev.fail_on_lightbar = OSError(errno.ENODEV, "gone")
    world.run(1)
    assert world.streamer.state == IDLE
    assert world.streamer.stats.write_errors == 1
    assert world.dev.closed and world.cap.closed and world.leds_restored


def test_lightbar_report_is_a_valid_state_report(world):
    world.select_pad_sink()
    world.run(0.3)
    from padspeaker.protocol import build_lightbar_report
    assert world.dev.lights()[0][1] == build_lightbar_report(10, 20, 30)


# --- starting and stopping through combined outputs ---------------------------------------

def test_starts_when_its_sink_is_running_even_though_it_is_not_the_default(world):
    world.sinks.states[SINK] = "RUNNING"          # sound arrives through a combined output
    world.run(1)
    assert world.streamer.state == STREAMING
    assert world.streamer.snapshot()["via"] == "running"
    assert len(world.dev.audio()) > 10


def test_an_idle_or_suspended_sink_does_not_start_streaming(world):
    world.sinks.states[SINK] = "IDLE"
    world.run(2)
    assert world.streamer.state == IDLE
    world.sinks.states[SINK] = "SUSPENDED"
    world.run(2)
    assert world.streamer.state == IDLE


def test_starting_on_running_can_be_switched_off():
    w = World(start_on_running=False)
    w.sinks.states[SINK] = "RUNNING"
    w.run(2)
    assert w.streamer.state == IDLE


def test_default_sink_still_starts_without_asking_for_the_state(world):
    world.select_pad_sink()
    world.run(1)
    assert world.streamer.snapshot()["via"] == "default"
    assert world.sinks.state_calls == 0


def test_state_is_only_polled_about_twice_a_second(world):
    world.run(5)
    assert 8 <= world.sinks.state_calls <= 11


def test_stops_only_after_a_two_second_debounce(world):
    world.sinks.states[SINK] = "RUNNING"
    world.run(1)
    world.sinks.states[SINK] = "IDLE"             # combo deselected, nothing playing any more
    inactive_at = world.clock.now
    world.run(1.4)
    assert world.streamer.state == STREAMING      # still within the debounce
    world.run(2)
    assert world.streamer.state == IDLE
    assert world.dev.closed and world.cap.closed and world.leds_restored
    assert world.fallback_calls == []
    assert world.clock.now - inactive_at >= 3.0


def test_a_brief_gap_does_not_stop_it(world):
    world.sinks.states[SINK] = "RUNNING"
    world.run(1)
    for _ in range(6):                            # flaps: 1 s inactive, 1 s active, ...
        world.sinks.states[SINK] = "IDLE"
        world.run(1.0)
        world.sinks.states[SINK] = "RUNNING"
        world.run(1.0)
    assert world.streamer.state == STREAMING
    assert world.streamer.stats.starts == 1


def test_default_or_running_keeps_it_streaming(world):
    world.select_pad_sink()
    world.run(1)
    world.sinks.default = OTHER                   # user switched, but a combo is still playing
    world.sinks.states[SINK] = "RUNNING"
    world.run(6)
    assert world.streamer.state == STREAMING
    world.sinks.states[SINK] = "IDLE"
    world.run(3)
    assert world.streamer.state == IDLE


def test_unknown_state_neither_starts_nor_stops(world):
    world.sinks.fail_state = True
    world.run(2)
    assert world.streamer.state == IDLE
    world.select_pad_sink()
    world.run(1)
    world.sinks.default = OTHER                   # not default, state unknown
    world.run(5)
    assert world.streamer.state == STREAMING


def test_silence_pause_still_works_when_fed_through_a_combo(world):
    world.sinks.states[SINK] = "RUNNING"
    world.run(1)
    world.cap.audible = False
    world.run(5)
    assert world.streamer.snapshot()["silent"] is True
    assert world.streamer.state == STREAMING


def test_a_combo_is_not_remembered_as_the_previous_default():
    w = World()
    w.combos = {"padspeaker_tv_1122": (OTHER, SINK)}
    w.sinks.other_volumes["padspeaker_tv_1122"] = 50
    w.run(0.6)                                    # default is OTHER: remembered
    w.sinks.default = "padspeaker_tv_1122"               # the user picks a combo
    w.run(0.6)
    w.sinks.states[SINK] = "RUNNING"
    w.run(1)
    assert w.streamer.state == STREAMING
    assert w.streamer._last_other_default == OTHER      # not the combo
    w.dev.fail_with = OSError(errno.ENODEV, "gone")
    w.run(0.5)
    # the default is a combo, not this pad's sink: the daemon's combo manager
    # moves it away when the pad is removed, the streamer must not interfere
    assert w.fallback_calls == []


# --- volume follows what feeds the pad --------------------------------------------------

def test_volume_follows_the_default_combo_that_includes_this_pad(world):
    world.combos = {"padspeaker_tv_1122": (OTHER, SINK)}
    world.sinks.other_volumes["padspeaker_tv_1122"] = 30
    world.sinks.volume = 100                      # the pad's own sink is at 100 %
    world.sinks.default = "padspeaker_tv_1122"
    world.sinks.states[SINK] = "RUNNING"
    world.run(1)
    assert world.dev.states()[-1][1][3 + 21] == speaker_volume(30, False)
    world.sinks.other_volumes["padspeaker_tv_1122"] = 80
    world.run(0.6)
    assert world.dev.states()[-1][1][3 + 21] == speaker_volume(80, False)


def test_mute_of_the_default_combo_mutes_the_pad(world):
    world.combos = {"padspeaker_all": (SINK, "padspeaker_eeff")}
    world.sinks.other_volumes["padspeaker_all"] = 70
    world.sinks.other_muted["padspeaker_all"] = True
    world.sinks.default = "padspeaker_all"
    world.sinks.states[SINK] = "RUNNING"
    world.run(1)
    assert world.dev.states()[-1][1][3 + 21] == 0


def test_volume_uses_the_pads_own_sink_when_the_default_is_not_a_combo_with_it(world):
    world.combos = {"padspeaker_tv_eeff": (OTHER, "padspeaker_eeff")}      # a combo, but not ours
    world.sinks.other_volumes["padspeaker_tv_eeff"] = 10
    world.sinks.volume = 55
    world.sinks.default = "padspeaker_tv_eeff"
    world.sinks.states[SINK] = "RUNNING"
    world.run(1)
    assert world.dev.states()[-1][1][3 + 21] == speaker_volume(55, False)
    world.sinks.default = OTHER                                         # plain HDMI default
    world.run(0.8)
    assert world.dev.states()[-1][1][3 + 21] == speaker_volume(55, False)


def test_volume_uses_the_own_sink_when_it_is_the_default(world):
    world.select_pad_sink()
    world.sinks.volume = 40
    world.run(1)
    assert world.dev.states()[-1][1][3 + 21] == speaker_volume(40, False)


def test_streamer_without_a_combo_lookup_still_works():
    w = World()
    w.streamer._combo_lookup = None
    w.select_pad_sink()
    w.run(1)
    assert w.streamer.state == STREAMING


# --- Bluetooth link ---------------------------------------------------------------

def test_sniff_mode_is_disabled_right_before_the_pad_is_opened(world):
    world.run(2)
    assert world.link.sniff_calls == []                  # nothing while idle
    world.select_pad_sink()
    world.run(0.3)
    assert [mac for _, mac in world.link.sniff_calls] == [world.pad.mac]
    assert world.link.order[0] == ("sniff", 0)           # before the device was opened


def test_sniff_mode_is_disabled_again_every_30_seconds_while_streaming(world):
    world.select_pad_sink()
    world.run(95)
    times = [t for t, _ in world.link.sniff_calls]
    assert len(times) == 4                               # start, +30, +60, +90
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(29.9 <= g <= 30.6 for g in gaps)


def test_sniff_calls_continue_in_the_silent_pause(world):
    world.select_pad_sink()
    world.run(1)
    world.cap.audible = False
    world.run(65)
    assert len(world.link.sniff_calls) >= 3


def test_sniff_handling_can_be_switched_off():
    w = World(disable_sniff=False)
    w.select_pad_sink()
    w.run(65)
    assert w.link.sniff_calls == [] and w.streamer.state == STREAMING


def test_link_errors_do_not_stop_streaming(world):
    world.link.raises = True
    world.select_pad_sink()
    world.run(65)
    assert world.streamer.state == STREAMING
    assert len(world.dev.audio()) > 3000


def test_streamer_works_without_a_link_object():
    w = World()
    w.streamer._link = None
    w.select_pad_sink()
    w.run(35)
    assert w.streamer.state == STREAMING


@pytest.mark.parametrize("code", [errno.ENODEV, errno.EIO])
def test_hid_loss_triggers_one_clean_bluetooth_disconnect(code):
    w = World()
    w.select_pad_sink()
    w.run(0.5)
    w.dev.fail_with = OSError(code, "gone")
    w.run(0.5)
    assert w.streamer.state == IDLE
    assert w.link.disconnects == [w.pad.mac]


@pytest.mark.parametrize("code", [errno.EAGAIN, errno.EPIPE, errno.EBADF, errno.ENOTCONN])
def test_other_write_errors_do_not_disconnect(code):
    w = World()
    w.select_pad_sink()
    w.run(0.5)
    w.dev.fail_with = OSError(code, "x")
    w.run(0.5)
    assert w.streamer.state == IDLE
    assert w.link.disconnects == []


def test_disconnect_on_hid_loss_can_be_switched_off():
    w = World(disconnect_on_hid_loss=False)
    w.select_pad_sink()
    w.run(0.5)
    w.dev.fail_with = OSError(errno.ENODEV, "gone")
    w.run(0.5)
    assert w.link.disconnects == []
    assert w.streamer.state == IDLE


def test_disconnect_errors_are_contained():
    w = World()
    w.select_pad_sink()
    w.run(0.5)
    w.link.raises = True
    w.dev.fail_with = OSError(errno.EIO, "gone")
    w.run(0.5)                                           # no exception
    assert w.streamer.state == IDLE and w.leds_restored


# --- clean stop ----------------------------------------------------------------

def test_stops_cleanly_when_sink_is_no_longer_default(world):
    world.select_pad_sink()
    world.run(1)
    world.sinks.default = OTHER
    world.run(3)
    s = world.streamer
    assert s.state == IDLE
    assert world.dev.closed and world.cap.closed
    assert FakeEncoder.instances[-1].closed
    assert world.leds_restored == [(world.pad, world.led_value)]
    assert world.fallback_calls == []                    # user's choice, nothing to restore
    assert s.stats.write_errors == 0


def test_can_start_again_after_a_clean_stop(world):
    world.select_pad_sink()
    world.run(1)
    world.sinks.default = OTHER
    world.run(3)
    world.select_pad_sink()
    world.run(1)
    assert world.streamer.state == STREAMING
    assert len(world.pads) == 2 and len(world.captures) == 2
    first = parse_audio_report(world.pads[1].audio()[0][1])
    assert first.counter == 0                            # fresh session, fresh counter
    assert world.streamer.stats.starts == 2


# --- failures ---------------------------------------------------------------------

@pytest.mark.parametrize("code", [errno.EIO, errno.ENODEV, errno.ENOTCONN, errno.EBADF])
def test_pad_vanishing_stops_streaming_and_restores_previous_default(code):
    w = World()
    w.run(0.6)                                           # sees the previous default
    w.select_pad_sink()
    w.run(0.5)
    assert w.streamer.state == STREAMING
    w.dev.fail_with = OSError(code, "gone")
    w.run(0.3)
    s = w.streamer
    assert s.state == IDLE
    assert w.dev.closed and w.cap.closed
    assert w.leds_restored                               # lightbar restored regardless
    assert w.fallback_calls == [OTHER]
    assert s.stats.write_errors == 1


def test_other_write_errors_stop_without_fallback(world):
    world.select_pad_sink()
    world.run(0.5)
    world.dev.fail_with = OSError(errno.EAGAIN, "busy")
    world.run(0.3)
    assert world.streamer.state == IDLE
    assert world.fallback_calls == []
    assert world.streamer.stats.write_errors == 1
    assert world.leds_restored


def test_short_write_is_a_write_error(world):
    world.select_pad_sink()
    world.run(0.5)
    world.dev.short_write = True
    world.run(0.3)
    assert world.streamer.state == IDLE
    assert world.streamer.stats.write_errors == 1


def test_write_failure_on_the_first_volume_report_cleans_up():
    w = World()
    w.select_pad_sink()
    w.open_error = None
    # make the device fail immediately
    original = w._open_pad

    def failing(path):
        dev = original(path)
        dev.fail_with = OSError(errno.EIO, "gone")
        return dev
    w.streamer._open_pad = failing
    w.run(0.3)
    assert w.streamer.state == IDLE
    assert w.dev.closed and w.cap.closed
    assert w.leds_restored
    assert w.streamer.stats.write_errors == 1


def test_capture_ending_stops_and_restarts_with_backoff(world):
    world.select_pad_sink()
    world.run(1)
    world.cap.ended = True
    world.run(0.1)
    assert world.streamer.state == IDLE
    assert world.dev.closed and world.cap.closed and world.leds_restored
    assert world.fallback_calls == []
    world.run(3)
    assert world.streamer.state == STREAMING            # came back on its own
    assert world.streamer.stats.restarts == 1
    assert world.streamer.stats.write_errors == 0


def test_failed_start_backs_off_exponentially_to_8_seconds(world):
    world.select_pad_sink()
    world.open_error = OSError(errno.EACCES, "permission denied")
    world.run(40)
    times = world.open_times
    gaps = [round(b - a, 1) for a, b in zip(times, times[1:])]
    assert gaps[:5] == [0.5, 1.0, 2.0, 4.0, 8.0]
    assert all(g == 8.0 for g in gaps[4:])
    assert world.streamer.stats.start_failures == len(times)
    assert "permission denied" in world.streamer.stats.last_error
    assert world.streamer.state == IDLE
    # the failed attempts never leave a capture or lightbar change behind
    assert all(c.closed for c in world.captures)


def test_failed_start_does_not_spin(world):
    world.select_pad_sink()
    world.open_error = OSError(errno.ENOENT, "no such device")
    delays = []
    for _ in range(5):
        delays.append(world.streamer.step())
        world.clock.now += delays[-1]
    assert all(d > 0 for d in delays[1:])


def test_missing_pad_at_start_asks_for_the_previous_default():
    w = World()
    w.run(0.6)
    w.select_pad_sink()
    w.open_error = OSError(errno.ENODEV, "gone")
    w.run(1)
    assert w.fallback_calls == [OTHER]


def test_backoff_resets_after_a_stable_session(world):
    world.select_pad_sink()
    world.run(1)
    world.cap.ended = True
    world.run(0.1)
    world.run(2)                  # restarted after 0.5 s
    assert world.streamer.state == STREAMING
    world.run(10)                 # long stable session
    world.cap.ended = True
    world.run(0.1)
    stopped_at = world.clock.now
    world.run(3)
    assert world.open_times[-1] - stopped_at < 0.8       # short backoff again


def test_capture_start_failure_closes_what_was_opened(world):
    world.select_pad_sink()
    original = world._capture

    def broken(sink):
        cap = original(sink)

        def boom():
            raise RuntimeError("parec missing")
        cap.start = boom
        return cap
    world.streamer._capture_factory = broken
    world.run(1)
    assert world.streamer.state == IDLE
    assert world.dev.closed
    assert world.leds_restored
    assert world.streamer.stats.start_failures >= 1


def test_unexpected_exception_is_contained(world):
    world.select_pad_sink()
    world.run(0.5)
    FakeEncoder.instances[-1].fail = True
    world.run(0.3)                                       # must not raise
    assert world.streamer.state == IDLE
    assert world.dev.closed and world.cap.closed
    assert world.leds_restored
    assert "encoder blew up" in world.streamer.stats.last_error


def test_led_restore_runs_even_if_closing_fails(world):
    world.select_pad_sink()
    world.run(0.5)
    world.dev.close_raises = True
    world.sinks.default = OTHER
    world.run(3)
    assert world.streamer.state == IDLE
    assert world.cap.closed
    assert world.leds_restored


def test_unreadable_lightbar_is_not_restored(world):
    world.led_value = None
    world.select_pad_sink()
    world.run(0.5)
    world.sinks.default = OTHER
    world.run(3)
    assert world.leds_restored == []


# --- shutdown --------------------------------------------------------------------

def test_shutdown_stops_streaming_and_restores_light(world):
    world.select_pad_sink()
    world.run(1)
    world.streamer.shutdown()
    assert world.streamer.state == IDLE
    assert world.dev.closed and world.cap.closed and world.leds_restored
    assert world.fallback_calls == []
    assert world.streamer.step() > 0                     # closed streamer idles, never restarts
    assert len(world.pads) == 1


def test_shutdown_with_restore_default_moves_default_away_from_own_sink():
    w = World()
    w.run(0.6)
    w.select_pad_sink()
    w.run(1)
    w.streamer.shutdown(restore_default=True)
    assert w.fallback_calls == [OTHER]


def test_shutdown_restore_default_is_skipped_when_sink_is_not_default():
    w = World()
    w.run(0.6)
    w.streamer.shutdown(restore_default=True)
    assert w.fallback_calls == []


def test_shutdown_restore_default_when_idle_after_failed_starts():
    w = World()
    w.run(0.6)
    w.select_pad_sink()
    w.open_error = OSError(errno.EACCES, "denied")
    w.run(1)
    w.streamer.shutdown(restore_default=True)
    assert w.fallback_calls == [OTHER]


def test_shutdown_is_idempotent(world):
    world.select_pad_sink()
    world.run(0.5)
    world.streamer.shutdown()
    world.streamer.shutdown()
    assert len(world.leds_restored) == 1


def test_fallback_errors_are_contained():
    w = World()

    def bad(previous):
        raise RuntimeError("pactl gone")
    w.streamer._fallback = bad
    w.run(0.6)
    w.select_pad_sink()
    w.run(0.5)
    w.dev.fail_with = OSError(errno.ENODEV, "gone")
    w.run(0.3)                                           # no exception
    assert w.streamer.state == IDLE


# --- status ----------------------------------------------------------------------

def test_snapshot_reports_state_and_stats(world):
    snap = world.streamer.snapshot()
    assert snap["state"] == "idle" and snap["sink"] == SINK
    assert snap["mac"] == "aa:bb:cc:dd:11:22" and snap["silent"] is False
    world.select_pad_sink()
    world.run(1)
    snap = world.streamer.snapshot()
    assert snap["state"] == "streaming"
    assert snap["volume"] == MAX_SPEAKER_VOLUME
    for key in ("reports_sent", "underruns", "overruns", "write_errors", "restarts",
                "starts", "start_failures", "clock_resyncs", "last_error"):
        assert key in snap["stats"]
    assert snap["stats"]["reports_sent"] > 30


def test_snapshot_is_json_serialisable(world):
    import json
    world.select_pad_sink()
    world.run(0.5)
    json.dumps(world.streamer.snapshot())


# --- threaded helper -------------------------------------------------------------

def test_run_loop_stops_on_event():
    class Stub:
        def __init__(self):
            self.steps = 0

        def step(self):
            self.steps += 1
            return 0.01

    stub = Stub()
    w = World()
    w.streamer.step = stub.step
    stop = threading.Event()
    thread = threading.Thread(target=w.streamer.run, args=(stop,))
    thread.start()
    while stub.steps < 3:
        time.sleep(0.001)
    stop.set()
    thread.join(2)
    assert not thread.is_alive()
