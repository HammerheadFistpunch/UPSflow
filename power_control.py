"""Verified EcoFlow toggles; callers serialize all device writes."""
import asyncio
import time

CONTROLS = {
    "ac": ("ac_ports", "enable_ac_ports"),
    "dc": ("dc_12v_port", "enable_dc_12v_port"),
    "reserve": ("energy_backup", "enable_energy_backup"),
}


def power_state(device, subsystem):
    raw = getattr(device, CONTROLS[subsystem][0], None)
    raw = getattr(raw, "value", raw)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    return None


def checked_state(state, subsystem):
    if not state.device.is_connected:
        raise ConnectionError(f"{state.key} is not connected")
    updated = state.power_updates.get(subsystem, 0)
    if state.stale or not updated or time.monotonic() - updated > 15:
        raise ValueError(f"{state.key} {subsystem.upper()} telemetry is stale")
    current = power_state(state.device, subsystem)
    if current is None:
        raise ValueError(f"{state.key} {subsystem.upper()} state is unknown")
    return current


async def set_verified(state, subsystem, enabled, timeout_seconds=10):
    baseline = state.power_updates.get(subsystem, 0)
    result = await getattr(state.device, CONTROLS[subsystem][1])(enabled)
    if result is False:
        raise ValueError(f"{state.key} {subsystem.upper()} rejected by device")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not state.device.is_connected:
            raise ConnectionError(f"{state.key} disconnected during control")
        if state.power_updates.get(subsystem, 0) > baseline and not state.stale and power_state(state.device, subsystem) is enabled:
            return
        await asyncio.sleep(0.25)
    raise RuntimeError(f"{state.key} {subsystem.upper()} {'ON' if enabled else 'OFF'} not confirmed")


async def execute_power(state, subsystem, action, delay_seconds=5):
    subsystem, action = subsystem.lower(), action.upper()
    if subsystem not in CONTROLS or action not in {"ON", "OFF", "STATE", "CYCLE", "RESET"}:
        raise ValueError("Unsupported power subsystem or action")
    initial = checked_state(state, subsystem)
    result = {"status": "ok", "device": state.key, "control": subsystem,
              "initial_state": initial, "verified": True}
    if action == "STATE":
        return dict(result, final_state=initial)
    if subsystem == "reserve":
        reserve = getattr(state.device, "energy_backup_battery_level", None)
        if isinstance(reserve, bool) or not isinstance(reserve, (int, float)) or not 0 <= reserve <= 100:
            raise ValueError(f"{state.key} reserve percentage is unknown")
    if action in {"ON", "OFF"}:
        await set_verified(state, subsystem, action == "ON")
        return dict(result, final_state=action == "ON")
    # A cycle always starts ON and ends ON, including when initially OFF.
    if not initial:
        await set_verified(state, subsystem, True)
    try:
        await set_verified(state, subsystem, False)
        await asyncio.sleep(delay_seconds)
    except Exception:
        # Attempt restoration even when OFF confirmation fails; retain the failure.
        try:
            await set_verified(state, subsystem, True)
        except Exception as restore_error:
            raise RuntimeError(f"Cycle failed; ON restoration unconfirmed: {restore_error}") from restore_error
        raise
    await set_verified(state, subsystem, True)
    return dict(result, off_seconds=delay_seconds, final_state=True, cycle_confirmed=True)
