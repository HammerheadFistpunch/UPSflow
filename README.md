# UPSflow

UPSflow is a small Windows-native, read-only BLE monitor for two EcoFlow River 2 units. It is intended to become the telemetry source for the Keymaster/RFZ query chain.

The first phase deliberately does **not** modify RFZ, Keymaster, Docker, or the EcoFlow configuration. It only proves that the existing Windows host can maintain BLE connections to both River 2 units and read the telemetry we need.

## Telemetry

Each configured unit reports:

- BLE connection state
- Battery percentage
- AC input present/absent
- AC input voltage
- AC input watts
- Solar input watts
- Total input watts
- Output watts
- Telemetry age/staleness

### AC input detection

AC presence is based on the River 2's reported AC input voltage, not AC input watts. This is intentional: the UPS units can remain connected to utility AC while solar is supplying the load, so AC input power can be near zero even though utility power is still available.

The default AC-present threshold is 80 V. It can be changed with `ac_present_voltage` in `config.json`.

## Requirements

- Windows host with a working Bluetooth LE adapter
- Python 3.13 or newer
- Git (required by the pinned `ha-ef-ble` dependency)
- EcoFlow user ID used by the local BLE authentication
- Both River 2 units awake and within BLE range

The underlying BLE implementation is the community `rabits/ha-ef-ble` project, pinned to commit `89fa21c113f38640b65fdaa9329a918a14d9efd3`. That implementation explicitly supports the base River 2 and exposes River 2 AC input power and other telemetry.

## Initial test

From a PowerShell window:

```powershell
cd UPSflow
py -3.13 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

First scan without connecting:

```powershell
python upsflow.py scan
```

You should see each River 2's name, serial number, and Windows BLE address.

Copy the two addresses into a local `config.json`:

```powershell
Copy-Item config.example.json config.json
notepad config.json
```

Fill in the EcoFlow user ID and assign the units:

```json
{
  "user_id": "YOUR_ECOFLOW_USER_ID",
  "poll_seconds": 2,
  "ac_present_voltage": 80.0,
  "stale_seconds": 15,
  "devices": {
    "server": {
      "address": "..."
    },
    "network": {
      "address": "..."
    }
  }
}
```

Then:

```powershell
python upsflow.py monitor
```

## What to test first

1. Confirm both River 2s are discovered.
2. Confirm both authenticate and remain connected.
3. Confirm battery, output, and solar readings look sensible.
4. With utility AC present and solar carrying the load, verify `AC input: YES` even when `AC watts` is low or zero.
5. If practical, interrupt utility AC to one unit and verify it changes to `AC input: NO` while the battery continues supplying the load.
6. Restore AC and verify it returns to `YES`.

If AC voltage is not populated correctly on the River 2 firmware in use, the next step will be to capture the inverter heartbeat and adjust the parser. No RFZ integration should be added until this basic telemetry is trustworthy.

## Read-only by design

UPSflow does not implement EcoFlow control operations. It will never turn AC/DC/USB outputs on or off, change charging limits, or change the River 2 operating mode.
