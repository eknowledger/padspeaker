"""Tests for padspeaker.linkpolicy with a fake runner (no hcitool, no Bluetooth)."""
import logging
import subprocess

import pytest

from padspeaker.linkpolicy import BluetoothLink, disable_sniff

MAC = "aa:bb:cc:dd:11:22"


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Simulates hcitool: a mutable link policy that ``lp MAC POLICY`` changes."""

    def __init__(self, policy="RSWITCH SNIFF"):
        self.policy = policy
        self.calls = []
        self.set_works = True
        self.missing = False
        self.read_error = None
        self.disconnect_result = Result(0, "Successful disconnected\n")

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if self.missing:
            raise FileNotFoundError(2, "No such file", argv[0])
        if argv[0].endswith("bluetoothctl"):
            return self.disconnect_result
        assert argv[1] == "lp" and argv[2] == MAC
        if len(argv) == 3:
            if self.read_error:
                return Result(1, "", self.read_error)
            return Result(0, "Link policy settings: %s \n" % self.policy)
        if self.set_works:
            self.policy = " ".join(argv[3:])
        return Result(0 if self.set_works else 1, "", "" if self.set_works else "Operation not permitted")

    def sets(self):
        return [c for c, _ in self.calls if c[0].endswith("hcitool") and len(c) > 3]


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def test_sniff_is_removed_and_verified():
    runner = FakeRunner("RSWITCH SNIFF")
    result = disable_sniff(MAC, runner)
    assert result.ok and result.changed
    assert runner.sets() == [["hcitool", "lp", MAC, "RSWITCH"]]
    assert runner.policy == "RSWITCH"
    # the last call verifies by reading the policy back
    assert runner.calls[-1][0] == ["hcitool", "lp", MAC]
    assert all(isinstance(c[0], list) and c[1] > 0 for c in runner.calls)   # argv lists, timeouts


def test_nothing_is_changed_when_sniff_is_already_off():
    runner = FakeRunner("RSWITCH")
    result = disable_sniff(MAC, runner)
    assert result.ok and not result.changed
    assert runner.sets() == []


def test_unreadable_policy_still_tries_to_set_it():
    class Flaky(FakeRunner):
        first = True

        def __call__(self, argv, timeout):
            if len(argv) == 3 and self.first:
                self.first = False
                self.calls.append((list(argv), timeout))
                return Result(1, "", "Can't read")
            return super().__call__(argv, timeout)
    flaky = Flaky("RSWITCH SNIFF")
    result = disable_sniff(MAC, flaky)
    assert result.ok and result.changed and flaky.policy == "RSWITCH"


def test_failed_set_is_reported_not_raised():
    runner = FakeRunner("RSWITCH SNIFF")
    runner.set_works = False
    result = disable_sniff(MAC, runner)
    assert not result.ok and not result.changed
    assert "Operation not permitted" in result.detail or "SNIFF" in result.detail


def test_verification_failure_is_not_ok():
    runner = FakeRunner("RSWITCH SNIFF")

    def stubborn(argv, timeout):
        runner.calls.append((list(argv), timeout))
        if len(argv) == 3:
            return Result(0, "Link policy settings: RSWITCH SNIFF \n")
        return Result(0)
    result = disable_sniff(MAC, stubborn)
    assert not result.ok and "still" in result.detail.lower()


def test_missing_hcitool_warns_once_and_never_raises(caplog):
    runner = FakeRunner()
    runner.missing = True
    link = BluetoothLink(runner=runner)
    with caplog.at_level(logging.DEBUG, logger="padspeaker"):
        first = link.disable_sniff(MAC)
        second = link.disable_sniff(MAC)
    assert not first.ok and not second.ok
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "hcitool" in warnings[0].getMessage()
    assert len(runner.calls) == 1               # not even tried again


def test_timeouts_and_oserrors_never_raise():
    def slow(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    def broken(argv, timeout):
        raise PermissionError(13, "denied")
    assert not disable_sniff(MAC, slow).ok
    assert not disable_sniff(MAC, broken).ok


def test_unexpected_runner_exception_never_raises():
    def boom(argv, timeout):
        raise RuntimeError("weird")
    assert not disable_sniff(MAC, boom).ok


@pytest.mark.parametrize("mac", ["", "nonsense", "aa:bb:cc:dd:11", "aa:bb:cc:dd:11:22; reboot",
                                 "-h", "aa:bb:cc:dd:11:zz"])
def test_invalid_addresses_never_reach_the_runner(mac):
    runner = FakeRunner()
    result = disable_sniff(mac, runner)
    assert not result.ok and runner.calls == []


def test_uppercase_mac_is_accepted_and_normalised():
    runner = FakeRunner("RSWITCH SNIFF")
    assert disable_sniff(MAC.upper(), runner).ok
    assert runner.sets() == [["hcitool", "lp", MAC, "RSWITCH"]]


def test_info_log_only_when_something_changed(caplog):
    link = BluetoothLink(runner=FakeRunner("RSWITCH SNIFF"))
    with caplog.at_level(logging.INFO, logger="padspeaker"):
        link.disable_sniff(MAC)
        link.disable_sniff(MAC)                   # already fine now
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1 and "sniff" in infos[0].getMessage().lower()


def test_repeated_failures_are_not_logged_as_warnings_every_time(caplog):
    runner = FakeRunner("RSWITCH SNIFF")
    runner.set_works = False
    link = BluetoothLink(runner=runner)
    with caplog.at_level(logging.DEBUG, logger="padspeaker"):
        for _ in range(5):
            link.disable_sniff(MAC)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


# --- disconnect ----------------------------------------------------------------------

def test_disconnect_runs_bluetoothctl():
    runner = FakeRunner()
    link = BluetoothLink(runner=runner, clock=Clock())
    assert link.disconnect(MAC) is True
    assert runner.calls[0][0] == ["bluetoothctl", "disconnect", MAC]


def test_disconnect_is_rate_limited_per_pad():
    runner = FakeRunner()
    clock = Clock()
    link = BluetoothLink(runner=runner, clock=clock, disconnect_min_interval=60)
    assert link.disconnect(MAC) is True
    clock.now += 10
    assert link.disconnect(MAC) is False
    assert len(runner.calls) == 1
    assert link.disconnect("aa:bb:cc:dd:ee:ff") is True        # another pad is independent
    clock.now += 61
    assert link.disconnect(MAC) is True


def test_disconnect_failure_and_missing_tool_never_raise(caplog):
    runner = FakeRunner()
    runner.disconnect_result = Result(1, "", "Failed to disconnect")
    link = BluetoothLink(runner=runner, clock=Clock())
    assert link.disconnect(MAC) is False
    runner2 = FakeRunner()
    runner2.missing = True
    with caplog.at_level(logging.WARNING, logger="padspeaker"):
        assert BluetoothLink(runner=runner2, clock=Clock()).disconnect(MAC) is False
    assert any("bluetoothctl" in r.getMessage() for r in caplog.records)


def test_disconnect_rejects_bad_addresses():
    runner = FakeRunner()
    assert BluetoothLink(runner=runner).disconnect("not a mac") is False
    assert runner.calls == []


def test_tools_present():
    link = BluetoothLink(runner=FakeRunner(), which=lambda name: "/usr/bin/" + name
                         if name == "hcitool" else None)
    assert link.tools() == {"hcitool": True, "bluetoothctl": False}
