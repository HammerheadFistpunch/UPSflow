import csv
import io

import pytest

from telemetry_history import TelemetryHistory, history_window


@pytest.mark.parametrize("minutes", [120, 240, 43200])
def test_history_range(minutes):
    start, end = history_window({"minutes": [str(minutes)]})
    assert end - start == minutes * 60_000


def test_monthly_cell_temperature_uses_archived_raw_readings(tmp_path, monkeypatch):
    now = 2_000_000_000_000
    monkeypatch.setattr("telemetry_history.time.time_ns", lambda: now * 1_000_000)
    history = TelemetryHistory(tmp_path / "history.sqlite3", 60)
    month_ago = now - 30 * 86_400_000
    server = {"connected": True, "stale": False, "raw_telemetry": {"cell_temperature": -2}}
    network = {"connected": True, "stale": True, "raw_telemetry": {"cell_temperature": 25}}
    snapshot = {"devices": {"server": server, "network": network}}
    history.append(month_ago - 1, snapshot)
    history.append(month_ago, snapshot)
    history.append(now, {"devices": {"server": {"connected": True, "stale": False}}})

    query = {"minutes": ["43200"], "metric": ["cell_temperature"]}
    assert history.read(query)["samples"] == [(month_ago, -2, None), (now, None, None)]
    assert len(list(csv.DictReader(io.StringIO(history.export_csv(query))))) == 3
    with pytest.raises(ValueError, match="minutes must be"):
        history_window({"minutes": ["43201"]})


def test_efficiency_calibration_data_and_overlay_use_all_dc_sources(tmp_path, monkeypatch):
    from efficiency import balance_days

    now = 2_000_000_000_000
    start = now - 3_600_000
    monkeypatch.setattr("telemetry_history.time.time_ns", lambda: now * 1_000_000)
    history = TelemetryHistory(tmp_path / "history.sqlite3", 60)
    network = {
        "connected": True, "stale": False, "ac_enabled": True, "ac_watts": 0,
        "total_input_watts": 44, "dc_in_watts": 44, "ac_output_watts": 21,
        "net_watts": 20.5, "battery_percent": 76.06,
        "raw_telemetry": {"battery_charge_limit_max": 100},
    }
    server = {**network, "ac_watts": 10, "total_input_watts": 54, "net_watts": 30.5}
    for minute in range(60):
        history.append(start + minute * 60_000, {"devices": {"network": network, "server": server}})

    query = {"minutes": ["60"], "metric": ["net_watts"]}
    assert history.read(query)["samples"][0] == (start, 30.5, 20.5)
    assert history.read(query, {"network": (25, 0), "server": (25, 0)})["samples"][0] == (
        start, 30.5, 20.5, 19.5, 9.5
    )
    assert history.read(query, {"network": (None, 20.5)})["samples"][0] == (
        start, 30.5, 20.5, None, 0
    )
    assert balance_days(history, "server", start, now, 25)[0]["corrected_net_watts"] == 19.5
    [network_day] = balance_days(history, "network", start, now, 25)
    assert network_day["windows"] == 1
    assert network_day["net_watts"] == 20.5
    assert network_day["corrected_net_watts"] == 9.5
    assert network_day["target_percent"] == 46.59
    assert balance_days(history, "network", start + 60_000, now) == []
