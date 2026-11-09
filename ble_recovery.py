"""Per-device BLE recovery, independent of the EcoFlow protocol implementation."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

LOG = logging.getLogger("upsflow.recovery")


@dataclass
class DeviceState:
    key: str
    device: Any = None
    last_update: float = 0.0
    last_error: str | None = None
    power_updates: dict[str, float] = field(default_factory=dict)
    address: str = ""
    recovering: bool = False
    recovery_attempts: int = 0
    next_retry: float = 0.0
    connected_since: float = 0.0

    @property
    def stale(self) -> bool:
        return self.last_update == 0 or time.monotonic() - self.last_update > 15


class BLEWatchdog:
    def __init__(self, discover, make_device, install_telemetry, user_id, control_lock,
                 stale_seconds=60, attempt_timeout=60, disconnect_timeout=10):
        self.discover = discover
        self.make_device = make_device
        self.install_telemetry = install_telemetry
        self.user_id = user_id
        self.control_lock = control_lock
        self.stale_seconds = stale_seconds
        self.attempt_timeout = attempt_timeout
        self.disconnect_timeout = disconnect_timeout
        # Windows discovery is shared, but an absent unit must not block a healthy one.
        self.scan_lock = asyncio.Lock()

    def needs_recovery(self, state):
        if state.device is None or not state.device.is_connected:
            return True
        baseline = state.last_update or state.connected_since
        return not baseline or time.monotonic() - baseline > self.stale_seconds

    async def disconnect(self, state):
        state.last_update = 0.0
        state.connected_since = 0.0
        state.power_updates.clear()
        # Call even for a down link: eflib also cancels its pending tasks here.
        if state.device is not None:
            await asyncio.wait_for(state.device.disconnect(), self.disconnect_timeout)
            state.device = None

    async def recover(self, state):
        async with self.control_lock:
            # A pending control may have received telemetry while we waited.
            if not self.needs_recovery(state):
                return
            state.recovering = True
            state.recovery_attempts += 1
            try:
                async with asyncio.timeout(self.attempt_timeout):
                    await self.disconnect(state)
                    async with self.scan_lock:
                        devices = await self.discover(return_adv=True, timeout=5.0)
                    match = next((pair for address, pair in devices.items()
                                  if address.lower() == state.address.lower()), None)
                    if match is None:
                        raise ConnectionError(f"{state.key} not found at {state.address}")
                    device = self.make_device(*match)
                    if device is None:
                        raise ConnectionError(f"{state.key} is not advertising as a base River 2")
                    state.device = device
                    # Exactly one retry owner; do not race eflib's reconnect task.
                    device.with_disabled_reconnect(True)
                    self.install_telemetry(state)
                    await device.connect(self.user_id, max_attempts=3)
                    _, error = await device.wait_until_authenticated_or_error(return_exc=True)
                    if error is not None:
                        raise error
                    if not device.is_connected:
                        raise ConnectionError("BLE disconnected before authentication completed")
                    state.connected_since = time.monotonic()
                    state.last_error = None
                    LOG.info("%s BLE authenticated; waiting for fresh telemetry", state.key)
            except Exception as exc:
                state.last_error = f"{type(exc).__name__}: {exc}"
                LOG.warning("%s recovery failed: %s", state.key, state.last_error)
                try:
                    await self.disconnect(state)
                except Exception:
                    # Retain the instance so the next attempt retries cleanup first.
                    LOG.exception("%s BLE cleanup failed", state.key)
            finally:
                state.recovering = False
                delay = min(60, 5 * 2 ** min(state.recovery_attempts - 1, 4))
                state.next_retry = time.monotonic() + delay

    async def run(self, state):
        while True:
            if self.needs_recovery(state):
                if time.monotonic() >= state.next_retry:
                    await self.recover(state)
            elif state.last_update:
                # Authentication alone does not prove the telemetry stream recovered.
                state.recovery_attempts = 0
                state.next_retry = 0.0
                state.last_error = None
            await asyncio.sleep(1)
