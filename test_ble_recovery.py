import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from ble_recovery import BLEWatchdog, DeviceState
from power_control import execute_power


class Device:
    def __init__(self, *, connected=False, error=None):
        self.is_connected = connected
        self.error = error
        self.disabled = False
        self.disconnects = 0
        self.connects = 0

    def with_disabled_reconnect(self, value):
        self.disabled = value

    async def connect(self, user_id, max_attempts):
        assert self.disabled and user_id == 'user' and max_attempts == 3
        self.connects += 1
        self.is_connected = True

    async def wait_until_authenticated_or_error(self, **kwargs):
        return 'authenticated', self.error

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False


def watchdog(device=None, **options):
    device = device or Device()
    discover = AsyncMock(return_value={'AA:BB': (device, object())})
    hooks = []
    def install(state):
        assert state.device.connects == 0  # Listen before the first auth/telemetry packet.
        hooks.append(state.device)
    result = BLEWatchdog(discover, lambda dev, adv: dev, install,
                         'user', asyncio.Lock(), **options)
    result.hooks = hooks
    return result


def test_device_absent_at_startup_is_retried_when_it_returns():
    async def check():
        w = watchdog()
        state = DeviceState('server', address='aa:bb')
        w.discover.side_effect = [{}, {'AA:BB': (Device(), object())}]
        await w.recover(state)
        assert state.device is None and 'not found' in state.last_error
        assert state.recovery_attempts == 1
        await w.recover(state)
        assert state.device.is_connected and state.last_error is None
        assert state.last_update == 0  # Auth does not make cached data fresh.
    asyncio.run(check())


def test_dropped_device_is_replaced_with_fresh_discovery_and_authentication():
    async def check():
        old = Device()
        state = DeviceState('server', old, last_update=1, power_updates={'ac': 1}, address='aa:bb')
        w = watchdog()
        await w.recover(state)
        assert old.disconnects == 1
        assert state.device is w.hooks[0] and state.device is not old
        assert state.device.connects == 1 and state.device.disabled
        assert state.power_updates == {} and state.stale
    asyncio.run(check())


def test_live_link_without_telemetry_is_reset_after_grace_period():
    async def check():
        state = DeviceState('server', Device(connected=True), address='aa:bb',
                            connected_since=time.monotonic())
        w = watchdog(stale_seconds=60)
        assert not w.needs_recovery(state)
        state.connected_since -= 61
        assert w.needs_recovery(state)
        await w.recover(state)
        assert state.device.connects == 1
    asyncio.run(check())


def test_stale_connected_link_is_reset_but_healthy_link_is_untouched():
    async def check():
        w = watchdog()
        state = DeviceState('network', Device(connected=True), last_update=time.monotonic())
        await w.recover(state)
        assert state.device.disconnects == 0 and w.discover.await_count == 0
        state.last_update -= 61
        state.address = 'aa:bb'
        await w.recover(state)
        assert state.device.connects == 1
    asyncio.run(check())


def test_failed_auth_is_cleaned_up_and_keeps_retrying():
    async def check():
        device = Device(error=RuntimeError('bad auth'))
        w = watchdog(device)
        state = DeviceState('server', address='aa:bb')
        await w.recover(state)
        assert device.disconnects == 1 and state.device is None
        assert 'bad auth' in state.last_error and not state.recovering
        assert 0 < state.next_retry - time.monotonic() <= 5
    asyncio.run(check())


def test_scan_and_connect_hangs_are_bounded():
    async def check(stage):
        device = Device()
        w = watchdog(device, attempt_timeout=0.01)
        async def hang(*args, **kwargs):
            await asyncio.Event().wait()
        if stage == 'scan':
            w.discover.side_effect = hang
        else:
            device.connect = hang
        state = DeviceState('server', address='aa:bb')
        await w.recover(state)
        assert 'TimeoutError' in state.last_error
        assert state.device is None and not state.recovering
        assert not w.control_lock.locked()
    for stage in ('scan', 'connect'):
        asyncio.run(check(stage))


def test_failed_cleanup_must_finish_before_another_connection_is_created():
    async def check():
        old = Device(connected=True)
        old.disconnect = AsyncMock(side_effect=RuntimeError('transport stuck'))
        state = DeviceState('server', old, last_update=1, power_updates={'ac': 1})
        w = watchdog()
        await w.recover(state)
        assert state.device is old and w.discover.await_count == 0
        assert state.last_update == 0 and state.power_updates == {}
        assert w.needs_recovery(state)
    asyncio.run(check())


def test_disconnect_timeout_is_bounded_and_retryable():
    async def check():
        old = Device()
        async def hang():
            await asyncio.Event().wait()
        old.disconnect = hang
        w = watchdog(disconnect_timeout=0.01)
        state = DeviceState('server', old)
        await w.recover(state)
        assert 'TimeoutError' in state.last_error
        assert state.device is old and not state.recovering
    asyncio.run(check())


def test_recovery_waits_for_controls_and_rechecks_telemetry():
    async def check():
        w = watchdog()
        state = DeviceState('server', Device(connected=True), last_update=1)
        async with w.control_lock:
            task = asyncio.create_task(w.recover(state))
            await asyncio.sleep(0)
            assert w.discover.await_count == 0
            state.last_update = time.monotonic()
        await task
        assert w.discover.await_count == 0
    asyncio.run(check())


def test_cancellation_releases_locks_and_does_not_restart_connection():
    async def check():
        started = asyncio.Event()
        w = watchdog()
        async def scan(**kwargs):
            started.set()
            await asyncio.Event().wait()
        w.discover.side_effect = scan
        state = DeviceState('server', address='aa:bb')
        task = asyncio.create_task(w.run(state))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not w.control_lock.locked() and not w.scan_lock.locked()
        assert not state.recovering
    asyncio.run(check())


def test_retry_delay_caps_at_sixty_seconds_and_does_not_give_up():
    async def check():
        w = watchdog()
        w.discover.return_value = {}
        state = DeviceState('server', address='aa:bb')
        for delay in (5, 10, 20, 40, 60, 60, 60):
            before = time.monotonic()
            await w.recover(state)
            assert before + delay <= state.next_retry <= time.monotonic() + delay
        assert state.recovery_attempts == 7
    asyncio.run(check())


def test_one_missing_unit_does_not_disconnect_the_healthy_unit():
    async def check():
        w = watchdog()
        w.discover.return_value = {}
        healthy = DeviceState('network', Device(connected=True), last_update=time.monotonic())
        missing = DeviceState('server', address='aa:bb')
        await asyncio.gather(w.recover(missing), w.recover(healthy))
        assert healthy.device.disconnects == 0 and healthy.device.is_connected
    asyncio.run(check())


def test_controls_fail_cleanly_before_first_discovery():
    with pytest.raises(ConnectionError, match='not connected'):
        asyncio.run(execute_power(DeviceState('server'), 'ac', 'ON'))


def test_watchdog_scheduler_honors_backoff_and_resets_after_fresh_telemetry(monkeypatch):
    from types import SimpleNamespace
    import ble_recovery

    async def check():
        clock = [100.0]
        monkeypatch.setattr(ble_recovery, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        w = watchdog()
        w.discover.return_value = {}
        state = DeviceState('server', address='aa:bb')
        attempts = []
        async def tick(seconds):
            attempts.append(w.discover.await_count)
            if len(attempts) == 7:
                # A fresh connection must reset accumulated backoff.
                state.device = Device(connected=True)
                state.last_update = clock[0]
            if len(attempts) == 8:
                raise asyncio.CancelledError
            clock[0] += seconds
        monkeypatch.setattr(ble_recovery.asyncio, 'sleep', tick)
        with pytest.raises(asyncio.CancelledError):
            await w.run(state)
        assert attempts[:6] == [1, 1, 1, 1, 1, 2]
        assert state.recovery_attempts == 0 and state.next_retry == 0
        assert state.last_error is None
    asyncio.run(check())
