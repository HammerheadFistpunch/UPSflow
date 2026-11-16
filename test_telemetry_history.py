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
