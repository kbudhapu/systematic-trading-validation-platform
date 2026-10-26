"""Telemetry retention policy (garage G3.3).

Per-table retention with a hard PERMANENCE contract: the append-only doctrine
tables -- diagnostic reports, the PSD trial ledger, the config-hash chain,
quarantined hypotheses, and operator alerts -- are NEVER pruned (VTD section 5.6 /
LLD section 6.6: survivor-only statistics are prohibited; retirement archives
capital, never data). Raw high-volume telemetry is aggregated to weekly buckets
(kept indefinitely) and then pruned past its window.

Ordering is enforced: a table with `aggregate_before_prune` cannot be pruned
until its aggregation has run (so a prune can never discard un-aggregated rows).
VACUUM is a separate, off-hours step.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


class PermanentTablePruneError(Exception):
    """Raised on any attempt to prune a PERMANENT (append-only doctrine) table."""


class AggregateBeforePruneError(Exception):
    """Raised when a prune is attempted before the required aggregation has run."""


@dataclass(frozen=True)
class RetentionPolicy:
    table: str
    permanent: bool = False
    retention_days: int | None = None
    timestamp_column: str = "created_utc"
    aggregate_before_prune: bool = False
    aggregate_table: str | None = None


# The doctrine permanence set + raw-telemetry windows. Permanent tables are the
# append-only ledgers; everything with a retention_days is regenerable telemetry.
RETENTION_POLICIES: dict[str, RetentionPolicy] = {
    # --- PERMANENT (never pruned) ---
    "vtd_diagnostic_reports":     RetentionPolicy("vtd_diagnostic_reports", permanent=True),
    "psd_trial_ledger":           RetentionPolicy("psd_trial_ledger", permanent=True),
    "config_hash_chain":          RetentionPolicy("config_hash_chain", permanent=True),
    "vtd_quarantined_hypotheses": RetentionPolicy("vtd_quarantined_hypotheses", permanent=True),
    "operator_alerts":            RetentionPolicy("operator_alerts", permanent=True),
    # --- RAW TELEMETRY (windowed; aggregated-then-pruned where noted) ---
    "loop_heartbeats":            RetentionPolicy("loop_heartbeats", retention_days=7,
                                                  timestamp_column="beat_utc"),
    "cycle_metrics":              RetentionPolicy("cycle_metrics", retention_days=30,
                                                  timestamp_column="created_utc",
                                                  aggregate_before_prune=True,
                                                  aggregate_table="cycle_metrics_weekly"),
}


def is_permanent(table: str) -> bool:
    p = RETENTION_POLICIES.get(table)
    return bool(p and p.permanent)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _count_older(conn: sqlite3.Connection, policy: RetentionPolicy, cutoff_iso: str) -> int:
    if not _table_exists(conn, policy.table):
        return 0
    row = conn.execute(
        f"SELECT COUNT(*) FROM {policy.table} WHERE {policy.timestamp_column} < ?",
        (cutoff_iso,)).fetchone()
    return int(row[0]) if row else 0


def prune_table(
    table: str,
    db_path: str | Path,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    aggregated: bool = False,
) -> int:
    """Prune rows older than the table's window. Returns the row count pruned (or,
    in dry_run, that WOULD be pruned). Raises on a permanent table, or on an
    aggregate table pruned before its aggregation ran."""
    policy = RETENTION_POLICIES.get(table)
    if policy is None:
        raise KeyError(f"no retention policy for table {table!r}")
    if policy.permanent:
        raise PermanentTablePruneError(
            f"{table} is PERMANENT (append-only doctrine table) and must never be pruned")
    if policy.aggregate_before_prune and not aggregated:
        raise AggregateBeforePruneError(
            f"{table} must be aggregated into {policy.aggregate_table} before pruning")
    if policy.retention_days is None:
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=policy.retention_days)).isoformat()
    with sqlite3.connect(db_path) as conn:
        n = _count_older(conn, policy, cutoff)
        if not dry_run and n > 0:
            conn.execute(f"DELETE FROM {policy.table} WHERE {policy.timestamp_column} < ?", (cutoff,))
    return n


def aggregate_weekly(table: str, db_path: str | Path, *, now: datetime | None = None) -> int:
    """Roll raw rows into weekly-bucket counts in the aggregate table (kept
    indefinitely) BEFORE pruning. Returns the number of weekly buckets written.
    Aggregates are keyed by ISO week so re-runs are idempotent (INSERT OR REPLACE)."""
    policy = RETENTION_POLICIES.get(table)
    if policy is None or not policy.aggregate_table:
        return 0
    with sqlite3.connect(db_path) as conn:
        if not _table_exists(conn, policy.table):
            return 0
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {policy.aggregate_table} (
                   iso_week TEXT PRIMARY KEY, row_count INTEGER NOT NULL, rolled_utc TEXT NOT NULL)""")
        rows = conn.execute(
            f"""SELECT strftime('%Y-W%W', {policy.timestamp_column}) AS wk, COUNT(*)
                FROM {policy.table} GROUP BY wk""").fetchall()
        rolled = (now or datetime.now(timezone.utc)).isoformat()
        for wk, cnt in rows:
            conn.execute(
                f"INSERT OR REPLACE INTO {policy.aggregate_table} (iso_week, row_count, rolled_utc) "
                f"VALUES (?, ?, ?)", (wk, int(cnt), rolled))
        return len(rows)


def plan_retention(db_path: str | Path, *, now: datetime | None = None) -> list[dict]:
    """Dry-run: what WOULD be pruned per non-permanent table (no deletion)."""
    now = now or datetime.now(timezone.utc)
    plan: list[dict] = []
    for table, policy in RETENTION_POLICIES.items():
        if policy.permanent:
            plan.append({"table": table, "permanent": True, "would_prune": 0})
            continue
        n = prune_table(table, db_path, now=now, dry_run=True,
                        aggregated=policy.aggregate_before_prune)
        plan.append({"table": table, "permanent": False, "would_prune": n,
                     "retention_days": policy.retention_days,
                     "aggregate_table": policy.aggregate_table})
    return plan


def run_retention(db_path: str | Path, *, now: datetime | None = None,
                  dry_run: bool = False) -> dict:
    """Apply retention across all non-permanent tables: aggregate-then-prune where
    required. Permanent tables are skipped. Returns a per-table report."""
    now = now or datetime.now(timezone.utc)
    report: dict[str, dict] = {}
    for table, policy in RETENTION_POLICIES.items():
        if policy.permanent:
            report[table] = {"permanent": True, "pruned": 0}
            continue
        aggregated = False
        buckets = 0
        if policy.aggregate_before_prune and not dry_run:
            buckets = aggregate_weekly(table, db_path, now=now)   # aggregate FIRST
            aggregated = True
        pruned = prune_table(table, db_path, now=now, dry_run=dry_run,
                             aggregated=aggregated or policy.aggregate_before_prune)
        report[table] = {"permanent": False, "pruned": pruned,
                         "weekly_buckets": buckets}
    return report


def vacuum(db_path: str | Path) -> None:
    """Reclaim space (run OFF-HOURS via the maintenance daemon, never mid-session)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("VACUUM")
