"""Persistent snapshots of the UPSflow telemetry API."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

METRICS = {
    "battery_percent", "ac_present", "ac_voltage", "ac_watts", "dc_in_watts",
    "total_input_watts", "output_watts", "net_watts",
}


class TelemetryHistory:
    def __init__(self, path: Path, interval: int):
        self.path = path
        self.interval = interval
        self.write_failed = False

    def append(self, timestamp_ms: int, snapshot: dict) -> None:
        payload = json.dumps(snapshot, separators=(",", ":"), allow_nan=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INTEGER PRIMARY KEY,
                    timestamp_ms INTEGER NOT NULL,
                    snapshot TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS telemetry_time ON telemetry(timestamp_ms)
            """)
            db.execute(
                "INSERT INTO telemetry(timestamp_ms, snapshot) VALUES (?, ?)",
                (timestamp_ms, payload),
            )

    def read(self, query: dict[str, list[str]]) -> dict:
        metric = query.get("metric", [""])[0]
        hours = int(query.get("hours", ["24"])[0])
        after = int(query.get("after", ["0"])[0])
        if not 1 <= hours <= 168 or not 0 <= after < 2**63:
            raise ValueError("hours must be 1–168 and after must be a valid sample ID")
        if metric and metric not in METRICS:
            raise ValueError("unknown chart metric")

        end = time.time_ns() // 1_000_000
        start = end - hours * 3_600_000
        result = {"interval_seconds": self.interval, "write_failed": self.write_failed}
        # Avoid creating an empty database when a reader arrives before the sampler.
        if not self.path.exists():
            if self.write_failed:
                raise OSError("telemetry archive could not be created")
            return {**result, "start_ms": start, "end_ms": end, "samples": [], "next_after": None}

        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            if metric:
                rows = db.execute("""
                    SELECT timestamp_ms,
                        CASE WHEN json_extract(snapshot, '$.devices.server.connected') = 1
                              AND json_extract(snapshot, '$.devices.server.stale') = 0
                             THEN json_extract(snapshot, ?) END,
                        CASE WHEN json_extract(snapshot, '$.devices.network.connected') = 1
                              AND json_extract(snapshot, '$.devices.network.stale') = 0
                             THEN json_extract(snapshot, ?) END
                    FROM telemetry WHERE timestamp_ms BETWEEN ? AND ?
                    ORDER BY timestamp_ms, id
                """, (f"$.devices.server.{metric}", f"$.devices.network.{metric}", start, end))
                return {**result, "start_ms": start, "end_ms": end, "samples": list(rows)}

            rows = db.execute("""
                SELECT id, timestamp_ms, snapshot FROM telemetry
                WHERE id > ? ORDER BY id LIMIT 501
            """, (after,)).fetchall()
            return {
                **result,
                "samples": [
                    {"id": sample_id, "timestamp_ms": timestamp_ms, "telemetry": json.loads(snapshot)}
                    for sample_id, timestamp_ms, snapshot in rows[:500]
                ],
                "next_after": rows[499][0] if len(rows) > 500 else None,
            }
