"""Stage-5 live-reconciliation tracker store (final-prep P4; VTD Stage 5 / channel c).

Append-only tables that the soak populates FROM DAY ONE so the analysis layer has
data when it is eventually built. The analysis itself stays DEFERRED -- VTD channel
(c) requires >= 20 trading days of live divergence data before any cost
recalibration. Two tables:
  - live_fill_costs: realized vs modeled round-trip cost per fill (bps)
  - signal_timing_deltas: signal-to-fill timing (ms)
Rows are only ever INSERTed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS live_fill_costs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    leg_id            TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    signal_ts_utc     TEXT,
    fill_ts_utc       TEXT,
    modeled_cost_bps  REAL,
    realized_cost_bps REAL,
    divergence_bps    REAL,
    created_utc       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fill_costs_leg ON live_fill_costs(leg_id);
CREATE TABLE IF NOT EXISTS signal_timing_deltas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    leg_id        TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    signal_ts_utc TEXT,
    fill_ts_utc   TEXT,
    latency_ms    REAL,
    created_utc   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_timing_leg ON signal_timing_deltas(leg_id);
"""


def ensure_reconciliation_schema(db_path: str | Path) -> None:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.executescript(_DDL)


def persist_fill_costs(payloads: Sequence[dict], db_path: str | Path) -> None:
    if not payloads:
        return
    ensure_reconciliation_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for p in payloads:
        modeled = p.get("modeled_cost_bps")
        realized = p.get("realized_cost_bps")
        div = (realized - modeled) if (modeled is not None and realized is not None) else None
        rows.append((str(p["leg_id"]), str(p["symbol"]), str(p.get("side", "")),
                     p.get("signal_ts_utc"), p.get("fill_ts_utc"), modeled, realized, div, now))
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """INSERT INTO live_fill_costs
               (leg_id, symbol, side, signal_ts_utc, fill_ts_utc, modeled_cost_bps,
                realized_cost_bps, divergence_bps, created_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)


def persist_timing_deltas(payloads: Sequence[dict], db_path: str | Path) -> None:
    if not payloads:
        return
    ensure_reconciliation_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    rows = [(str(p["leg_id"]), str(p["symbol"]), p.get("signal_ts_utc"),
             p.get("fill_ts_utc"), p.get("latency_ms"), now) for p in payloads]
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """INSERT INTO signal_timing_deltas
               (leg_id, symbol, signal_ts_utc, fill_ts_utc, latency_ms, created_utc)
               VALUES (?, ?, ?, ?, ?, ?)""", rows)


def persist_reconciliation_rows(payloads: Sequence[dict], db_path: str | Path) -> None:
    """Writer-drain entrypoint: routes each payload by `record_type`."""
    fills = [p for p in payloads if p.get("record_type") == "fill_cost"]
    timings = [p for p in payloads if p.get("record_type") == "timing_delta"]
    persist_fill_costs(fills, db_path)
    persist_timing_deltas(timings, db_path)


def read_fill_cost_count(db_path: str | Path, *, leg_id: str | None = None) -> int:
    ensure_reconciliation_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        if leg_id is None:
            row = conn.execute("SELECT COUNT(*) FROM live_fill_costs").fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM live_fill_costs WHERE leg_id=?",
                               (leg_id,)).fetchone()
    return int(row[0]) if row else 0


def read_timing_count(db_path: str | Path) -> int:
    ensure_reconciliation_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM signal_timing_deltas").fetchone()
    return int(row[0]) if row else 0
