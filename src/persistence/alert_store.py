"""SQLite persistence for operator alerts (garage G1.5).

Append-only alerts table backing the LogNotifier. Every notification is durably
recorded here in addition to the structured log, so an operator can reconstruct
what fired and when even if the log stream was missed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS operator_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    message     TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operator_alerts_kind ON operator_alerts(kind);
"""


def ensure_alert_schema(db_path: str | Path) -> None:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.executescript(_DDL)


def persist_alert(alert: dict, db_path: str | Path) -> int:
    ensure_alert_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    detail = alert.get("detail", {})
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO operator_alerts (kind, severity, message, detail_json, created_utc)
               VALUES (?, ?, ?, ?, ?)""",
            (str(alert.get("kind", "alert")), str(alert.get("severity", "warning")),
             str(alert.get("message", "")), json.dumps(detail, default=str), now),
        )
        return int(cur.lastrowid)


def read_alert_count(db_path: str | Path, *, kind: str | None = None) -> int:
    ensure_alert_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        if kind is None:
            row = conn.execute("SELECT COUNT(*) FROM operator_alerts").fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM operator_alerts WHERE kind = ?", (str(kind),)).fetchone()
    return int(row[0]) if row else 0
