from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

# ha-ef-ble is packaged as a Home Assistant integration, so importing
# custom_components.ef_ble would execute its Home Assistant-dependent __init__.py.
# UPSflow only needs the standalone eflib package. The dependency is installed
# editable so its source tree remains available inside the virtual environment.
def _load_eflib_path() -> None:
    repo_root = Path(__file__).resolve().parent
    candidates = [
        repo_root / ".venv" / "src" / "ha-ef-ble" / "custom_components" / "ef_ble",
        Path(sys.prefix) / "src" / "ha-ef-ble" / "custom_components" / "ef_ble",
    ]
    for path in candidates:
        if (path / "eflib" / "__init__.py").is_file():
            sys.path.insert(0, str(path))
            return
    raise RuntimeError(
        "Could not locate the ha-ef-ble eflib source package. "
        "Run 'python -m pip install -r requirements.txt' in the UPSflow virtual environment."
    )


_load_eflib_path()

import eflib
from eflib.devices import river2
from eflib.props.raw_data_field import raw_field
from eflib.props.transforms import pdiv

LOG = logging.getLogger("upsflow")
DEFAULT_CONFIG = Path("config.json")
RIVER2_PREFIXES = (b"R601", b"R603")


class UPSFlowRiver2(river2.Device):
    """River 2 with the AC input telemetry needed by UPSflow."""

    # The upstream River 2 implementation exposes AC input watts, but not the
    # inverter's AC input voltage/current. Voltage is important here because a
    # River 2 can be connected to AC while deliberately drawing almost no AC
    # power when solar is carrying the load.
    ac_input_voltage = raw_field(river2.pb_inv.ac_in_vol, pdiv(1000, 2)).default_when_missing(0)
    ac_input_current = raw_field(river2.pb_inv.ac_in_amp, pdiv(1000, 2)).default_when_missing(0)


@dataclass
class DeviceConfig:
    key: str
    address: str


@dataclass
class DeviceState:
    key: str
    device: UPSFlowRiver2
    last_update: float = 0.0
    last_error: str | None = None

    @property
    def stale(self) -> bool:
        return self.last_update == 0 or (time.monotonic() - self.last_update) > 15


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"Missing {path}. Copy config.example.json to config.json and fill in your EcoFlow user ID and BLE addresses."
        )
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def river2_from_advertisement(
    ble_dev: BLEDevice, adv_data: AdvertisementData
) -> UPSFlowRiver2 | None:
    sn = eflib.sn_from_advertisement(adv_data)
    if sn is None or sn[:4] not in RIVER2_PREFIXES:
        return None
    return UPSFlowRiver2(ble_dev, adv_data, sn.decode("ascii"))


async def scan() -> None:
    print("Scanning for EcoFlow River 2 BLE devices...\n")
    devices = await BleakScanner.discover(return_adv=True)
    found = 0

    for address, (ble_dev, adv_data) in devices.items():
        device = river2_from_advertisement(ble_dev, adv_data)
        if device is None:
            continue
        found += 1
        print(f"Name:    {device.name}")
        print(f"Serial:  {device.serial_number}")
        print(f"Address: {address}")
        print()

    if not found:
        print("No River 2 devices found.")
        print("Make sure Bluetooth is enabled, both units are awake, and the Windows host is nearby.")


async def connect_device(
    state: DeviceState, user_id: str, max_attempts: int = 3
) -> None:
    try:
        LOG.info("Connecting %s (%s)", state.key, state.device.address)
        await state.device.connect(user_id, max_attempts=max_attempts)
        status, error = await state.device.wait_until_authenticated_or_error(
            return_exc=True
        )
        if error is not None:
            raise error
        LOG.info("%s authenticated: %s", state.key, status)

        # The upstream library parses pushed heartbeat packets. This callback is
        # deliberately only used to timestamp fresh telemetry; UPSflow never sends
        # a control packet to the River 2.
        parser = eflib.get_fixed_length_coding_device(state.device)
        if parser is not None:
            parser.on_message_processed(lambda _message: setattr(state, "last_update", time.monotonic()))

    except Exception as exc:  # BLE libraries expose several platform-specific exception types.
        state.last_error = f"{type(exc).__name__}: {exc}"
        LOG.exception("%s connection failed", state.key)


def value(device: UPSFlowRiver2, name: str, default: float | int = 0) -> float | int:
    result = getattr(device, name, None)
    return default if result is None else result


def format_device(state: DeviceState, stale_seconds: int) -> list[str]:
    d = state.device
    battery = value(d, "battery_level", 0)
    ac_w = value(d, "ac_input_power", 0)
    ac_v = value(d, "ac_input_voltage", 0)
    solar_w = value(d, "solar_input_power", 0)
    total_in = value(d, "input_power", 0)
    output = value(d, "output_power", 0)

    age = "never"
    if state.last_update:
        age = f"{time.monotonic() - state.last_update:.0f}s"

    threshold = float(CONFIG.get("ac_present_voltage", 80.0))
    ac_present = float(ac_v) >= threshold
    stale = state.last_update == 0 or (time.monotonic() - state.last_update) > stale_seconds

    lines = [state.key.upper()]
    lines.append(f"  BLE:       {'Connected' if d.is_connected else 'Disconnected'}")
    lines.append(f"  Battery:   {battery:.1f}%")
    lines.append(f"  AC input:  {'YES' if ac_present else 'NO'}")
    lines.append(f"  AC volts:  {ac_v:.1f} V")
    lines.append(f"  AC watts:  {ac_w:.0f} W")
    lines.append(f"  Solar:     {solar_w:.0f} W")
    lines.append(f"  Total in:  {total_in:.0f} W")
    lines.append(f"  Output:    {output:.0f} W")
    lines.append(f"  Telemetry: {'STALE' if stale else age + ' ago'}")
    if state.last_error:
        lines.append(f"  Error:     {state.last_error}")
    return lines


async def monitor(config: dict[str, Any], config_path: Path) -> None:
    global CONFIG
    CONFIG = config

    user_id = os.getenv("ECOFLOW_USER_ID") or str(config.get("user_id", "")).strip()
    if not user_id or user_id == "YOUR_ECOFLOW_USER_ID":
        raise SystemExit(
            "EcoFlow user ID is required. Put it in config.json or set ECOFLOW_USER_ID."
        )

    device_entries = config.get("devices", {})
    states: list[DeviceState] = []
    for key in ("server", "network"):
        entry = device_entries.get(key, {})
        address = str(entry.get("address", "")).strip()
        if not address or address.startswith("BLE_ADDRESS_"):
            LOG.warning("No BLE address configured for %s; skipping", key)
            continue

        scanner_devices = await BleakScanner.discover(return_adv=True)
        match = scanner_devices.get(address)
        if match is None:
            # Windows BLE addresses can change representation; scan by normalized address.
            for candidate_address, pair in scanner_devices.items():
                if candidate_address.lower() == address.lower():
                    match = pair
                    address = candidate_address
                    break

        if match is None:
            LOG.error("Could not find configured %s device at %s", key, address)
            continue

        ble_dev, adv_data = match
        device = river2_from_advertisement(ble_dev, adv_data)
        if device is None:
            LOG.error("Configured %s device is not advertising as a base River 2", key)
            continue
        states.append(DeviceState(key, device))

    if not states:
        raise SystemExit("No configured River 2 devices could be found. Run 'python upsflow.py scan'.")

    await asyncio.gather(*(connect_device(state, user_id) for state in states))

    poll_seconds = max(1, int(config.get("poll_seconds", 2)))
    stale_seconds = max(poll_seconds * 2, int(config.get("stale_seconds", 15)))

    try:
        while True:
            print("\x1b[2J\x1b[H", end="")
            print("UPSflow — EcoFlow River 2 Monitor")
            print("=" * 43)
            print()
            for state in states:
                for line in format_device(state, stale_seconds):
                    print(line)
                print()
            print(f"Refresh: {poll_seconds}s   Config: {config_path}")
            print("Read-only mode: UPSflow sends no EcoFlow control commands.")
            await asyncio.sleep(poll_seconds)
    finally:
        await asyncio.gather(
            *(state.device.disconnect() for state in states if state.device.is_connected),
            return_exceptions=True,
        )


CONFIG: dict[str, Any] = {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Local EcoFlow River 2 telemetry for Keymaster/RFZ")
    parser.add_argument("command", choices=("scan", "monitor"), help="Scan for River 2 devices or monitor configured devices")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Configuration file (default: config.json)")
    parser.add_argument("--debug", action="store_true", help="Enable BLE debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "scan":
        asyncio.run(scan())
    else:
        asyncio.run(monitor(load_config(args.config), args.config))


if __name__ == "__main__":
    if sys.platform != "win32":
        LOG.warning("UPSflow is intended for the Windows host's native Bluetooth stack.")
    main()
