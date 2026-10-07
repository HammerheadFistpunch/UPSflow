import asyncio
import time
from types import SimpleNamespace
import pytest
from power_control import execute_power, set_verified


class Device:
    is_connected = True
    energy_backup_battery_level = 30

    def __init__(self, state, subsystem, initial=True, fail=None):
        self.state, self.subsystem, self.fail = state, subsystem, fail
        self.calls = []
        self.field = {"ac": "ac_ports", "dc": "dc_12v_port", "reserve": "energy_backup"}[subsystem]
        setattr(self, self.field, initial)

    async def write(self, enabled):
        self.calls.append(enabled)
        if enabled is self.fail:
            raise RuntimeError("BLE write failed")
        await asyncio.sleep(0)
        setattr(self, self.field, enabled)
        self.state.last_update = time.monotonic()
        self.state.power_updates[self.subsystem] = self.state.last_update

    enable_ac_ports = enable_dc_12v_port = enable_energy_backup = write


def state_for(subsystem, initial=True, fail=None):
    state = SimpleNamespace(key="network", stale=False, last_update=time.monotonic(), power_updates={subsystem:time.monotonic()})
    state.device = Device(state, subsystem, initial, fail)
    return state


@pytest.mark.parametrize("subsystem", ["ac", "dc", "reserve"])
@pytest.mark.parametrize("initial", [False, True])
def test_cycle_verifies_every_transition(subsystem, initial):
    state = state_for(subsystem, initial)
    result = asyncio.run(execute_power(state, subsystem, "CYCLE", 0))
    assert state.device.calls == ([False, True] if initial else [True, False, True])
    assert result["cycle_confirmed"] and result["final_state"] is True


def test_failed_off_attempt_restores_on_and_reports_failure():
    state = state_for("dc", fail=False)
    with pytest.raises(RuntimeError, match="BLE write failed"):
        asyncio.run(execute_power(state, "dc", "CYCLE", 0))
    assert state.device.calls == [False, True]


def test_failed_final_on_never_reports_completed_cycle():
    state = state_for("ac", fail=True)
    with pytest.raises(RuntimeError):
        asyncio.run(execute_power(state, "ac", "CYCLE", 0))


@pytest.mark.parametrize("bad", ["stale", "disconnected", "unknown"])
def test_bad_initial_telemetry_prevents_writes(bad):
    state = state_for("reserve")
    if bad == "stale": state.stale = True
    if bad == "disconnected": state.device.is_connected = False
    if bad == "unknown": state.device.energy_backup = None
    with pytest.raises((ValueError, ConnectionError)):
        asyncio.run(execute_power(state, "reserve", "CYCLE", 0))
    assert state.device.calls == []


def test_old_state_without_new_telemetry_cannot_confirm_write():
    state = state_for("dc")
    async def write(enabled): pass
    state.device.enable_dc_12v_port = write
    with pytest.raises(RuntimeError, match="not confirmed"):
        asyncio.run(set_verified(state, "dc", True, timeout_seconds=0.01))


def test_state_is_read_only():
    state = state_for("reserve", False)
    result = asyncio.run(execute_power(state, "reserve", "STATE"))
    assert result["final_state"] is False
    assert state.device.calls == []


def test_unknown_reserve_percentage_prevents_defaulting_to_30():
    state = state_for("reserve")
    state.device.energy_backup_battery_level = None
    with pytest.raises(ValueError, match="percentage is unknown"):
        asyncio.run(execute_power(state, "reserve", "ON"))
    assert state.device.calls == []


def test_other_heartbeat_does_not_confirm_control():
    state = state_for("ac")
    async def write(enabled):
        state.last_update = time.monotonic()
        state.power_updates["reserve"] = state.last_update
    state.device.enable_ac_ports = write
    with pytest.raises(RuntimeError, match="not confirmed"):
        asyncio.run(set_verified(state, "ac", True, timeout_seconds=0.01))
