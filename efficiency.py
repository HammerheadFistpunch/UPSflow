"""Estimate the missing watts in the DC-input / AC-inverter power balance."""
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from statistics import median

from long_term import number

MAX_CORRECTION_WATTS = 100
# Match this operating mode in both historical plots and calibration.
DC_INVERTER = """json_extract(device, '$.connected') = 1
    AND json_extract(device, '$.stale') = 0
    AND json_extract(device, '$.ac_enabled') = 1
    AND json_extract(device, '$.ac_watts') = 0
    AND json_extract(device, '$.dc_in_watts') > 0
    AND abs(json_extract(device, '$.total_input_watts')
            - json_extract(device, '$.dc_in_watts')) <= 2
"""


def valid_correction(value):
    return type(value) in (int, float) and 0 <= value <= MAX_CORRECTION_WATTS


def balance_days(history, key, start_ms, end_ms):
    """Summarize non-overlapping, nearly hour-long steady-SoC windows by day."""
    if not history.path.exists():
        return []
    days = defaultdict(list)
    window = []
    with closing(sqlite3.connect(history.path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(f"""
            WITH readings AS (
                SELECT timestamp_ms, json_extract(snapshot, ?) AS device
                FROM telemetry WHERE timestamp_ms >= ? AND timestamp_ms < ?
            )
            SELECT timestamp_ms, CASE WHEN {DC_INVERTER} THEN device END
            FROM readings ORDER BY timestamp_ms
        """, (f"$.devices.{key}", start_ms, end_ms))
        for time_ms, raw in rows:
            if raw is None:
                window.clear()
                continue
            device = json.loads(raw)
            soc = device.get("battery_percent")
            ceiling = device.get("raw_telemetry", {}).get("battery_charge_limit_max")
            net, dc, ac = (device.get(field) for field in (
                "net_watts", "dc_in_watts", "ac_output_watts"
            ))
            if (not all(number(v) for v in (soc, ceiling, net, dc, ac))
                    or not 5 <= soc <= ceiling - 2 or ac <= 0):
                window.clear()
                continue
            if window and not 0 < time_ms - window[-1][0] <= 1500 * history.interval:
                window.clear()
            window.append((time_ms, soc, net, dc, ac))
            elapsed = time_ms - window[0][0]
            if elapsed < 3_540_000:
                continue
            _, levels, _, _, _ = zip(*window)
            if len(window) >= 12 and max(levels) - min(levels) <= 0.25:
                means = [
                    sum((b[0] - a[0]) * (a[i] + b[i]) / 2
                        for a, b in zip(window, window[1:])) / elapsed
                    for i in (2, 3, 4)
                ]
                if valid_correction(means[0]):
                    day = datetime.fromtimestamp(window[0][0] / 1000).date().isoformat()
                    days[day].append(means)
                window.clear()
            else:
                window.pop(0)
    return [
        {"date": day, "windows": len(windows),
         **{name: round(median(values), 2) for name, values in
            zip(("net_watts", "dc_input_watts", "ac_output_watts"), zip(*windows))}}
        for day, windows in sorted(days.items())
    ]
