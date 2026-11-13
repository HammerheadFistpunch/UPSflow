"""Exercise the real monitor/API with only the hardware dependencies replaced."""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def app(monkeypatch):
    modules = {name: ModuleType(name) for name in (
        'bleak', 'bleak.backends', 'bleak.backends.device', 'bleak.backends.scanner',
        'eflib', 'eflib.devices', 'eflib.props', 'eflib.props.raw_data_field',
        'eflib.props.transforms')}
    modules['bleak'].BleakScanner = SimpleNamespace(discover=AsyncMock(return_value={}))
    modules['bleak.backends.device'].BLEDevice = object
    modules['bleak.backends.scanner'].AdvertisementData = object
    modules['eflib.devices'].river2 = SimpleNamespace(
        Device=object, pb_inv=SimpleNamespace(ac_in_vol=1, ac_in_amp=2),
        Mr330MpptHeart=type('Mppt', (), {}),
        DirectPdHeartbeatPack=type('Pd', (), {}))
    modules['eflib.props.raw_data_field'].raw_field = lambda *args: SimpleNamespace(default_when_missing=lambda v: v)
    modules['eflib.props.transforms'].pdiv = lambda *args: None
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, 'path', sys.path.copy())
    spec = importlib.util.spec_from_file_location('upsflow_test_app', Path(__file__).with_name('upsflow.py'))
    module = importlib.util.module_from_spec(spec)
    with patch.object(Path, 'is_file', return_value=True):
        spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'print', lambda *args, **kwargs: None, raising=False)
    return module


def test_undiscovered_device_has_valid_snapshot_and_console_output(app):
    state = app.DeviceState('server', address='aa:bb')
    snapshot = app.telemetry_device(state, 15)
    assert not snapshot['connected'] and snapshot['stale']
    assert snapshot['address'] == 'aa:bb'
    assert snapshot['raw_telemetry']['battery_level'] is None
    assert 'Disconnected' in '\n'.join(app.format_device(state, 15))


def test_api_starts_and_stays_available_with_both_devices_missing(app, monkeypatch, tmp_path):
    async def check():
        listening = asyncio.Event()
        servers = []
        original = asyncio.start_server
        async def start(*args, **kwargs):
            # Use an ephemeral port; production clamps config ports to at least 1.
            server = await original(args[0], args[1], 0, **kwargs)
            servers.append(server)
            listening.set()
            return server
        monkeypatch.setattr(app.asyncio, 'start_server', start)
        task = asyncio.create_task(app.monitor({
            'user_id': 'user', 'history_sample_seconds': 0,
            'http_host': '127.0.0.1', 'http_port': 0,
            'devices': {'server': {'address': 'aa:bb'}, 'network': {'address': 'cc:dd'}},
        }, tmp_path / 'config.json'))
        try:
            await asyncio.wait_for(listening.wait(), 2)
            port = servers[0].sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(b'GET /v1/telemetry HTTP/1.1\r\nHost: localhost\r\n\r\n')
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 2)
            writer.close()
            await writer.wait_closed()
            assert response.startswith(b'HTTP/1.1 200')
            import json
            payload = json.loads(response.split(b'\r\n\r\n', 1)[1])
            assert set(payload['devices']) == {'server', 'network'}
            assert all(not d['connected'] and d['stale'] for d in payload['devices'].values())
            assert not task.done()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not servers[0].is_serving()
    asyncio.run(asyncio.wait_for(check(), 4))


def test_telemetry_listener_updates_freshness_and_ignores_retired_device(app, monkeypatch):
    callbacks = []
    device = object()
    state = app.DeviceState('server', device)
    monkeypatch.setattr(app.eflib, 'get_fixed_length_coding_device',
                        lambda d: SimpleNamespace(on_message_processed=callbacks.append), raising=False)
    app.install_telemetry(state)
    callbacks[0](app.river2.Mr330MpptHeart())
    assert state.last_update and set(state.power_updates) == {'ac', 'dc'}
    baseline = state.last_update
    state.device = object()
    callbacks[0](app.river2.DirectPdHeartbeatPack())
    assert state.last_update == baseline and 'reserve' not in state.power_updates


def test_tuning_panel_persists_per_device_mode_and_restores_it_on_restart(app, tmp_path):
    import json

    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "user_id": "example",
        "long_term_history": {"enabled": True},
        "devices": {"server": {"address": "AA"}, "network": {"address": "BB"}},
    }), encoding="utf-8")
    config = app.load_config(path)
    history = app.TelemetryHistory(tmp_path / "logs" / "telemetry.sqlite3", 60)
    archive, tuning = app.long_term_settings(config, history)
    states = [app.DeviceState(key, address=entry["address"]) for key, entry in config["devices"].items()]
    panel = app.TuningPanel(config, path, archive, tuning, states)

    async def check():
        assert (await panel.status())["devices"]["server"]["mode"] == "off"
        await panel.save({"device": "server", "mode": "recommend", "min_percent": 30, "max_percent": 85})
        await panel.save({"device": "network", "mode": "auto", "min_percent": 40, "max_percent": 75})
        with pytest.raises(ValueError):
            await panel.save({"device": "network", "mode": "auto", "min_percent": 80, "max_percent": 75})
        await panel.save({"device": "server", "mode": "off"})
        saved = app.load_config(path)
        assert saved["user_id"] == "example"
        assert saved["devices"]["server"]["address"] == "AA"
        assert saved["devices"]["server"]["reserve_tuning"]["min_percent"] == 30
        assert app.long_term_settings(saved, history)[1] == tuning == {"network": ("auto", 40, 75)}

    asyncio.run(check())
