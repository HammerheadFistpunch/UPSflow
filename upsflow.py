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

        parser = eflib.get_fixed_length_coding_device(state.device)
        if parser is not None:
            parser.on_message_processed(lambda _message: setattr(state, "last_update", time.monotonic()))

    except Exception as exc:
        state.last_error = f"{type(exc).__name__}: {exc}"
        LOG.exception("%s connection failed", state.key)


def value(device: UPSFlowRiver2, name: str, default: float | int = 0) -> float | int:
    result = getattr(device, name, None)
    return default if result is None else result


def dc_mode_label(mode: Any) -> str:
    """Return the stable display/API label for eflib's DCMode value."""
    if mode is None:
        return "UNKNOWN"
    label = getattr(mode, "name", None)
    if label:
        return str(label).upper()
    text = str(mode).upper()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def format_device(state: DeviceState, stale_seconds: int) -> list[str]:
    d = state.device
    battery = value(d, "battery_level", 0)
    ac_w = value(d, "ac_input_power", 0)
    ac_v = value(d, "ac_input_voltage", 0)
    dc_w = value(d, "dc_port_input_power", 0)
    dc_mode = dc_mode_label(getattr(d, "dc_mode", None))
    total_in = value(d, "input_power", 0)
    dc12_w = value(d, "dc12v_output_power", 0)
    usb_w = float(value(d, "usba_output_power", 0)) + float(value(d, "usbc_output_power", 0))
    output = value(d, "output_power", 0)
    net = float(total_in) - float(output)

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
    lines.append(f"  AC watts:  {ac_w:.0f} W")
    lines.append(f"  DC In:     {dc_w:.0f} W")
    lines.append(f"  DC State:  {dc_mode}")
    lines.append(f"  Total in:  {total_in:.0f} W")
    lines.append(f"  12V out:   {dc12_w:.0f} W")
    lines.append(f"  USB out:   {usb_w:.0f} W")
    lines.append(f"  Total out: {output:.0f} W")
    lines.append(f"  Net power: {net:+.0f} W")
    lines.append(f"  Telemetry: {'STALE' if stale else age + ' ago'}")
    if state.last_error:
        lines.append(f"  Error:     {state.last_error}")
    return lines


TELEMETRY_FIELDS = (
    "battery_level",
    "input_power",
    "output_power",
    "cell_temperature",
    "ac_input_power",
    "ac_output_power",
    "ac_input_voltage",
    "ac_input_current",
    "ac_ports",
    "ac_xboost",
    "ac_charging_speed",
    "ac_charging_power_min",
    "ac_charging_power_max",
    "dc_port_input_power",
    "solar_input_power",
    "car_input_power",
    "dc_mode",
    "dc_12v_port",
    "dc12v_output_power",
    "dc_charging_max_amps",
    "dc_charging_current_max",
    "usbc_output_power",
    "usba_output_power",
    "energy_backup",
    "energy_backup_battery_level",
    "battery_charge_limit_min",
    "battery_charge_limit_max",
    "remaining_time_charging",
    "remaining_time_discharging",
)


def json_value(raw: Any) -> Any:
    """Convert eflib values/enums into JSON-safe native values without losing raw telemetry."""
    if raw is None or isinstance(raw, (str, int, float, bool)):
        return raw
    if isinstance(raw, (list, tuple)):
        return [json_value(item) for item in raw]
    if isinstance(raw, dict):
        return {str(key): json_value(item) for key, item in raw.items()}
    name = getattr(raw, "name", None)
    if name is not None:
        return str(name)
    return str(raw)


def dc_12v_state(device: UPSFlowRiver2) -> bool | None:
    """Return the native 12V DC port state without conflating missing with OFF."""
    raw = getattr(device, "dc_12v_port", None)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    raw_value = getattr(raw, "value", None)
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)) and raw_value in (0, 1):
        return bool(raw_value)
    return None


def telemetry_device(state: DeviceState, stale_seconds: int) -> dict[str, Any]:
    d = state.device
    battery = float(value(d, "battery_level", 0))
    dc_12v_on = dc_12v_state(d)
    ac_w = float(value(d, "ac_input_power", 0))
    ac_v = float(value(d, "ac_input_voltage", 0))
    ac_out_w = float(value(d, "ac_output_power", 0))
    dc_w = float(value(d, "dc_port_input_power", 0))
    dc_mode = dc_mode_label(getattr(d, "dc_mode", None))
    total_in = float(value(d, "input_power", 0))
    dc12_w = float(value(d, "dc12v_output_power", 0))
    usba_w = float(value(d, "usba_output_power", 0))
    usbc_w = float(value(d, "usbc_output_power", 0))
    usb_w = usba_w + usbc_w
    output = float(value(d, "output_power", 0))
    net = total_in - output
    age = None if state.last_update == 0 else max(0.0, time.monotonic() - state.last_update)
    threshold = float(CONFIG.get("ac_present_voltage", 80.0))
    stale = age is None or age > stale_seconds

    raw = {field: json_value(getattr(d, field, None)) for field in TELEMETRY_FIELDS}
    return {
        "connected": bool(d.is_connected),
        "name": json_value(getattr(d, "name", None)),
        "serial_number": json_value(getattr(d, "serial_number", None)),
        "address": json_value(getattr(d, "address", None)),
        "battery_percent": battery,
        "ac_present": ac_v >= threshold,
        "ac_voltage": ac_v,
        "ac_watts": ac_w,
        "ac_output_watts": ac_out_w,
        "dc_in_watts": dc_w,
        "dc_state": dc_mode,
        "dc_12v_port_on": dc_12v_on,
        "dc_enabled": dc_12v_on,
        "total_input_watts": total_in,
        "dc12v_output_watts": dc12_w,
        "usba_output_watts": usba_w,
        "usbc_output_watts": usbc_w,
        "usb_output_watts": usb_w,
        "output_watts": output,
        "net_watts": net,
        "telemetry_age_seconds": age,
        "stale": stale,
        "error": state.last_error,
        "raw_telemetry": raw,
    }


def find_device(states: list[DeviceState], key: str) -> DeviceState | None:
    return next((state for state in states if state.key == key), None)


async def control_dc_port(
    states: list[DeviceState], key: str, enabled: bool
) -> dict[str, Any]:
    state = find_device(states, key)
    if state is None:
        raise KeyError(f"device not configured: {key}")
    if not state.device.is_connected:
        raise ConnectionError(f"{key} is not connected")
    await state.device.enable_dc_12v_port(enabled)
    return {
        "status": "ok",
        "device": key,
        "control": "dc_12v_port",
        "requested": enabled,
    }


async def wait_for_dc_state(
    state: DeviceState, expected: bool, timeout_seconds: float = 10.0
) -> bool:
    """Wait for the device's native DC state to reflect an expected value."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        current = dc_12v_state(state.device)
        if current is expected:
            return True
        await asyncio.sleep(0.25)
    return False


async def reset_dc_port(
    states: list[DeviceState], key: str, delay_seconds: float = 5.0
) -> dict[str, Any]:
    """Reset DC: verify current state, force OFF, verify, wait, then force ON and verify."""
    state = find_device(states, key)
    if state is None:
        raise KeyError(f"device not configured: {key}")
    if not state.device.is_connected:
        raise ConnectionError(f"{key} is not connected")
    if state.stale:
        raise ValueError(f"{key} DC telemetry is stale")

    current = dc_12v_state(state.device)
    if current is None:
        raise ValueError(f"{key} DC state is unknown")

    LOG.info("DC reset %s: initial state=%s", key, current)
    await state.device.enable_dc_12v_port(False)
    if not await wait_for_dc_state(state, False):
        raise RuntimeError(f"{key} DC OFF state was not confirmed")

    await asyncio.sleep(delay_seconds)

    await state.device.enable_dc_12v_port(True)
    if not await wait_for_dc_state(state, True):
        raise RuntimeError(f"{key} DC ON state was not confirmed")

    LOG.info("DC reset %s complete: initial=%s final=True", key, current)
    return {
        "status": "ok",
        "device": key,
        "control": "dc_12v_port_reset",
        "initial_state": current,
        "off_seconds": delay_seconds,
        "final_state": True,
    }


def telemetry_snapshot(
    states: list[DeviceState], stale_seconds: int, poll_seconds: int
) -> dict[str, Any]:
    return {
        "service": "UPSflow",
        "read_only": False,
        "poll_seconds": poll_seconds,
        "stale_seconds": stale_seconds,
        "devices": {state.key: telemetry_device(state, stale_seconds) for state in states},
    }


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>UPSflow</title>
<style>
body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f3f4f6;color:#17202a}main{max-width:1100px;margin:0 auto;padding:24px}
h1{margin:0 0 4px}.sub{color:#667085;margin-bottom:16px}nav{display:flex;gap:8px;margin-bottom:20px}
nav a{padding:8px 12px;border:1px solid #cfd5dc;border-radius:7px;background:white;color:#344054;text-decoration:none;font-weight:600}nav a.active{background:#344054;color:white}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}.card{background:white;border:1px solid #d9dee5;border-radius:10px;padding:18px;box-shadow:0 1px 2px #0001}
h2{margin:0 0 14px}.row{display:flex;justify-content:space-between;gap:16px;border-top:1px solid #eee;padding:8px 0}.label{color:#667085}.value{font-variant-numeric:tabular-nums;text-align:right;overflow-wrap:anywhere}
.error{color:#b42318}.ok{color:#067647}.stale{color:#b54708}.positive{color:#067647}.negative{color:#b42318}
details{margin-top:14px;border-top:1px solid #eee;padding-top:10px}summary{cursor:pointer;color:#344054;font-weight:600}.raw{margin-top:8px}.raw .row{font-size:13px}
footer{margin-top:18px;color:#667085;font-size:13px}
</style></head><body><main>
<h1>UPSflow</h1><div class="sub">EcoFlow River 2 telemetry and local monitoring</div>
<nav><a class="active" href="/">Monitor</a><a href="/controls">Controls / Test</a></nav>
<div id="grid" class="grid"></div><footer id="status">Loading…</footer>
</main><script>
const esc=s=>String(s??"—");
const rawLabels={battery_level:"Battery level",input_power:"Total input power",output_power:"Total output power",cell_temperature:"Cell temperature",ac_input_power:"AC input power",ac_output_power:"AC output power",ac_input_voltage:"AC input voltage",ac_input_current:"AC input current",ac_ports:"AC ports",ac_xboost:"AC X-Boost",ac_charging_speed:"AC charging speed",ac_charging_power_min:"AC charging power min",ac_charging_power_max:"AC charging power max",dc_port_input_power:"DC input power",solar_input_power:"Solar input power",car_input_power:"Car input power",dc_mode:"DC mode",dc_12v_port:"12V DC port",dc12v_output_power:"12V output power",dc_charging_max_amps:"DC charging max amps",dc_charging_current_max:"DC charging current max",usbc_output_power:"USB-C output power",usba_output_power:"USB-A output power",energy_backup:"Energy backup",energy_backup_battery_level:"Energy backup battery level",battery_charge_limit_min:"Battery charge limit min",battery_charge_limit_max:"Battery charge limit max",remaining_time_charging:"Remaining charging time",remaining_time_discharging:"Remaining discharging time"};
function rawValue(name,v){if(v==null)return"—";if(typeof v==="number"){if(name.includes("temperature")||name.includes("voltage")||name.includes("current")||name.includes("amps"))return v.toFixed(2);if(name.includes("power"))return Math.round(v)+" W";if(name==="battery_level")return Number(v).toFixed(1)+"%";return String(v)}if(typeof v==="object")return JSON.stringify(v);return String(v)}
function rawRows(d){return Object.entries(d.raw_telemetry||{}).map(([n,v])=>'<div class="row"><span class="label">'+esc(rawLabels[n]||n)+'</span><span class="value">'+esc(rawValue(n,v))+'</span></div>').join("")}
function card(key,d){
 const connected=d.connected?'<span class="ok">Connected</span>':'<span>Disconnected</span>',ac=d.ac_present?'<span class="ok">YES</span>':'<span>NO</span>',net=Number(d.net_watts||0),nc=net>=0?"positive":"negative";
 const age=d.stale?'<span class="stale">STALE</span>':d.telemetry_age_seconds==null?"Never":Math.round(d.telemetry_age_seconds)+"s ago";
 const err=d.error?'<div class="row"><span class="label">Error</span><span class="value error">'+esc(d.error)+'</span></div>':"";
 return '<section class="card"><h2>'+esc(key).toUpperCase()+'</h2>'+
 '<div class="row"><span class="label">BLE</span><span class="value">'+connected+'</span></div>'+
 '<div class="row"><span class="label">Battery</span><span class="value">'+Number(d.battery_percent||0).toFixed(1)+'%</span></div>'+
 '<div class="row"><span class="label">AC input</span><span class="value">'+ac+'</span></div>'+
 '<div class="row"><span class="label">AC In</span><span class="value">'+Math.round(d.ac_watts||0)+' W</span></div>'+
 '<div class="row"><span class="label">DC In</span><span class="value">'+Math.round(d.dc_in_watts||0)+' W</span></div>'+
 '<div class="row"><span class="label">DC State</span><span class="value">'+esc(d.dc_state)+'</span></div>'+
 '<div class="row"><span class="label">Total Input</span><span class="value">'+Math.round(d.total_input_watts||0)+' W</span></div>'+
 '<div class="row"><span class="label">12V out</span><span class="value">'+Math.round(d.dc12v_output_watts||0)+' W</span></div>'+
 '<div class="row"><span class="label">USB out</span><span class="value">'+Math.round(d.usb_output_watts||0)+' W</span></div>'+
 '<div class="row"><span class="label">Total Output</span><span class="value">'+Math.round(d.output_watts||0)+' W</span></div>'+
 '<div class="row"><span class="label">Net power</span><span class="value '+nc+'">'+(net>=0?"+":"")+Math.round(net)+' W</span></div>'+
 '<div class="row"><span class="label">Telemetry</span><span class="value">'+age+'</span></div>'+err+
 '<details><summary>Show more</summary><div class="raw">'+
 '<div class="row"><span class="label">Name</span><span class="value">'+esc(d.name)+'</span></div>'+
 '<div class="row"><span class="label">Serial number</span><span class="value">'+esc(d.serial_number)+'</span></div>'+
 '<div class="row"><span class="label">BLE address</span><span class="value">'+esc(d.address)+'</span></div>'+
 '<div class="row"><span class="label">Derived AC voltage</span><span class="value">'+Number(d.ac_voltage||0).toFixed(1)+' V</span></div>'+
 '<div class="row"><span class="label">Derived AC input present</span><span class="value">'+ac+'</span></div>'+
 '<div class="row"><span class="label">Derived AC output</span><span class="value">'+Math.round(d.ac_output_watts||0)+' W</span></div>'+
 '<div class="row"><span class="label">Derived DC state</span><span class="value">'+esc(d.dc_12v_port_on==null?"UNKNOWN":d.dc_12v_port_on?"ON":"OFF")+'</span></div>'+
 '<div class="row"><span class="label">Telemetry age</span><span class="value">'+esc(d.telemetry_age_seconds==null?"Never":Number(d.telemetry_age_seconds).toFixed(1)+" s")+'</span></div>'+
 '<div class="row"><span class="label">Stale</span><span class="value">'+(d.stale?"YES":"NO")+'</span></div>'+rawRows(d)+'</div></details></section>';
}
async function refresh(){try{const r=await fetch("/v1/telemetry",{cache:"no-store"});if(!r.ok)throw new Error("HTTP "+r.status);const p=await r.json();document.getElementById("grid").innerHTML=Object.entries(p.devices||{}).map(([k,d])=>card(k,d)).join("");const s=Number(p.poll_seconds)>0?Number(p.poll_seconds):2;document.getElementById("status").textContent="Updated "+new Date().toLocaleTimeString()+" · Monitor · Refresh "+s+"s";window.__upsflowPollMs=s*1000}catch(e){document.getElementById("status").textContent="Telemetry unavailable: "+e;window.__upsflowPollMs=window.__upsflowPollMs||2000}finally{setTimeout(refresh,window.__upsflowPollMs||2000)}}refresh();
</script></body></html>"""

CONTROLS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>UPSflow Controls / Test</title>
<style>body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f3f4f6;color:#17202a}main{max-width:800px;margin:0 auto;padding:24px}h1{margin:0 0 4px}.sub{color:#667085;margin-bottom:16px}nav{display:flex;gap:8px;margin-bottom:20px}nav a{padding:8px 12px;border:1px solid #cfd5dc;border-radius:7px;background:white;color:#344054;text-decoration:none;font-weight:600}nav a.active{background:#344054;color:white}.card{background:white;border:1px solid #d9dee5;border-radius:10px;padding:18px;margin-bottom:16px}.control-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}button{padding:10px 14px;font-weight:600;cursor:pointer}button:disabled{cursor:wait;opacity:.65}.note{color:#667085;font-size:13px;margin-top:8px}#status{color:#667085;font-size:13px}</style></head>
<body><main><h1>UPSflow</h1><div class="sub">Local EcoFlow DC controls and test operations</div><nav><a href="/">Monitor</a><a class="active" href="/controls">Controls / Test</a></nav><div id="devices"></div><div id="status">Loading…</div>
<script>
const esc=s=>String(s??"—");
function render(devices){document.getElementById("devices").innerHTML=Object.entries(devices||{}).map(([key,d])=>{const state=d.dc_12v_port_on==null?"UNKNOWN":d.dc_12v_port_on?"ON":"OFF";return '<section class="card"><h2>'+esc(key).toUpperCase()+'</h2><div>12V DC state: <strong>'+state+'</strong></div><div class="control-grid" style="margin-top:14px"><button onclick="setDc(\''+esc(key)+'\',true)">Turn DC ON</button><button onclick="setDc(\''+esc(key)+'\',false)">Turn DC OFF</button><button onclick="resetDc(\''+esc(key)+'\')">Reset DC (5s)</button></div><div class="note">These controls write to the EcoFlow unit. Use this page for testing; the Monitor page is telemetry-only.</div></section>}).join("")}
async function load(){try{const r=await fetch("/v1/telemetry",{cache:"no-store"});if(!r.ok)throw new Error("HTTP "+r.status);const p=await r.json();render(p.devices);document.getElementById("status").textContent="Telemetry loaded "+new Date().toLocaleTimeString()}catch(e){document.getElementById("status").textContent="Telemetry unavailable: "+e}}
async function post(path,body){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","Accept":"application/json"},body:body?JSON.stringify(body):undefined});const p=await r.json();if(!r.ok)throw new Error(p.detail||("HTTP "+r.status));return p}
async function setDc(key,enabled){try{document.getElementById("status").textContent="Sending DC "+(enabled?"ON":"OFF")+" to "+key.toUpperCase()+"…";await post("/v1/devices/"+encodeURIComponent(key)+"/dc",{enabled});document.getElementById("status").textContent="DC command sent; waiting for telemetry…";setTimeout(load,500)}catch(e){document.getElementById("status").textContent="DC control failed: "+e}}
async function resetDc(key){try{document.getElementById("status").textContent="Running 5-second DC reset on "+key.toUpperCase()+"…";await post("/v1/devices/"+encodeURIComponent(key)+"/dc/reset");document.getElementById("status").textContent="DC reset completed and verified.";setTimeout(load,500)}catch(e){document.getElementById("status").textContent="DC reset failed: "+e}}
load();
</script></main></body></html>"""



async def http_response(
    writer: asyncio.StreamWriter,
    status: int,
    payload: dict[str, Any] | str,
    content_type: str = "application/json",
) -> None:
    if isinstance(payload, str):
        body = payload.encode("utf-8")
    else:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 500: "Internal Server Error"}.get(status, "Error")
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    writer.write(headers + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def handle_http_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    states: list[DeviceState],
    stale_seconds: int,
    poll_seconds: int,
) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        parts = request_line.decode("ascii", "ignore").strip().split()
        if len(parts) < 2:
            await http_response(writer, 405, {"status": "error", "detail": "invalid request"})
            return
        method, target = parts[0].upper(), parts[1].split("?", 1)[0]
        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if not line or line in {b"\r\n", b"\n"}:
                break
            if b":" in line:
                name, value_text = line.decode("iso-8859-1").split(":", 1)
                headers[name.strip().lower()] = value_text.strip()

        if method == "GET" and target == "/health":
            await http_response(writer, 200, {"status": "ok", "service": "UPSflow"})
        elif method == "GET" and target == "/v1/telemetry":
            await http_response(writer, 200, telemetry_snapshot(states, stale_seconds, poll_seconds))
        elif method == "GET" and target == "/":
            await http_response(writer, 200, DASHBOARD_HTML, "text/html")
        elif method == "POST" and target.startswith("/v1/devices/") and target.endswith("/dc/reset"):
            key = target[len("/v1/devices/"):-len("/dc/reset")].strip("/")
            if not key:
                await http_response(writer, 400, {"status": "error", "detail": "device key required"})
                return
            try:
                async with CONTROL_LOCK:
                    result = await reset_dc_port(states, key)
                await http_response(writer, 200, result)
            except KeyError as exc:
                await http_response(writer, 404, {"status": "error", "detail": str(exc)})
            except ConnectionError as exc:
                await http_response(writer, 409, {"status": "error", "detail": str(exc)})
            except ValueError as exc:
                await http_response(writer, 409, {"status": "error", "detail": str(exc)})
            except Exception as exc:
                LOG.exception("DC reset failed for %s", key)
                await http_response(writer, 500, {"status": "error", "detail": f"{type(exc).__name__}: {exc}"})
        elif method == "POST" and target.startswith("/v1/devices/") and target.endswith("/dc"):
            key = target[len("/v1/devices/"):-len("/dc")].strip("/")
            if not key:
                await http_response(writer, 400, {"status": "error", "detail": "device key required"})
                return
            try:
                content_length = int(headers.get("content-length", "0"))
            except ValueError:
                await http_response(writer, 400, {"status": "error", "detail": "invalid content-length"})
                return
            if content_length <= 0 or content_length > 1024:
                await http_response(writer, 400, {"status": "error", "detail": "request body required"})
                return
            body = await asyncio.wait_for(reader.readexactly(content_length), timeout=2.0)
            try:
                payload = json.loads(body.decode("utf-8"))
                enabled = payload["enabled"]
                if not isinstance(enabled, bool):
                    raise ValueError
            except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
                await http_response(writer, 400, {"status": "error", "detail": "JSON body must contain boolean 'enabled'"})
                return
            try:
                async with CONTROL_LOCK:
                    result = await control_dc_port(states, key, enabled)
                await http_response(writer, 200, result)
            except KeyError as exc:
                await http_response(writer, 404, {"status": "error", "detail": str(exc)})
            except ConnectionError as exc:
                await http_response(writer, 409, {"status": "error", "detail": str(exc)})
            except Exception as exc:
                LOG.exception("DC control failed for %s", key)
                await http_response(writer, 500, {"status": "error", "detail": f"{type(exc).__name__}: {exc}"})
        elif method != "GET":
            await http_response(writer, 405, {"status": "error", "detail": "GET or POST required"})
        else:
            await http_response(writer, 404, {"status": "error", "detail": "not found"})
    except (asyncio.TimeoutError, ConnectionError, UnicodeError):
        try:
            await http_response(writer, 405, {"status": "error", "detail": "invalid request"})
        except Exception:
            writer.close()
    except Exception:
        writer.close()
    finally:
        if not writer.is_closing():
            writer.close()


async def monitor(config: dict[str, Any], config_path: Path) -> None:
    global CONFIG
    CONFIG = config

    user_id = os.getenv("ECOFLOW_USER_ID") or str(config.get("user_id", "")).strip()
    if not user_id or user_id == "YOUR_ECOFLOW_USER_ID":
        raise SystemExit("EcoFlow user ID is required. Put it in config.json or set ECOFLOW_USER_ID.")

    device_entries = config.get("devices", {})
    states: list[DeviceState] = []
    for key in ("network", "server"):
        entry = device_entries.get(key, {})
        address = str(entry.get("address", "")).strip()
        if not address or address.startswith("BLE_ADDRESS_"):
            LOG.warning("No BLE address configured for %s; skipping", key)
            continue

        scanner_devices = await BleakScanner.discover(return_adv=True)
        match = scanner_devices.get(address)
        if match is None:
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
    http_host = str(config.get("http_host", "0.0.0.0")).strip() or "0.0.0.0"
    http_port = max(1, int(config.get("http_port", 5005)))
    http_server = await asyncio.start_server(
        lambda reader, writer: handle_http_client(
            reader, writer, states, stale_seconds, poll_seconds
        ),
        http_host,
        http_port,
    )
    LOG.info("Telemetry API listening on %s:%d", http_host, http_port)

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
            print(f"Refresh: {poll_seconds}s   API: http://{http_host}:{http_port}/v1/telemetry   Config: {config_path}")
            print("DC control is available through the local HTTP GUI.")
            await asyncio.sleep(poll_seconds)
    finally:
        http_server.close()
        await http_server.wait_closed()
        await asyncio.gather(
            *(state.device.disconnect() for state in states if state.device.is_connected),
            return_exceptions=True,
        )


CONFIG: dict[str, Any] = {}
CONTROL_LOCK = asyncio.Lock()


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
