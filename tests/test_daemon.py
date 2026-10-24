"""Tests for padspeaker.daemon: configuration, lock, status file and the pad loop.

The daemon is exercised with a fake sink manager, scripted pad discovery, fake
streamers and runners and a fake clock.  Nothing touches hardware, pactl or
(except in two small tests) real threads and signals.
"""
import json
import logging
import os
import signal
import threading

import pytest

from padspeaker import __version__
from padspeaker import daemon as daemon_module
from padspeaker.daemon import (AUTO, AlreadyRunning, ConfigError, Daemon, DaemonConfig,
                               InstanceLock, StateDirError, ThreadRunner,
                               default_lock_file, default_state_dir, default_status_file,
                               load_config_file, normalize_mac, secure_state_dir,
                               setup_logging, write_status_file)
from padspeaker.pads import Pad, PadWatcher
from padspeaker.sinks import Sink, SinkDetails, SinkError

PREFIXES = ("padspeaker_", "padspeaker_all", "padspeaker_tv_", "ds4_speaker_", "ds4_speakers_", "ds4_tv_")
MAC1 = "aa:bb:cc:dd:11:22"
MAC2 = "aa:bb:cc:dd:ee:ff"


def make_pad(mac=MAC1, node=7, name="Wireless Controller"):
    return Pad("/dev/hidraw%d" % node, mac, name, "0005:054C:09CC", None)


# --- fakes -------------------------------------------------------------------

class FakeSinks:
    def __init__(self, events):
        self.events = events
        self.sink_list = [Sink("alsa_output.hdmi", "HDMI", 80, False),
                          Sink("alsa_output.analog", "Analog", 50, False)]
        self.fail_create = 0
        self.fail_list = False
        self.create_error = SinkError("Connection refused")
        self.default = "alsa_output.hdmi"
        self.next_id = 100
        self.create_calls = 0
        self.combo_slaves = {}
        self.module_names = {}
        self.props = {}                 # sink name -> pactl properties (node.nick...)
        self.details_calls = 0
        self.fail_details = False

    def create_null_sink(self, name, description):
        self.create_calls += 1
        if self.fail_create > 0:
            self.fail_create -= 1
            raise self.create_error
        self.next_id += 1
        self.events.append(("create", name, description))
        self.sink_list.append(Sink(name, description, 100, False))
        return self.next_id

    def remove_null_sink(self, module):
        self.events.append(("remove", module))
        name = self.module_names.get(module, module)
        if name in self.combo_slaves:
            self.events.append(("remove_combo", name))
            del self.combo_slaves[name]
        self.sink_list = [s for s in self.sink_list if s.name != name]

    def sink_details(self):
        self.details_calls += 1
        if self.fail_details:
            raise SinkError("pactl list sinks failed")
        return {s.name: SinkDetails(s.name, s.description, dict(self.props.get(s.name, {})))
                for s in self.sink_list}

    def create_combine_sink(self, name, description, slaves):
        self.next_id += 1
        self.events.append(("create_combo", name, tuple(slaves)))
        self.combo_slaves[name] = tuple(slaves)
        self.module_names[self.next_id] = name
        self.sink_list.append(Sink(name, description, 100, False))
        return self.next_id

    def list_sinks(self):
        if self.fail_list:
            raise SinkError("down")
        return list(self.sink_list)

    def default_sink(self):
        return self.default

    def set_default_sink(self, name):
        self.events.append(("set_default", name))
        self.default = name


class FakeStreamer:
    def __init__(self, pad, sink_name, fallback, events):
        self.pad = pad
        self.sink_name = sink_name
        self.fallback = fallback
        self.events = events
        self.shutdowns = []

    def snapshot(self):
        return {"state": "idle", "mac": self.pad.mac, "sink": self.sink_name,
                "stats": {"reports_sent": 5}}

    def shutdown(self, restore_default=False):
        self.events.append(("shutdown", self.pad.mac, restore_default))
        self.shutdowns.append(restore_default)


class FakeRunner:
    def __init__(self, streamer, events):
        self.streamer = streamer
        self.events = events
        self.running = False

    def start(self):
        self.running = True
        self.events.append(("start", self.streamer.pad.mac))

    def stop(self):
        self.running = False
        self.events.append(("stop", self.streamer.pad.mac))


class FakeLink:
    def __init__(self, events):
        self.events = events
        self.sniff = []
        self.raises = False

    def disable_sniff(self, mac):
        self.events.append(("sniff", mac))
        self.sniff.append(mac)
        if self.raises:
            raise RuntimeError("hcitool exploded")

    def disconnect(self, mac):
        self.events.append(("disconnect", mac))
        return True


class Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


class Harness:
    def __init__(self, tmp_path, **config):
        self.events = []
        self.sinks = FakeSinks(self.events)
        self.clock = Clock()
        self.present = []
        self.streamers = []
        self.runners = []
        self.status_path = str(tmp_path / "status.json")
        config.setdefault("status_file", self.status_path)
        config.setdefault("lock_file", None)
        for key in ("combine_pads", "combine_tv_pad", "combine_tv_all"):
            config.setdefault(key, False)          # combined outputs have their own tests
        self.config = DaemonConfig(**config)
        self.link = FakeLink(self.events)
        self.batocera_calls = []
        self.daemon = Daemon(
            self.config,
            link=self.link,
            batocera_restore=self._batocera,
            sinks=self.sinks,
            watcher=PadWatcher(discover=lambda: list(self.present)),
            streamer_factory=self._streamer,
            runner_factory=self._runner,
            clock=self.clock,
            wall_clock=lambda: 1700000000.0 + (self.clock.now - 5000.0),
        )

    def _batocera(self, fallback, prefix):
        self.events.append(("batocera", fallback, prefix))
        self.batocera_calls.append((fallback, prefix))
        return True

    def _streamer(self, pad, sink_name, fallback):
        s = FakeStreamer(pad, sink_name, fallback, self.events)
        self.streamers.append(s)
        return s

    def _runner(self, streamer):
        r = FakeRunner(streamer, self.events)
        self.runners.append(r)
        return r

    def tick(self, advance=1.0):
        self.daemon.tick()
        self.clock.now += advance

    def kinds(self, kind):
        return [e for e in self.events if e[0] == kind]

    def status(self):
        with open(self.status_path) as handle:
            return json.load(handle)


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


# --- configuration -----------------------------------------------------------

def test_default_config_values():
    c = DaemonConfig()
    assert c.poll_interval == 1.0 and c.jitter_reports == 3
    assert c.speaker_volume_max == 0x4F
    assert c.sink_name_prefix == "padspeaker_"
    assert c.only_macs == [] and c.ignore_macs == []
    assert c.log_file is None
    assert c.keep_lightbar is True and c.lightbar_fallback == [0, 0, 64]


def test_normalize_mac():
    assert normalize_mac("aa:bb:cc:dd:11:22") == MAC1
    assert normalize_mac("aa-bb-cc-dd-11-22") == MAC1
    assert normalize_mac("aabbccdd1122") == MAC1
    with pytest.raises(ConfigError, match="MAC address"):
        normalize_mac("not-a-mac")


def test_from_dict_normalises_and_validates():
    c = DaemonConfig.from_dict({"only_macs": ["aa-bb-cc-dd-11-22"], "poll_interval": 2,
                                "speaker_volume_max": 40, "jitter_reports": 4,
                                "sink_name_prefix": "pad_", "log_file": "/tmp/x.log"})
    assert c.only_macs == [MAC1] and c.poll_interval == 2.0
    assert c.speaker_volume_max == 40 and c.jitter_reports == 4
    assert c.sink_name_prefix == "pad_" and c.log_file == "/tmp/x.log"


def test_lightbar_settings_from_dict():
    c = DaemonConfig.from_dict({"keep_lightbar": False, "lightbar_fallback": [255, 0, 10]})
    assert c.keep_lightbar is False and c.lightbar_fallback == [255, 0, 10]


@pytest.mark.parametrize("data, message", [
    ({"keep_lightbar": "yes"}, "keep_lightbar"),
    ({"lightbar_fallback": [0, 0]}, "lightbar_fallback"),
    ({"lightbar_fallback": [0, 0, 256]}, "lightbar_fallback"),
    ({"lightbar_fallback": "blue"}, "lightbar_fallback"),
    ({"lightbar_fallback": [0, 0, True]}, "lightbar_fallback"),
])
def test_lightbar_settings_are_validated(data, message):
    with pytest.raises(ConfigError) as info:
        DaemonConfig.from_dict(data)
    assert message in str(info.value)


def test_default_streamer_gets_the_lightbar_settings(tmp_path):
    config = DaemonConfig(status_file=None, lock_file=None, keep_lightbar=False,
                          lightbar_fallback=[9, 8, 7], jitter_reports=4, speaker_volume_max=30)
    daemon = Daemon(config, sinks=object(), watcher=PadWatcher(discover=lambda: []))
    streamer = daemon._default_streamer(make_pad(), "padspeaker_1122", lambda p: None)
    assert streamer.config.keep_lightbar is False
    assert tuple(streamer.config.lightbar_fallback) == (9, 8, 7)
    assert streamer.config.jitter_reports == 4 and streamer.config.speaker_volume_max == 30


@pytest.mark.parametrize("data, message", [
    ({"speaker_volume_max": 200}, "speaker_volume_max"),
    ({"speaker_volume_max": "loud"}, "speaker_volume_max"),
    ({"poll_interval": 0}, "poll_interval"),
    ({"jitter_reports": 0}, "jitter_reports"),
    ({"jitter_reports": 50}, "jitter_reports"),
    ({"sink_name_prefix": "bad name!"}, "sink_name_prefix"),
    ({"only_macs": "aa:bb:cc:dd:11:22"}, "only_macs"),
    ({"ignore_macs": ["zz"]}, "MAC address"),
    ({"colour": "red"}, "colour"),
])
def test_from_dict_rejects_bad_values_in_plain_language(data, message):
    with pytest.raises(ConfigError) as info:
        DaemonConfig.from_dict(data)
    assert message in str(info.value)


def test_load_config_file(tmp_path):
    path = tmp_path / "ds4.json"
    path.write_text(json.dumps({"poll_interval": 2, "ignore_macs": [MAC2]}))
    data = load_config_file(str(path))
    assert data == {"poll_interval": 2, "ignore_macs": [MAC2]}


def test_load_config_file_errors_are_readable(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config_file(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config_file(str(bad))
    arr = tmp_path / "arr.json"
    arr.write_text("[1]")
    with pytest.raises(ConfigError, match="JSON object"):
        load_config_file(str(arr))


def test_allows():
    open_ = DaemonConfig()
    assert open_.allows(MAC1) and open_.allows(MAC2)
    only = DaemonConfig(only_macs=[MAC1])
    assert only.allows(MAC1) and not only.allows(MAC2)
    ignore = DaemonConfig(ignore_macs=[MAC1])
    assert not ignore.allows(MAC1) and ignore.allows(MAC2)
    both = DaemonConfig(only_macs=[MAC1], ignore_macs=[MAC1])
    assert not both.allows(MAC1)


# --- single instance lock ----------------------------------------------------

def test_lock_blocks_second_instance_and_releases(tmp_path):
    path = str(tmp_path / "run" / "ds4.lock")
    first = InstanceLock(path)
    first.acquire()
    second = InstanceLock(path)
    with pytest.raises(AlreadyRunning) as info:
        second.acquire()
    assert str(os.getpid()) in str(info.value)
    assert "already running" in str(info.value)
    first.release()
    second.acquire()          # free again
    second.release()
    second.release()          # idempotent


def test_lock_context_manager(tmp_path):
    path = str(tmp_path / "l.lock")
    with InstanceLock(path):
        with pytest.raises(AlreadyRunning):
            InstanceLock(path).acquire()
    InstanceLock(path).acquire()


# --- status file --------------------------------------------------------------

def test_write_status_file_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "sub" / "status.json"
    write_status_file(str(path), {"a": 1})
    write_status_file(str(path), {"a": 2})
    assert json.loads(path.read_text()) == {"a": 2}
    assert [p.name for p in path.parent.iterdir()] == ["status.json"]


# --- pad lifecycle -------------------------------------------------------------

def test_pad_connect_creates_sink_and_streamer(h):
    h.present = [make_pad()]
    h.tick()
    assert h.kinds("create") == [("create", "padspeaker_1122",
                                  "Controller speaker (Wireless Controller 1122)")]
    assert [s.sink_name for s in h.streamers] == ["padspeaker_1122"]
    assert h.runners[0].running
    # polling again does not duplicate anything
    h.tick()
    assert len(h.streamers) == 1 and len(h.kinds("create")) == 1


def test_two_pads_get_two_sinks(h):
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h.tick()
    assert sorted(s.sink_name for s in h.streamers) == ["padspeaker_1122", "padspeaker_eeff"]


def test_custom_sink_prefix(tmp_path):
    h = Harness(tmp_path, sink_name_prefix="pad_")
    h.present = [make_pad()]
    h.tick()
    assert h.streamers[0].sink_name == "pad_1122"


def test_pad_disconnect_stops_streamer_then_removes_sink(h):
    h.present = [make_pad()]
    h.tick()
    h.events.clear()
    h.present = []
    h.tick()
    assert h.events == [("stop", MAC1), ("shutdown", MAC1, True), ("remove", 101)]
    assert not h.runners[0].running


def test_pad_reconnect_on_another_node_gets_a_fresh_streamer(h):
    h.present = [make_pad(node=7)]
    h.tick()
    h.present = [make_pad(node=9)]
    h.tick()
    assert len(h.streamers) == 2
    assert h.streamers[1].pad.hidraw_path == "/dev/hidraw9"
    assert h.runners[0].running is False and h.runners[1].running is True


def test_ignored_and_unlisted_pads_are_left_alone(tmp_path):
    h = Harness(tmp_path, ignore_macs=[MAC2])
    h.present = [make_pad(MAC2, 8)]
    h.tick()
    assert h.events == [] and h.streamers == []
    assert h.status()["ignored"] == [MAC2]
    h2 = Harness(tmp_path, only_macs=[MAC1])
    h2.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h2.tick()
    assert [s.pad.mac for s in h2.streamers] == [MAC1]
    h2.present = []
    h2.tick()           # removing an ignored pad must not touch anything
    assert h2.kinds("remove") == [("remove", 101)]


def test_discovery_errors_do_not_crash_the_loop(h):
    def boom():
        raise OSError("sysfs hiccup")
    h.daemon._watcher = PadWatcher(discover=boom)
    h.tick()                 # no exception
    h.daemon._watcher = PadWatcher(discover=lambda: [make_pad()])
    h.tick()
    assert len(h.streamers) == 1


def test_streamer_factory_failure_is_contained_and_retried(h):
    calls = []
    original = h.daemon._streamer_factory

    def flaky(pad, sink, fallback):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("libsbc exploded")
        return original(pad, sink, fallback)
    h.daemon._streamer_factory = flaky
    h.present = [make_pad()]
    h.tick()
    assert h.streamers == []
    for _ in range(40):
        h.tick(1.0)
    assert len(h.streamers) == 1


# --- sink creation retries -------------------------------------------------------

def test_sink_creation_is_retried_with_backoff(h):
    h.sinks.fail_create = 3
    h.present = [make_pad()]
    h.tick(0.0)
    assert h.sinks.create_calls == 1 and h.streamers == []
    h.tick(0.5)                      # too early for the retry
    assert h.sinks.create_calls == 1
    seen = []
    for _ in range(40):
        before = h.sinks.create_calls
        h.tick(0.25)
        if h.sinks.create_calls > before:
            seen.append(h.clock.now)
        if h.streamers:
            break
    assert len(h.streamers) == 1 and h.runners[0].running
    gaps = [b - a for a, b in zip(seen, seen[1:])]
    assert gaps == sorted(gaps)      # delays never shrink
    assert max(gaps) <= 16.0


def test_sink_retry_keeps_going_for_a_long_time_and_never_raises(h, caplog):
    h.sinks.fail_create = 10 ** 6
    h.present = [make_pad()]
    with caplog.at_level(logging.INFO, logger="padspeaker"):
        for _ in range(400):
            h.tick(1.0)
    assert h.streamers == []
    assert h.sinks.create_calls > 10
    # retries settle to a fixed, modest rate
    assert h.sinks.create_calls < 40
    problems = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert 1 <= len(problems) <= 8       # about one per minute, not one per attempt
    assert "audio server" in problems[0].getMessage()


def test_pad_removed_while_waiting_for_sink(h):
    h.sinks.fail_create = 5
    h.present = [make_pad()]
    h.tick()
    h.present = []
    h.tick()
    assert h.kinds("remove") == [] and h.streamers == []
    for _ in range(40):
        h.tick(1.0)
    assert h.streamers == [] and h.sinks.create_calls == 1


def test_status_shows_pending_sink(h):
    h.sinks.fail_create = 5
    h.present = [make_pad()]
    h.tick()
    pad = h.status()["pads"][0]
    assert pad["sink_ready"] is False and "Connection refused" in pad["sink_error"]


# --- default sink fallback -------------------------------------------------------

def test_fallback_prefers_the_previous_default(h):
    h.present = [make_pad()]
    h.tick()
    fallback = h.streamers[0].fallback
    h.sinks.default = "padspeaker_1122"
    fallback("alsa_output.analog")
    assert h.sinks.default == "alsa_output.analog"


def test_fallback_without_a_usable_previous_picks_another_real_sink(h):
    h.present = [make_pad()]
    h.tick()
    fallback = h.streamers[0].fallback
    for previous in (None, "gone", "padspeaker_1122"):
        h.sinks.default = "padspeaker_1122"
        fallback(previous)
        assert h.sinks.default == "alsa_output.hdmi"


def test_fallback_errors_are_contained(h):
    h.present = [make_pad()]
    h.tick()
    h.sinks.fail_list = True
    h.streamers[0].fallback(None)        # must not raise


# --- shutdown ---------------------------------------------------------------------

def test_shutdown_stops_everything_in_order(h):
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h.tick()
    assert os.path.exists(h.status_path)
    h.events.clear()
    h.daemon.shutdown()
    assert sorted(h.kinds("stop")) == sorted([("stop", MAC1), ("stop", MAC2)])
    assert sorted(h.kinds("shutdown")) == sorted([("shutdown", MAC1, True),
                                                  ("shutdown", MAC2, True)])
    assert all(s.shutdowns == [True] for s in h.streamers)
    assert len(h.kinds("remove")) == 2
    assert not os.path.exists(h.status_path)
    # every stop precedes its shutdown, which precedes its sink removal
    for mac in (MAC1, MAC2):
        order = [e[0] for e in h.events if len(e) > 1 and e[1] == mac]
        assert order[:2] == ["stop", "shutdown"]
    h.daemon.shutdown()                  # idempotent
    assert len(h.kinds("remove")) == 2


def test_stale_sinks_of_ours_are_removed_once_pads_are_known(h):
    h.sinks.sink_list.append(Sink("padspeaker_dead", "old", 100, False))
    h.present = [make_pad()]
    h.tick()
    assert [e[1] for e in h.kinds("remove")] == ["padspeaker_dead"]
    assert "alsa_output.hdmi" in [s.name for s in h.sinks.sink_list]
    h.tick()
    assert len(h.kinds("remove")) == 1         # only checked once


def test_stale_sinks_of_the_pre_rename_version_are_removed_too(h):
    """A crashed ds4-bt-speaker (the old name) can leave ds4_* sinks behind; the renamed daemon cleans them up."""
    for name in ("ds4_speaker_9999", "ds4_speakers_all", "ds4_tv_9999", "ds4_tv_all"):
        h.sinks.sink_list.append(Sink(name, "old", 100, False))
    h.present = [make_pad()]
    h.tick()
    assert sorted(e[1] for e in h.kinds("remove")) == ["ds4_speaker_9999", "ds4_speakers_all", "ds4_tv_9999", "ds4_tv_all"]
    assert "alsa_output.hdmi" in [s.name for s in h.sinks.sink_list]


def test_default_sink_names_use_the_new_prefix(h):
    h.present = [make_pad()]
    h.tick()
    assert [e[1] for e in h.kinds("create")] == ["padspeaker_1122"]


def test_stale_sink_cleanup_retries_when_pactl_is_down(h):
    h.sinks.sink_list.append(Sink("padspeaker_dead", "old", 100, False))
    h.sinks.fail_list = True
    h.tick()                                    # must not raise
    h.sinks.fail_list = False
    h.tick()
    assert [e[1] for e in h.kinds("remove")] == ["padspeaker_dead"]


# --- status file ------------------------------------------------------------------

def test_status_file_content(h):
    h.present = [make_pad()]
    h.tick()
    status = h.status()
    assert status["version"] == __version__
    assert status["pid"] == os.getpid()
    assert status["state"] == "running"
    assert status["updated_at"] >= status["started_at"]
    pad = status["pads"][0]
    assert pad["mac"] == MAC1 and pad["sink"] == "padspeaker_1122"
    assert pad["sink_ready"] is True and pad["hidraw"] == "/dev/hidraw7"
    assert pad["name"] == "Wireless Controller"
    assert pad["streamer"]["stats"]["reports_sent"] == 5


def test_status_file_refreshes_periodically_and_on_change(h):
    h.present = [make_pad()]
    h.tick(0.0)
    first = h.status()["updated_at"]
    h.clock.now += 0.5
    h.tick(0.0)
    assert h.status()["updated_at"] == first          # too soon, nothing changed
    h.clock.now += 5
    h.tick(0.0)
    assert h.status()["updated_at"] > first
    h.present = []
    h.tick(0.0)
    assert h.status()["pads"] == []                   # change is written at once


def test_status_file_can_be_disabled(tmp_path):
    h = Harness(tmp_path, status_file=None)
    h.present = [make_pad()]
    h.tick()
    assert not os.path.exists(h.status_path)


def test_unwritable_status_file_does_not_crash(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    h = Harness(tmp_path, status_file=str(blocker / "status.json"))
    h.present = [make_pad()]
    h.tick()
    assert len(h.streamers) == 1


# --- run loop -----------------------------------------------------------------------

def test_run_loops_until_stopped_and_cleans_up(tmp_path):
    h = Harness(tmp_path, poll_interval=0.01)
    h.present = [make_pad()]
    polls = []

    def discover():
        polls.append(1)
        if len(polls) == 3:
            h.daemon.request_stop()
        return list(h.present)
    h.daemon._watcher = PadWatcher(discover=discover)
    assert h.daemon.run(install_signals=False) == 0
    assert len(polls) == 3
    assert h.kinds("remove") and not h.runners[0].running
    assert not os.path.exists(h.status_path)


def test_run_exits_zero_on_sigterm_and_restores_handlers(tmp_path):
    h = Harness(tmp_path, poll_interval=0.01)
    h.present = [make_pad()]
    before = signal.getsignal(signal.SIGTERM)
    timer = threading.Timer(0.15, os.kill, args=(os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        assert h.daemon.run() == 0
    finally:
        timer.cancel()
    assert signal.getsignal(signal.SIGTERM) == before
    assert h.kinds("remove")


def test_run_takes_the_instance_lock(tmp_path):
    lock_path = str(tmp_path / "x.lock")
    h = Harness(tmp_path, lock_file=lock_path, poll_interval=0.01)
    holder = InstanceLock(lock_path)
    holder.acquire()
    try:
        with pytest.raises(AlreadyRunning):
            h.daemon.run(install_signals=False)
    finally:
        holder.release()
    assert h.events == []                # a refused instance touches nothing


# --- thread runner ----------------------------------------------------------------

def test_thread_runner_runs_and_stops_the_streamer():
    class Stub:
        def __init__(self):
            self.steps = 0
            self.started = threading.Event()

        def run(self, stop):
            self.started.set()
            stop.wait(5)

    stub = Stub()
    runner = ThreadRunner(stub)
    runner.start()
    assert stub.started.wait(2)
    runner.stop()
    runner.stop()                        # idempotent
    assert not runner.alive


# --- logging --------------------------------------------------------------------------

def test_setup_logging_levels_and_file(tmp_path):
    path = tmp_path / "logs" / "ds4.log"
    logger = setup_logging(verbosity=1, log_file=str(path))
    try:
        assert logger.level == logging.DEBUG
        logging.getLogger("padspeaker.test").debug("hello file")
        for handler in logger.handlers:
            handler.flush()
        assert "hello file" in path.read_text()
        handlers_before = len(logger.handlers)
        setup_logging(verbosity=0, log_file=str(path))      # no duplicate handlers
        assert len(logger.handlers) == handlers_before
        assert logger.level == logging.INFO
        assert setup_logging(verbosity=-1).level == logging.WARNING
    finally:
        setup_logging(verbosity=0)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)


def test_setup_logging_uses_rotation(tmp_path):
    from logging.handlers import RotatingFileHandler
    logger = setup_logging(0, str(tmp_path / "r.log"))
    try:
        assert any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_setup_logging_survives_unwritable_log_file(tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    logger = setup_logging(0, str(blocker / "x.log"))   # cannot create the directory
    try:
        assert logger.handlers          # stderr handler still there
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)


# --- safe locations for the lock and status files ---------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    return monkeypatch


def test_default_state_dir_prefers_an_owned_xdg_runtime_dir(clean_env, tmp_path):
    clean_env.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert default_state_dir() == str(tmp_path / "padspeaker")
    assert default_status_file() == str(tmp_path / "padspeaker" / "status.json")
    assert default_lock_file() == str(tmp_path / "padspeaker" / "daemon.lock")


def test_default_state_dir_is_run_for_root(clean_env, monkeypatch):
    monkeypatch.setattr(daemon_module.os, "geteuid", lambda: 0)
    assert default_state_dir() == "/run/padspeaker"


def test_default_state_dir_ignores_an_xdg_dir_owned_by_someone_else(clean_env, tmp_path,
                                                                    monkeypatch):
    clean_env.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    real = os.geteuid()
    monkeypatch.setattr(daemon_module.os, "geteuid", lambda: real + 1)
    with pytest.raises(StateDirError):
        default_state_dir()
    monkeypatch.setattr(daemon_module.os, "geteuid", lambda: 0)
    assert default_state_dir() == "/run/padspeaker"


def test_default_state_dir_refuses_instead_of_using_tmp(clean_env, monkeypatch):
    monkeypatch.setattr(daemon_module.os, "geteuid", lambda: 1000)
    with pytest.raises(StateDirError) as info:
        default_state_dir()
    message = str(info.value)
    assert "--runtime-dir" in message and "--lock-file" in message and "--status-file" in message
    assert "/tmp" not in message


def test_explicit_runtime_dir_is_used_even_for_ordinary_users(clean_env, monkeypatch):
    monkeypatch.setattr(daemon_module.os, "geteuid", lambda: 1000)
    assert default_state_dir("/var/run") == "/var/run/padspeaker"


def test_config_resolves_defaults_and_honours_explicit_values(clean_env, tmp_path):
    clean_env.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    c = DaemonConfig()
    assert c.status_file is AUTO and c.lock_file is AUTO and c.uses_default_state_dir()
    assert c.status_path() == str(tmp_path / "padspeaker" / "status.json")
    c = DaemonConfig(status_file="/x/s.json", lock_file=None)
    assert (c.status_path(), c.lock_path()) == ("/x/s.json", None)
    assert not c.uses_default_state_dir()
    # JSON null disables, an absent key keeps the default
    assert DaemonConfig.from_dict({"status_file": None}).status_file is None
    assert DaemonConfig.from_dict({}).status_file is AUTO


def test_secure_state_dir_creates_a_private_directory(tmp_path):
    target = tmp_path / "state"
    secure_state_dir(str(target))
    assert target.is_dir() and (target.stat().st_mode & 0o777) == 0o700
    secure_state_dir(str(target))            # existing and fine


def test_secure_state_dir_refuses_a_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    os.symlink(str(real), str(link))
    with pytest.raises(StateDirError, match="not a plain directory"):
        secure_state_dir(str(link))


def test_secure_state_dir_refuses_a_file(tmp_path):
    (tmp_path / "f").write_text("x")
    with pytest.raises(StateDirError):
        secure_state_dir(str(tmp_path / "f"))


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777, 0o720])
def test_secure_state_dir_refuses_group_or_world_writable(tmp_path, mode):
    target = tmp_path / "d"
    target.mkdir()
    os.chmod(str(target), mode)
    with pytest.raises(StateDirError, match="written by other users"):
        secure_state_dir(str(target))


def test_secure_state_dir_refuses_a_foreign_owner(tmp_path, monkeypatch):
    target = tmp_path / "d"
    target.mkdir(mode=0o700)
    real = os.geteuid()
    monkeypatch.setattr(daemon_module.os, "geteuid", lambda: real + 1)
    with pytest.raises(StateDirError, match="owned by another user"):
        secure_state_dir(str(target))


def test_secure_state_dir_with_missing_parent_explains(tmp_path):
    with pytest.raises(StateDirError, match="--runtime-dir"):
        secure_state_dir(str(tmp_path / "nope" / "state"))


def test_lock_does_not_follow_a_planted_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    lock_path = tmp_path / "daemon.lock"
    os.symlink(str(victim), str(lock_path))
    with pytest.raises(StateDirError):
        InstanceLock(str(lock_path)).acquire()
    assert victim.read_text() == "precious"           # never truncated or written


def test_lock_refuses_a_foreign_owned_file_without_truncating(tmp_path, monkeypatch):
    lock_path = tmp_path / "daemon.lock"
    lock_path.write_text("4242\n")
    real = os.geteuid()
    monkeypatch.setattr(daemon_module.os, "geteuid", lambda: real + 1)
    with pytest.raises(StateDirError, match="regular file owned by you"):
        InstanceLock(str(lock_path)).acquire()
    assert lock_path.read_text() == "4242\n"


def test_lock_file_is_private_and_normal_use_still_works(tmp_path):
    lock_path = tmp_path / "sub" / "daemon.lock"
    with InstanceLock(str(lock_path)):
        assert (lock_path.stat().st_mode & 0o777) == 0o600
        assert lock_path.read_text().strip() == str(os.getpid())


def test_status_temp_file_is_exclusive_and_not_followed(tmp_path, monkeypatch):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    monkeypatch.setattr(daemon_module.secrets, "token_hex", lambda n: "deadbeef")
    os.symlink(str(victim), str(tmp_path / ".status-deadbeef.tmp"))
    with pytest.raises(OSError):
        write_status_file(str(tmp_path / "status.json"), {"a": 1})
    assert victim.read_text() == "precious"
    assert not (tmp_path / "status.json").exists()


def test_status_temp_names_are_random(tmp_path, monkeypatch):
    names = []
    real_open = os.open

    def spy(path, flags, mode=0o777):
        if ".status-" in str(path):
            names.append(os.path.basename(str(path)))
            assert flags & os.O_EXCL and flags & os.O_NOFOLLOW
        return real_open(path, flags, mode)
    monkeypatch.setattr(daemon_module.os, "open", spy)
    write_status_file(str(tmp_path / "s.json"), {"a": 1})
    write_status_file(str(tmp_path / "s.json"), {"a": 2})
    assert len(set(names)) == 2


def test_status_file_is_replaced_not_followed_if_it_is_a_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    os.symlink(str(victim), str(tmp_path / "status.json"))
    write_status_file(str(tmp_path / "status.json"), {"a": 1})
    assert victim.read_text() == "precious"
    assert json.loads((tmp_path / "status.json").read_text()) == {"a": 1}


def test_run_prepares_and_secures_the_default_state_dir(clean_env, tmp_path):
    clean_env.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    h = Harness(tmp_path, status_file=AUTO, lock_file=AUTO, poll_interval=0.01)
    h.present = [make_pad()]
    polls = []

    def discover():
        polls.append(1)
        if len(polls) == 2:
            h.daemon.request_stop()
        return list(h.present)
    h.daemon._watcher = PadWatcher(discover=discover)
    assert h.daemon.run(install_signals=False) == 0
    state = tmp_path / "padspeaker"
    assert state.is_dir() and (state.stat().st_mode & 0o777) == 0o700


def test_run_refuses_an_unsafe_default_state_dir(clean_env, tmp_path):
    clean_env.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    bad = tmp_path / "padspeaker"
    bad.mkdir()
    os.chmod(str(bad), 0o777)
    h = Harness(tmp_path, status_file=AUTO, lock_file=AUTO, poll_interval=0.01)
    with pytest.raises(StateDirError):
        h.daemon.run(install_signals=False)
    assert h.events == []


# --- Bluetooth link and Batocera sync -------------------------------------------------

def test_new_pad_gets_sniff_mode_disabled_before_its_streamer_starts(h):
    h.present = [make_pad()]
    h.tick()
    assert h.link.sniff == [MAC1]
    sniff = h.events.index(("sniff", MAC1))
    assert sniff < h.events.index(("start", MAC1))
    h.tick()
    assert h.link.sniff == [MAC1]                  # once per connect, not per poll


def test_each_new_pad_gets_it_and_ignored_pads_do_not(tmp_path):
    h = Harness(tmp_path, ignore_macs=[MAC2])
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h.tick()
    assert h.link.sniff == [MAC1]


def test_disable_sniff_setting_off(tmp_path):
    h = Harness(tmp_path, disable_sniff=False)
    h.present = [make_pad()]
    h.tick()
    assert h.link.sniff == [] and len(h.streamers) == 1


def test_link_errors_do_not_stop_a_pad_from_being_set_up(h):
    h.link.raises = True
    h.present = [make_pad()]
    h.tick()
    assert len(h.streamers) == 1 and h.runners[0].running


def test_link_settings_reach_the_default_streamer():
    config = DaemonConfig(status_file=None, lock_file=None, disable_sniff=False,
                          disconnect_on_hid_loss=False)
    link = object()
    daemon = Daemon(config, sinks=object(), watcher=PadWatcher(discover=lambda: []), link=link)
    streamer = daemon._default_streamer(make_pad(), "padspeaker_1122", lambda p: None)
    assert streamer.config.disable_sniff is False
    assert streamer.config.disconnect_on_hid_loss is False
    assert streamer._link is link


def test_fallback_also_fixes_the_saved_batocera_choice(h):
    h.present = [make_pad()]
    h.tick()
    h.sinks.default = "padspeaker_1122"
    h.streamers[0].fallback("alsa_output.analog")
    assert h.batocera_calls == [("alsa_output.analog", PREFIXES)]
    assert h.events.index(("set_default", "alsa_output.analog")) < \
        h.events.index(("batocera", "alsa_output.analog", PREFIXES))


def test_batocera_sync_follows_the_chosen_sink_and_prefix(tmp_path):
    h = Harness(tmp_path, sink_name_prefix="pad_")
    h.present = [make_pad()]
    h.tick()
    h.streamers[0].fallback(None)
    assert h.batocera_calls == [("alsa_output.hdmi", ("pad_", "padspeaker_all", "padspeaker_tv_", "ds4_speaker_", "ds4_speakers_", "ds4_tv_"))]


def test_batocera_sync_can_be_switched_off(tmp_path):
    h = Harness(tmp_path, sync_batocera_audio_device=False)
    h.present = [make_pad()]
    h.tick()
    h.streamers[0].fallback("alsa_output.analog")
    assert h.batocera_calls == []
    assert h.sinks.default == "alsa_output.analog"


def test_batocera_sync_is_skipped_when_no_sink_could_be_chosen(h):
    h.present = [make_pad()]
    h.tick()
    h.sinks.sink_list = []
    h.streamers[0].fallback(None)
    assert h.batocera_calls == []


def test_batocera_errors_are_contained(tmp_path):
    h = Harness(tmp_path)

    def boom(fallback, prefix):
        raise RuntimeError("settings tool broke")
    h.daemon._batocera_restore = boom
    h.present = [make_pad()]
    h.tick()
    h.streamers[0].fallback("alsa_output.analog")      # no exception
    assert h.sinks.default == "alsa_output.analog"


def test_new_config_keys_and_defaults():
    c = DaemonConfig()
    assert c.disable_sniff is True and c.disconnect_on_hid_loss is True
    assert c.sync_batocera_audio_device is None            # automatic: only where the tool exists
    c = DaemonConfig.from_dict({"disable_sniff": False, "disconnect_on_hid_loss": False,
                                "sync_batocera_audio_device": True})
    assert (c.disable_sniff, c.disconnect_on_hid_loss, c.sync_batocera_audio_device) == \
        (False, False, True)
    assert DaemonConfig.from_dict({"sync_batocera_audio_device": None}) \
        .sync_batocera_audio_device is None


@pytest.mark.parametrize("key", ["disable_sniff", "disconnect_on_hid_loss",
                                 "sync_batocera_audio_device"])
def test_new_boolean_keys_are_validated(key):
    with pytest.raises(ConfigError, match=key):
        DaemonConfig.from_dict({key: "yes"})


# --- combined outputs ---------------------------------------------------------------------

HDMI = "alsa_output.hdmi"
ANALOG = "alsa_output.analog"


def combo_harness(tmp_path, **config):
    config.setdefault("combine_pads", True)
    config.setdefault("combine_tv_pad", True)
    config.setdefault("combine_tv_all", True)
    return Harness(tmp_path, **config)


def combos_of(h):
    return dict(h.sinks.combo_slaves)


def test_one_pad_gets_a_tv_combo_with_the_default_output_as_tv(tmp_path):
    h = combo_harness(tmp_path)
    h.present = [make_pad()]
    h.tick()
    assert combos_of(h) == {"padspeaker_tv_1122": (HDMI, "padspeaker_1122")}
    desc = [s.description for s in h.sinks.sink_list if s.name == "padspeaker_tv_1122"]
    assert desc == ["HDMI + controller speaker (Wireless Controller 1122)"]   # the sink's own description
    assert h.status()["tv_sink"] == HDMI
    assert h.status()["combos"][0]["name"] == "padspeaker_tv_1122"


def descriptions_of(h):
    return {s.name: s.description for s in h.sinks.sink_list if s.name in h.sinks.combo_slaves}


def test_descriptions_use_the_main_outputs_own_description(tmp_path):
    h = combo_harness(tmp_path)
    h.sinks.sink_list[0] = Sink(HDMI, "HD-Audio Generic Digital Stereo (HDMI 2)", 80, False)
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h.tick()
    d = descriptions_of(h)
    assert d["padspeaker_tv_1122"] == ("HD-Audio Generic Digital Stereo (HDMI 2) + controller speaker "
                                "(Wireless Controller 1122)")
    assert d["padspeaker_tv_all"] == "HD-Audio Generic Digital Stereo (HDMI 2) + all controller speakers"
    assert d["padspeaker_all"] == "Controller speakers (all)"
    assert set(d) == {"padspeaker_all", "padspeaker_tv_1122", "padspeaker_tv_eeff", "padspeaker_tv_all"}   # names fixed


EXAMPLE_DISPLAY_PROPS = {"node.nick": "Example Display", "alsa.name": "Example Display",
                 "device.description": "HD-Audio Generic"}


def example_display_harness(tmp_path, **config):
    h = combo_harness(tmp_path, **config)
    h.sinks.sink_list[0] = Sink(HDMI, "HD-Audio Generic Digital Stereo (HDMI 2)", 80, False)
    h.sinks.props[HDMI] = dict(EXAMPLE_DISPLAY_PROPS)
    return h


def test_label_uses_the_tvs_own_name_from_its_properties(tmp_path):
    h = example_display_harness(tmp_path)
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h.tick()
    d = descriptions_of(h)
    assert d["padspeaker_tv_1122"] == "Example Display (HDMI 2) + controller speaker (Wireless Controller 1122)"
    assert d["padspeaker_tv_all"] == "Example Display (HDMI 2) + all controller speakers"
    assert set(d) == {"padspeaker_all", "padspeaker_tv_1122", "padspeaker_tv_eeff", "padspeaker_tv_all"}   # names fixed


def test_configured_label_wins_and_skips_the_property_lookup(tmp_path):
    h = example_display_harness(tmp_path, tv_label="TV")
    h.present = [make_pad()]
    h.tick()
    assert descriptions_of(h)["padspeaker_tv_1122"] == "TV + controller speaker (Wireless Controller 1122)"
    assert h.sinks.details_calls == 0


def test_properties_are_looked_up_once_per_cycle_not_per_tick(tmp_path):
    h = example_display_harness(tmp_path)
    h.present = [make_pad()]
    for _ in range(20):                                    # 20 s of ticks
        h.tick()
    assert h.sinks.details_calls <= 2


def test_label_is_rebuilt_when_the_tv_is_swapped_on_the_same_port(tmp_path):
    h = example_display_harness(tmp_path)
    h.present = [make_pad()]
    h.tick()
    h.sinks.default = "padspeaker_tv_1122"
    h.sinks.props[HDMI] = {"node.nick": "LG TV", "device.description": "HD-Audio Generic"}
    h.events.clear()
    for _ in range(40):                                    # the periodic refresh notices it
        h.tick()
    assert descriptions_of(h)["padspeaker_tv_1122"] == "LG TV (HDMI 2) + controller speaker (Wireless Controller 1122)"
    assert h.sinks.default == "padspeaker_tv_1122" and h.batocera_calls == []
    assert h.sinks.details_calls <= 4


def test_label_follows_a_changed_description_at_once(tmp_path):
    h = example_display_harness(tmp_path)
    h.present = [make_pad()]
    h.tick()
    h.sinks.sink_list[0] = Sink(HDMI, "HD-Audio Generic Digital Stereo (HDMI 3)", 80, False)
    for _ in range(3):
        h.tick()
    assert descriptions_of(h)["padspeaker_tv_1122"].startswith("Example Display (HDMI 3) + ")


def test_label_falls_back_to_the_description_when_properties_cannot_be_read(tmp_path):
    h = example_display_harness(tmp_path)
    h.sinks.fail_details = True
    h.present = [make_pad()]
    h.tick()
    assert descriptions_of(h)["padspeaker_tv_1122"].startswith(
        "HD-Audio Generic Digital Stereo (HDMI 2) + ")
    h.sinks.fail_details = False
    for _ in range(40):
        h.tick()
    assert descriptions_of(h)["padspeaker_tv_1122"].startswith("Example Display (HDMI 2) + ")


def test_generic_nick_gives_the_description_label(tmp_path):
    h = example_display_harness(tmp_path)
    h.sinks.props[HDMI] = {"node.nick": "HDMI", "device.description": "HD-Audio Generic"}
    h.present = [make_pad()]
    h.tick()
    assert descriptions_of(h)["padspeaker_tv_1122"].startswith("HD-Audio Generic Digital Stereo (HDMI 2) + ")


def test_tv_label_overrides_the_wording(tmp_path):
    h = combo_harness(tmp_path, tv_label="TV")
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h.tick()
    d = descriptions_of(h)
    assert d["padspeaker_tv_1122"] == "TV + controller speaker (Wireless Controller 1122)"
    assert d["padspeaker_tv_all"] == "TV + all controller speakers"


def test_tv_label_is_sanitised_and_capped(tmp_path):
    h = combo_harness(tmp_path, tv_label='Living "room" \n' + "x" * 100)
    h.present = [make_pad()]
    h.tick()
    (description,) = descriptions_of(h).values()
    label = description.split(" + controller speaker")[0]
    assert '"' not in label and "\n" not in label and len(label) <= 60
    assert label.startswith("Living room x")


def test_label_follows_the_tv_sink_when_it_changes(tmp_path):
    h = combo_harness(tmp_path)
    h.present = [make_pad()]
    h.tick()
    h.sinks.sink_list = [s for s in h.sinks.sink_list if s.name not in (HDMI, ANALOG)]
    h.sinks.sink_list.append(Sink(ANALOG, "Built-in Analog", 50, False))     # HDMI unplugged
    h.sinks.default = ANALOG
    for _ in range(4):
        h.tick()
    assert descriptions_of(h)["padspeaker_tv_1122"] == "Built-in Analog + controller speaker (Wireless Controller 1122)"


def test_combo_is_rebuilt_when_only_the_description_changes_and_stays_default(tmp_path):
    h = combo_harness(tmp_path)
    h.present = [make_pad()]
    h.tick()
    h.sinks.default = "padspeaker_tv_1122"
    h.events.clear()
    for i, s in enumerate(h.sinks.sink_list):                  # the sink got renamed by the server
        if s.name == HDMI:
            h.sinks.sink_list[i] = Sink(HDMI, "HDMI (Monitor)", 80, False)
    for _ in range(3):
        h.tick()
    assert descriptions_of(h)["padspeaker_tv_1122"].startswith("HDMI (Monitor) + ")
    assert [e[0] for e in h.events].count("remove_combo") == 1
    assert h.sinks.default == "padspeaker_tv_1122" and h.batocera_calls == []


def test_tv_label_config_and_validation():
    assert DaemonConfig().tv_label is None
    assert DaemonConfig.from_dict({"tv_label": "Sound bar"}).tv_label == "Sound bar"
    assert DaemonConfig.from_dict({"tv_label": None}).tv_label is None
    with pytest.raises(ConfigError, match="tv_label"):
        DaemonConfig.from_dict({"tv_label": 5})


def test_second_pad_adds_the_group_combos(tmp_path):
    h = combo_harness(tmp_path)
    h.present = [make_pad(MAC1, 7)]
    h.tick()
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h.tick()
    assert set(combos_of(h)) == {"padspeaker_tv_1122", "padspeaker_tv_eeff", "padspeaker_all", "padspeaker_tv_all"}
    assert combos_of(h)["padspeaker_tv_all"] == (HDMI, "padspeaker_1122", "padspeaker_eeff")
    assert combos_of(h)["padspeaker_all"] == ("padspeaker_1122", "padspeaker_eeff")


def test_pad_leaving_removes_its_combos_before_its_sink(tmp_path):
    h = combo_harness(tmp_path)
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h.tick()
    h.events.clear()
    h.present = [make_pad(MAC1, 7)]
    h.tick()
    assert set(combos_of(h)) == {"padspeaker_tv_1122"}
    kinds = [e[0] for e in h.events]
    last_combo_removal = max(i for i, k in enumerate(kinds) if k == "remove_combo")
    assert last_combo_removal < kinds.index("stop")           # combos go before the pad's sink
    assert "padspeaker_eeff" not in {s for sl in combos_of(h).values() for s in sl}


def test_default_combo_vanishing_restores_the_previous_default_and_batocera(tmp_path):
    h = combo_harness(tmp_path)
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h.tick()
    h.sinks.default = "padspeaker_all"                       # the user picked the group
    h.tick()
    h.events.clear()
    h.present = [make_pad(MAC1, 7)]                           # one pad leaves: group is gone
    h.tick()
    assert h.sinks.default == HDMI                            # back to the remembered output
    assert h.batocera_calls[-1] == (HDMI, PREFIXES)
    kinds = [e[0] for e in h.events]
    assert kinds.index("set_default") < kinds.index("remove_combo")


def test_default_combo_that_is_rebuilt_stays_the_default(tmp_path):
    h = combo_harness(tmp_path)
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h.tick()
    h.sinks.default = "padspeaker_all"
    h.tick()
    third = "11:22:33:44:55:66"
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8), make_pad(third, 9)]
    h.tick()
    assert h.sinks.default == "padspeaker_all"
    assert h.sinks.combo_slaves["padspeaker_all"] == (
        "padspeaker_5566", "padspeaker_1122", "padspeaker_eeff")
    assert h.batocera_calls == []


def test_groups_can_be_switched_off(tmp_path):
    h = combo_harness(tmp_path, combine_pads=False, combine_tv_all=False)
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h.tick()
    assert set(combos_of(h)) == {"padspeaker_tv_1122", "padspeaker_tv_eeff"}
    h2 = combo_harness(tmp_path, combine_tv_pad=False, combine_tv_all=False)
    h2.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h2.tick()
    assert set(combos_of(h2)) == {"padspeaker_all"}


def test_all_groups_off_never_touches_combos(tmp_path):
    h = Harness(tmp_path)                                     # harness default: all off
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h.tick()
    assert combos_of(h) == {}


def test_tv_sink_is_the_first_default_that_is_not_ours_and_sticks(tmp_path):
    h = combo_harness(tmp_path)
    h.present = [make_pad()]
    h.tick()
    h.sinks.default = ANALOG                                  # the user switches outputs later
    for _ in range(5):
        h.tick()
    assert combos_of(h)["padspeaker_tv_1122"][0] == HDMI             # the TV stays the first one


def test_tv_sink_is_chosen_again_when_it_disappears(tmp_path):
    h = combo_harness(tmp_path)
    h.present = [make_pad()]
    h.tick()
    h.sinks.sink_list = [s for s in h.sinks.sink_list if s.name != HDMI]   # HDMI unplugged
    h.sinks.default = ANALOG
    for _ in range(4):
        h.tick()
    assert combos_of(h)["padspeaker_tv_1122"] == (ANALOG, "padspeaker_1122")
    assert h.status()["tv_sink"] == ANALOG


def test_no_tv_combo_while_the_default_is_one_of_ours(tmp_path):
    h = combo_harness(tmp_path)
    h.sinks.default = "padspeaker_1122"                      # left over from a previous run
    h.present = [make_pad()]
    h.tick()
    assert combos_of(h) == {}
    h.sinks.default = HDMI
    for _ in range(3):
        h.tick()
    assert set(combos_of(h)) == {"padspeaker_tv_1122"}


def test_configured_tv_sink_is_used_and_must_exist(tmp_path):
    h = combo_harness(tmp_path, tv_sink=ANALOG)
    h.present = [make_pad()]
    h.tick()
    assert combos_of(h)["padspeaker_tv_1122"][0] == ANALOG
    h2 = combo_harness(tmp_path, tv_sink="no_such_sink")
    h2.present = [make_pad()]
    h2.tick()
    assert combos_of(h2) == {}
    h3 = combo_harness(tmp_path, tv_sink="padspeaker_all")   # one of ours: refused
    h3.present = [make_pad()]
    h3.tick()
    assert combos_of(h3) == {}


def test_no_combo_ever_feeds_a_combo_or_a_pad_sink_with_itself(tmp_path):
    h = combo_harness(tmp_path)
    steps = [[make_pad(MAC1, 7)], [make_pad(MAC1, 7), make_pad(MAC2, 8)], [make_pad(MAC2, 8)], [],
             [make_pad(MAC1, 7), make_pad(MAC2, 8)]]
    for pads in steps:
        h.present = pads
        h.tick()
        combos = combos_of(h)
        for name, slaves in combos.items():
            assert not set(slaves) & set(combos)
            assert name not in slaves
            assert all(s == HDMI or s.startswith("padspeaker_") for s in slaves)
    assert combos_of(h) != {}


def test_streamers_get_the_combo_lookup(tmp_path):
    config = DaemonConfig(status_file=None, lock_file=None)
    daemon = Daemon(config, sinks=object(), watcher=PadWatcher(discover=lambda: []))
    streamer = daemon._default_streamer(make_pad(), "padspeaker_1122", lambda p: None)
    assert streamer._combo_lookup == daemon._combos.slaves_of
    assert streamer.config.start_on_running is True


def test_shutdown_removes_combos_first_and_restores_a_combo_default(tmp_path):
    h = combo_harness(tmp_path)
    h.present = [make_pad(MAC1, 7), make_pad(MAC2, 8)]
    h.tick()
    h.sinks.default = "padspeaker_tv_all"
    h.tick()
    h.events.clear()
    h.daemon.shutdown()
    kinds = [e[0] for e in h.events]
    assert combos_of(h) == {}
    assert h.sinks.default == HDMI
    assert h.batocera_calls[-1] == (HDMI, PREFIXES)
    assert max(i for i, k in enumerate(kinds) if k == "remove_combo") < kinds.index("stop")


def test_stale_combos_of_an_earlier_run_are_removed(tmp_path):
    h = combo_harness(tmp_path)
    h.sinks.sink_list.append(Sink("padspeaker_tv_all", "old", 100, False))
    h.sinks.sink_list.append(Sink("padspeaker_all", "old", 100, False))
    h.sinks.default = HDMI
    h.present = [make_pad()]
    h.tick()
    removed = [e[1] for e in h.kinds("remove")]
    assert "padspeaker_tv_all" in removed and "padspeaker_all" in removed
    assert set(combos_of(h)) == {"padspeaker_tv_1122"}


def test_combo_create_failures_are_retried(tmp_path):
    h = combo_harness(tmp_path)
    original = h.sinks.create_combine_sink
    calls = []

    def flaky(name, description, slaves):
        calls.append(name)
        if len(calls) == 1:
            raise SinkError("module-combine-sink failed")
        return original(name, description, slaves)
    h.sinks.create_combine_sink = flaky
    h.present = [make_pad()]
    h.tick()
    assert combos_of(h) == {}
    for _ in range(4):
        h.tick()
    assert set(combos_of(h)) == {"padspeaker_tv_1122"}


def test_audio_server_errors_during_observation_do_not_crash(tmp_path):
    h = combo_harness(tmp_path)
    h.present = [make_pad()]
    h.sinks.fail_list = True
    h.tick()
    h.sinks.fail_list = False
    for _ in range(3):
        h.tick()
    assert set(combos_of(h)) == {"padspeaker_tv_1122"}


def test_combo_config_keys():
    c = DaemonConfig()
    assert c.combine_pads and c.combine_tv_pad and c.combine_tv_all
    assert c.tv_sink is None and c.tv_delay_ms == 0 and c.start_on_running is True
    c = DaemonConfig.from_dict({"combine_pads": False, "combine_tv_pad": False,
                                "combine_tv_all": False, "tv_sink": "alsa_output.hdmi",
                                "tv_delay_ms": 120, "start_on_running": False})
    assert (c.combine_pads, c.combine_tv_pad, c.combine_tv_all) == (False, False, False)
    assert c.tv_sink == "alsa_output.hdmi" and c.tv_delay_ms == 120
    assert c.start_on_running is False


@pytest.mark.parametrize("data, key", [
    ({"combine_pads": 1}, "combine_pads"), ({"combine_tv_pad": "yes"}, "combine_tv_pad"),
    ({"combine_tv_all": None}, "combine_tv_all"), ({"start_on_running": "no"}, "start_on_running"),
    ({"tv_sink": "has space"}, "tv_sink"), ({"tv_sink": 5}, "tv_sink"),
    ({"tv_delay_ms": -1}, "tv_delay_ms"), ({"tv_delay_ms": 99999}, "tv_delay_ms"),
    ({"tv_delay_ms": "x"}, "tv_delay_ms"),
])
def test_combo_config_validation(data, key):
    with pytest.raises(ConfigError, match=key):
        DaemonConfig.from_dict(data)


def test_tv_delay_is_documented_as_not_implemented(tmp_path, caplog):
    h = combo_harness(tmp_path, tv_delay_ms=100)
    h.present = [make_pad()]
    with caplog.at_level(logging.WARNING, logger="padspeaker"):
        h.tick()
        h.tick()
    warnings = [r.getMessage() for r in caplog.records if "tv_delay_ms" in r.getMessage()]
    assert len(warnings) == 1 and "not implemented" in warnings[0]


# --- extra_pad_ids ------------------------------------------------------------------

def test_extra_pad_ids_default_is_empty():
    assert DaemonConfig().extra_pad_ids == []
    assert DaemonConfig.from_dict({}).extra_pad_ids == []


def test_extra_pad_ids_are_normalised_and_deduplicated():
    c = DaemonConfig.from_dict({"extra_pad_ids": ["1234:abcd", " 1234:ABCD ", "2dc8:3106"]})
    assert c.extra_pad_ids == ["1234:ABCD", "2DC8:3106"]
    assert DaemonConfig.from_dict({"extra_pad_ids": []}).extra_pad_ids == []


@pytest.mark.parametrize("value", ["1234:abcd", "", None, [5], [""], ["1234"], ["1234:abc"], ["0005:1234:abcd"],
                                   ["zz:zz"], ["1234:abcd", 7], [["1234:abcd"]], {"1234:abcd": 1}])
def test_extra_pad_ids_must_be_a_list_of_vendor_product_ids(value):
    with pytest.raises(ConfigError) as info:
        DaemonConfig.from_dict({"extra_pad_ids": value})
    assert "extra_pad_ids" in str(info.value) and "VVVV:PPPP" in str(info.value)


def test_extra_pad_ids_has_a_sane_limit():
    with pytest.raises(ConfigError, match="at most"):
        DaemonConfig.from_dict({"extra_pad_ids": ["%04X:0001" % i for i in range(65)]})


def test_the_default_watcher_gets_the_extra_ids(tmp_path, monkeypatch):
    from padspeaker import daemon as daemon_module
    seen = {}

    class RecordingWatcher:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

        def poll(self):
            return [], []
    monkeypatch.setattr(daemon_module, "PadWatcher", RecordingWatcher)
    daemon_module.Daemon(DaemonConfig(extra_pad_ids=["1234:ABCD"], lock_file=None, status_file=None),
                         sinks=object())
    assert seen == {"extra_ids": ["1234:ABCD"]}


def test_a_pad_found_through_an_extra_id_is_logged_as_such(tmp_path, caplog):
    h = Harness(tmp_path, extra_pad_ids=["1234:ABCD"])
    h.present = [Pad("/dev/hidraw7", MAC1, "Clone Pad", "0005:1234:ABCD", None, True)]
    with caplog.at_level(logging.INFO, logger="padspeaker"):
        h.tick()
    assert any("extra_pad_ids" in r.getMessage() and "1234:ABCD" in r.getMessage() for r in caplog.records)
    assert [s.sink_name for s in h.streamers] == ["padspeaker_1122"]           # served like any other pad
