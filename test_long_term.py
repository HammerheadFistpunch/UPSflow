from datetime import date

from long_term import LongTermHistory, midnight, read_rows, recommendation
from telemetry_history import TelemetryHistory


def test_week_and_month_use_daily_totals_across_calendar_boundary(tmp_path):
    history = TelemetryHistory(tmp_path / "telemetry.sqlite3", 60)
    archive = LongTermHistory(history, 0)
    device = {
        "connected": True, "stale": False, "ac_present": True, "reserve_enabled": True,
        "raw_telemetry": {"battery_level": 95, "solar_input_power": 60,
                          "energy_backup_battery_level": 70, "battery_charge_limit_max": 100},
    }
    for day in (date(2026, 9, 30), date(2026, 10, 1)):
        for minute in (0, 1, 10, 11):
            history.append(midnight(day) + minute * 60_000, {"devices": {"server": device}})
    archive.refresh(date(2026, 10, 5))
    assert [row["solar_wh"] for row in read_rows(archive.weekly)] == ["4.0"]
    assert [row["solar_wh"] for row in read_rows(archive.monthly)] == ["2.0"]
    assert not any(row["tuning_signal"] for row in read_rows(archive.daily))
    LongTermHistory(history, 0).refresh(date(2026, 10, 5))
    assert len(read_rows(archive.daily)) == 2
    assert [row["solar_wh"] for row in read_rows(archive.monthly)] == ["2.0"]


def test_recommendation_uses_matching_prior_month_and_respects_bounds():
    month = date(2026, 11, 1)
    rows = [
        {"date": f"2026-10-{day:02}", "device": "server", "reserve": "70",
         "charge_limit": "100", "tuning_signal": "early_full"}
        for day in range(1, 15)
    ]
    assert recommendation(rows, "server", month, 70, 100, 68, 85)[0] == 68
    assert recommendation(rows, "server", month, 75, 100, 68, 85)[0] == 75
    assert recommendation(rows, "network", month, 70, 100, 68, 85)[0] == 70
    for row in rows:
        row["tuning_signal"] = "below_target"
    assert recommendation(rows, "server", month, 70, 100, 68, 85)[0] == 75
