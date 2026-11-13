"""Compact local-calendar rollups and monthly reserve recommendations."""
import csv
import json
import math
import os
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

DAY = timedelta(days=1)
DAILY_FIELDS = (
    "date", "device", "observed_seconds", "coverage", "battery_min", "battery_max",
    "solar_wh", "ac_wh", "output_wh", "reserve", "charge_limit", "tuning_signal",
)
PERIOD_FIELDS = (
    "start", "end_exclusive", "device", "days_logged", "observed_hours",
    "battery_min", "battery_max", "solar_wh", "ac_wh", "output_wh",
    "early_full_days", "below_target_days", "eligible_days",
)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def midnight(day):
    return int(datetime.combine(day, datetime.min.time()).timestamp() * 1000)


def local_day(timestamp_ms):
    return datetime.fromtimestamp(timestamp_ms / 1000).date()


def read_rows(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def write_rows(path, fields, rows):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8", dir=path.parent, delete=False
        ) as target:
            temporary = Path(target.name)
            writer = csv.DictWriter(target, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class DailyReading:
    def __init__(self):
        self.seconds = 0
        self.minimum = 100
        self.maximum = 0
        self.solar_seconds = 0
        self.solar_after_full_seconds = 0
        self.energy = dict.fromkeys(("solar_wh", "ac_wh", "output_wh"), 0.0)
        self.first_solar = None
        self.first_full = None
        self.settings = set()
        self.on_utility = True

    def add(self, device, timestamp, seconds):
        raw = device.get("raw_telemetry", {})
        battery = raw.get("battery_level")
        if not number(battery) or not 0 <= battery <= 100:
            return
        solar = raw.get("solar_input_power")
        reserve = raw.get("energy_backup_battery_level")
        limit = raw.get("battery_charge_limit_max")
        if not number(solar):
            solar = device.get("dc_in_watts") if device.get("dc_state") == "SOLAR" else None
        self.seconds += seconds
        self.minimum = min(self.minimum, battery)
        self.maximum = max(self.maximum, battery)
        self.on_utility &= device.get("ac_present") is True
        if (device.get("reserve_enabled") is True and number(reserve) and number(limit)
                and 0 <= reserve < limit <= 100):
            self.settings.add((reserve, limit))
        else:
            self.settings.add((None, None))
        for name, watts in (
            ("solar_wh", solar), ("ac_wh", raw.get("ac_input_power")),
            ("output_wh", raw.get("output_power")),
        ):
            if number(watts):
                self.energy[name] += max(0, watts) * seconds / 3600
        if number(solar) and solar >= 5:
            if self.first_solar is None:
                self.first_solar = timestamp
            self.solar_seconds += seconds
            if self.first_full is not None:
                self.solar_after_full_seconds += seconds
        if (self.first_solar is not None and self.first_full is None
                and number(limit) and battery >= limit - 2):
            self.first_full = timestamp

    def row(self, day, device):
        coverage = self.seconds * 1000 / (midnight(day + DAY) - midnight(day))
        reserve, limit = next(iter(self.settings)) if len(self.settings) == 1 else (None, None)
        signal = ""
        if coverage >= 0.8 and self.on_utility and reserve is not None and self.solar_seconds >= 3600:
            if self.first_full is not None:
                early = self.first_full - self.first_solar <= 3 * 3600_000
                signal = "early_full" if early and self.solar_after_full_seconds >= 1800 else "on_target"
            elif self.maximum < limit - 5:
                signal = "below_target"
            else:
                signal = "on_target"
        return {
            "date": day.isoformat(), "device": device, "observed_seconds": self.seconds,
            "coverage": round(coverage, 4), "battery_min": self.minimum, "battery_max": self.maximum,
            **{key: round(value, 3) for key, value in self.energy.items()},
            "reserve": reserve, "charge_limit": limit, "tuning_signal": signal,
        }


def collect_days(db, start, end, interval):
    readings = defaultdict(DailyReading)
    previous = None
    for timestamp, snapshot in db.execute(
        "SELECT timestamp_ms, snapshot FROM telemetry WHERE timestamp_ms >= ? AND timestamp_ms <= ? "
        "ORDER BY timestamp_ms, id", (midnight(start), midnight(end) + interval * 2000),
    ):
        if previous is not None:
            before, payload = previous
            day = local_day(before)
            elapsed = (min(timestamp, midnight(day + DAY)) - before) / 1000
            if day < end and 0 < elapsed and timestamp - before <= interval * 2000:
                for key, device in json.loads(payload)["devices"].items():
                    if device.get("connected") and device.get("stale") is False:
                        readings[day, key].add(device, before, elapsed)
        previous = timestamp, snapshot
    return [
        reading.row(day, key) for (day, key), reading in sorted(readings.items()) if reading.seconds
    ]


def summarize(daily, today, monthly):
    groups = defaultdict(list)
    for row in daily:
        day = date.fromisoformat(row["date"])
        if monthly:
            start = day.replace(day=1)
            end = (start + timedelta(days=32)).replace(day=1)
        else:
            start = day - timedelta(days=day.weekday())
            end = start + 7 * DAY
        if end <= today:
            groups[start, end, row["device"]].append(row)
    for (start, end, device), rows in sorted(groups.items()):
        yield {
            "start": start.isoformat(), "end_exclusive": end.isoformat(), "device": device,
            "days_logged": len(rows),
            "observed_hours": round(sum(float(row["observed_seconds"]) for row in rows) / 3600, 2),
            "battery_min": min(float(row["battery_min"]) for row in rows),
            "battery_max": max(float(row["battery_max"]) for row in rows),
            **{key: round(sum(float(row[key]) for row in rows), 3)
               for key in ("solar_wh", "ac_wh", "output_wh")},
            "early_full_days": sum(row["tuning_signal"] == "early_full" for row in rows),
            "below_target_days": sum(row["tuning_signal"] == "below_target" for row in rows),
            "eligible_days": sum(bool(row["tuning_signal"]) for row in rows),
        }


def recommendation(daily, device, month, reserve, charge_limit, minimum, maximum):
    previous_month = (month - DAY).replace(day=1).isoformat()
    rows = [
        row for row in daily
        if row["device"] == device and row["date"][:7] == previous_month[:7]
        and row["tuning_signal"] and row["reserve"] not in ("", None) and row["charge_limit"]
        and float(row["reserve"]) == reserve and float(row["charge_limit"]) == charge_limit
    ]
    if len(rows) < 14 or not minimum <= reserve <= maximum:
        return reserve, "insufficient matching history or reserve outside configured bounds"
    signals = [row["tuning_signal"] for row in rows]
    if signals.count("early_full") > len(rows) / 2:
        return max(minimum, reserve - 5), "reached the charge ceiling early on most eligible days"
    if signals.count("below_target") > len(rows) / 2:
        return min(maximum, charge_limit - 1, reserve + 5), "ended below the charge ceiling on most eligible days"
    return reserve, "no consistent reason to move the reserve"


class LongTermHistory:
    def __init__(self, history, retention_days):
        self.history = history
        self.retention_days = retention_days
        self.directory = history.path.parent
        self.daily = self.directory / "long-term-daily.csv"
        self.weekly = self.directory / "long-term-weekly.csv"
        self.monthly = self.directory / "long-term-monthly.csv"
        self.decisions = self.directory / "reserve-tuning.csv"
        self.last_period = None

    def refresh(self, today):
        period = today - timedelta(days=today.weekday()), today.replace(day=1)
        if period == self.last_period or not self.history.path.exists():
            return
        with closing(sqlite3.connect(self.history.path)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'telemetry'").fetchone():
                return
            oldest = db.execute("SELECT MIN(timestamp_ms) FROM telemetry").fetchone()[0]
            if oldest is None:
                return
            daily = read_rows(self.daily)
            end = max(period)
            # Recompute recent days if their raw samples are still available.
            start = local_day(oldest)
            if daily:
                start = max(start + DAY, date.fromisoformat(daily[-1]["date"]) - DAY)
            if start < end:
                daily = [row for row in daily if row["date"] < start.isoformat()]
                daily.extend(collect_days(db, start, end, self.history.interval))
            write_rows(self.daily, DAILY_FIELDS, daily)
            write_rows(self.weekly, PERIOD_FIELDS, summarize(daily, today, monthly=False))
            write_rows(self.monthly, PERIOD_FIELDS, summarize(daily, today, monthly=True))
            if self.retention_days:
                with db:
                    db.execute("DELETE FROM telemetry WHERE timestamp_ms < ?", (
                        midnight(min(end, today - timedelta(days=self.retention_days))),
                    ))
        self.last_period = period

    def record(self, month, device, current, proposed, outcome, reason):
        rows = read_rows(self.decisions)
        rows.append(dict(month=month.isoformat(), device=device, current=current,
                         proposed=proposed, outcome=outcome, reason=reason))
        write_rows(self.decisions, ("month", "device", "current", "proposed", "outcome", "reason"), rows)
