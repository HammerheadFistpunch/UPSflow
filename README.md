# UPSflow

UPSflow is a small Windows-native, read-only BLE monitor for two EcoFlow River 2 units. It is the telemetry source for the Keymaster/RFZ query chain.

UPSflow maintains the BLE telemetry stream continuously and exposes the latest cached state through a small local HTTP API. The console `poll_seconds` setting only controls how often the display redraws.

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

AC presence is based on the River 2's reported AC input voltage, not AC input watts. This is intentional: the UPS units can remain connected to utility AC while solar is supplying the load, so AC input power can be near zero even though utility AC is still available.

The default AC-present threshold is 80 V. It can be changed with `ac_present_voltage` in `config.json`.

## Requirements

- Windows host with a working Bluetooth LE adapter
- Python 3.13 or newer
- Git (required by the pinned `ha-ef-ble` dependency)
- EcoFlow user ID used by the local BLE authentication
- Both River 2 units awake and within BLE range
- NSSM, if installing UPSflow as a Windows service

The underlying BLE implementation is the community `rabits/ha-ef-ble` project, pinned to commit `89fa21c113f38640b65fdaa9329a918a14d9efd3`. That implementation explicitly supports the base River 2 and exposes River 2 AC input power and other telemetry.

## Initial setup

From a PowerShell window:

```powershell
cd UPSflow
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
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

Fill in the EcoFlow user ID and assign the units. The API listens on TCP port 5005 by default:

```json
{
  "user_id": "YOUR_ECOFLOW_USER_ID",
  "poll_seconds": 2,
  "ac_present_voltage": 80.0,
  "stale_seconds": 15,
  "http_host": "0.0.0.0",
  "http_port": 5005,
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

Run the monitor directly for testing:

```powershell
python upsflow.py monitor
```

## Local Web GUI

UPSflow serves a basic read-only dashboard from the same HTTP server used by Keymaster. No second BLE connection or separate web service is required.

Open this in a browser on the Windows host:

```text
http://localhost:5005/
```

The dashboard shows both UPS units, BLE state, battery, AC presence/voltage/power, solar, total input, output, telemetry age, and errors. It refreshes automatically every 2 seconds.

The existing endpoints remain available at `/health` and `/v1/telemetry`.

The older Tkinter desktop viewer can still be run with `python upsflow_gui.py`, but the browser dashboard is the recommended local display.

## Windows service with NSSM

The recommended service arrangement is:

```text
River 2s --BLE--> UPSflow (NSSM service) --HTTP :5005--> Keymaster
                                      \
                                       \--> local GUI
```

NSSM runs the existing `python upsflow.py monitor` process continuously. The GUI is a separate desktop application and can be opened or closed independently.

Install NSSM so `nssm.exe` is in PATH, or pass its full path to the installer. From an **Administrator PowerShell** in the UPSflow directory:

```powershell
.\install-service.ps1
```

If NSSM is not in PATH:

```powershell
.\install-service.ps1 -NssmPath C:\path\to\nssm.exe
```

Then start the service:

```powershell
Start-Service UPSflow
Get-Service UPSflow
```

The service is configured for automatic startup and automatic restart after an unexpected process exit. Its Python console refresh output is discarded because the GUI provides the human-readable display. Python logging is retained in:

```text
logs\service-error.log
```

### Moving UPSflow

The installer derives all application paths from `$PSScriptRoot`, so the UPSflow directory itself can live anywhere. Windows services, however, store the resolved executable/script paths when the service is registered; they cannot automatically follow a directory that is subsequently moved.

After moving the entire UPSflow directory, open **Administrator PowerShell** in its new location and run:

```powershell
.\reinstall-service.ps1
Start-Service UPSflow
```

The relocation helper invokes the installer from the new directory, causing NSSM to register the service with the new paths. No path inside the script needs to be edited.

To remove the service without deleting UPSflow files or logs:

```powershell
.\remove-service.ps1
```

### Bluetooth service account

The installer uses the Windows LocalSystem account by default. If Windows/Bleak does not permit the service to access the River 2 BLE devices under LocalSystem, configure the NSSM service to run under the same Windows user account that successfully runs `upsflow.py monitor` interactively. The application itself does not require an interactive GUI session.

## Telemetry API

`GET /health` returns a simple service-health response.

`GET /v1/telemetry` returns the latest cached telemetry for the configured `server` and `network` River 2s. It is read-only and does not expose EcoFlow credentials or control operations.

The Keymaster container is expected to reach this API at `http://host.docker.internal:5005`.

## What to test first

1. Confirm both River 2s are discovered.
2. Confirm both authenticate and remain connected.
3. Confirm battery, output, and solar readings look sensible.
4. With utility AC present and solar carrying the load, verify `AC input: YES` even when `AC watts` is low or zero.
5. If practical, interrupt utility AC to one unit and verify it changes to `AC input: NO` while the battery continues supplying the load.
6. Restore AC and verify it returns to `YES`.
7. With UPSflow running, verify `http://localhost:5005/health` and `http://localhost:5005/v1/telemetry` from the Windows host.
8. Start the GUI and confirm it follows the same telemetry without interrupting the service.
9. Reboot Windows and confirm the UPSflow service starts automatically.

## Read-only by design

UPSflow does not implement EcoFlow control operations. It will never turn AC/DC/USB outputs on or off, change charging limits, or change the River 2 operating mode.
