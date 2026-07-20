"""G3.3 telemetry retention: permanence contract (permanent tables raise on
prune), dry-run accuracy, and aggregate-before-prune ordering."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.persistence.retention import (
    AggregateBeforePruneError, PermanentTablePruneError, RETENTION_POLICIES,
    aggregate_weekly, is_permanent, plan_retention, prune_table, run_retention,
)

NOW = datetime(2026, 7, 6, 3, 0, tzinfo=timezone.utc)


def _seed_heartbeats(db: Path, *, old: int, fresh: int) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE loop_heartbeats (id INTEGER PRIMARY KEY, component TEXT, "
                     "beat_utc TEXT, created_utc TEXT)")
        for i in range(old):
            ts = (NOW - timedelta(days=30 + i)).isoformat()
            conn.execute("INSERT INTO loop_heartbeats (component, beat_utc, created_utc) VALUES (?,?,?)",
                         ("main", ts, ts))
        for i in range(fresh):
            ts = (NOW - timedelta(hours=i)).isoformat()
            conn.execute("INSERT INTO loop_heartbeats (component, beat_utc, created_utc) VALUES (?,?,?)",
                         ("main", ts, ts))


def _seed_cycle_metrics(db: Path, *, old: int, fresh: int) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE cycle_metrics (id INTEGER PRIMARY KEY, created_utc TEXT)")
        for i in range(old):
            conn.execute("INSERT INTO cycle_metrics (created_utc) VALUES (?)",
                         ((NOW - timedelta(days=45 + i)).isoformat(),))
        for i in range(fresh):
            conn.execute("INSERT INTO cycle_metrics (created_utc) VALUES (?)",
                         ((NOW - timedelta(days=i)).isoformat(),))


def _count(db: Path, table: str) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# ---- permanence contract --------------------------------------------------- #

@pytest.mark.parametrize("table", [
    "vtd_diagnostic_reports", "psd_trial_ledger", "config_hash_chain",
    "vtd_quarantined_hypotheses", "operator_alerts",
])
def test_permanent_tables_raise_on_prune(table, tmp_path) -> None:
    assert is_permanent(table)
    with pytest.raises(PermanentTablePruneError):
        prune_table(table, tmp_path / "x.db", now=NOW)


def test_doctrine_permanence_set_is_declared() -> None:
    """The append-only doctrine tables MUST be marked permanent (VTD 5.6 / LLD 6.6)."""
    for table in ("vtd_diagnostic_reports", "psd_trial_ledger", "config_hash_chain"):
        assert RETENTION_POLICIES[table].permanent


# ---- dry-run accuracy ------------------------------------------------------ #

def test_dry_run_reports_without_deleting(tmp_path) -> None:
    db = tmp_path / "hb.db"
    _seed_heartbeats(db, old=12, fresh=5)   # 12 older than the 7-day window
    would = prune_table("loop_heartbeats", db, now=NOW, dry_run=True)
    assert would == 12
    assert _count(db, "loop_heartbeats") == 17, "dry-run must not delete"


def test_plan_retention_lists_all_tables(tmp_path) -> None:
    db = tmp_path / "hb.db"
    _seed_heartbeats(db, old=3, fresh=2)
    plan = plan_retention(db, now=NOW)
    by_table = {p["table"]: p for p in plan}
    assert by_table["loop_heartbeats"]["would_prune"] == 3
    assert by_table["vtd_diagnostic_reports"]["permanent"] is True
    assert by_table["vtd_diagnostic_reports"]["would_prune"] == 0


# ---- aggregate-before-prune ordering --------------------------------------- #

def test_prune_before_aggregate_raises(tmp_path) -> None:
    db = tmp_path / "cm.db"
    _seed_cycle_metrics(db, old=10, fresh=4)
    with pytest.raises(AggregateBeforePruneError):
        prune_table("cycle_metrics", db, now=NOW)   # aggregated=False


def test_run_retention_aggregates_then_prunes(tmp_path) -> None:
    db = tmp_path / "cm.db"
    _seed_cycle_metrics(db, old=10, fresh=4)   # 10 older than 30d
    _seed_heartbeats(db, old=6, fresh=3)       # 6 older than 7d
    report = run_retention(db, now=NOW)
    # cycle_metrics: aggregated to weekly buckets, then old rows pruned
    assert report["cycle_metrics"]["weekly_buckets"] >= 1
    assert report["cycle_metrics"]["pruned"] == 10
    assert _count(db, "cycle_metrics") == 4
    assert _count(db, "cycle_metrics_weekly") >= 1, "weekly aggregate kept indefinitely"
    # heartbeats pruned
    assert report["loop_heartbeats"]["pruned"] == 6
    assert _count(db, "loop_heartbeats") == 3
    # permanent tables untouched (not even attempted)
    assert report["psd_trial_ledger"]["permanent"] is True
    assert report["psd_trial_ledger"]["pruned"] == 0


def test_aggregate_weekly_is_idempotent(tmp_path) -> None:
    db = tmp_path / "cm.db"
    _seed_cycle_metrics(db, old=8, fresh=0)
    aggregate_weekly("cycle_metrics", db, now=NOW)
    first = _count(db, "cycle_metrics_weekly")
    aggregate_weekly("cycle_metrics", db, now=NOW)   # re-run
    assert _count(db, "cycle_metrics_weekly") == first, "weekly buckets must not duplicate"


def test_dry_run_run_retention_deletes_nothing(tmp_path) -> None:
    db = tmp_path / "hb.db"
    _seed_heartbeats(db, old=5, fresh=2)
    run_retention(db, now=NOW, dry_run=True)
    assert _count(db, "loop_heartbeats") == 7, "dry-run run_retention must not delete"
