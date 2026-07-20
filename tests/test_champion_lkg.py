"""R5 (INCIDENT-20260722 FINDING-6): champion LKG writer + restore + startup assertion.

The incident proved the old LKG restored a config FILE while pre_open_readiness consumes the CHAMPION
row -- so a missing/stale champion could never be repaired by restore. The LKG now snapshots the
champion state and restore re-stamps it fresh."""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

import pytest

from src.persistence import db
from src.control.maintenance_scheduler import audit_champion_staleness


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_restamp_records_lkg_and_restore_roundtrip(tmp_path):
    v = tmp_path / "rv.db"
    db.upsert_baseline_regime_champion(symbol="QQQ", params={"sma": 40}, promoted_at=_now(), db_path=v)
    assert "QQQ" in db.champion_lkg_symbols(v)                 # LKG recorded on re-stamp
    # corrupt/expire the LIVE champion (delete it) -> readiness would fail
    with sqlite3.connect(v) as c:
        c.execute("DELETE FROM regime_champions")
    assert audit_champion_staleness(v, ["QQQ"])["QQQ"]["reason"] == "no_champion"
    # restore from LKG re-stamps a FRESH champion -> readiness passes
    assert db.restore_champion_from_lkg("QQQ", v) is True
    st = audit_champion_staleness(v, ["QQQ"])["QQQ"]
    assert st["stale"] is False and st["age_days"] == 0
    assert db.restore_champion_from_lkg("ZZZ", v) is False     # no LKG -> no restore


def test_snapshot_champions_to_lkg(tmp_path):
    v = tmp_path / "rv.db"
    db.ensure_regime_champions_table(v)
    with sqlite3.connect(v) as c:                             # a champion written outside the upsert
        c.execute("INSERT INTO regime_champions (symbol,regime,params_json,composite_score,promoted_at,run_id)"
                  " VALUES ('SPY','CALM_MR','{}',2.5,?,0)", (_now(),))
    assert db.snapshot_champions_to_lkg(["SPY", "NONE"], v) == ["SPY"]   # only present ones
    assert "SPY" in db.champion_lkg_symbols(v)


def test_startup_assertion_raises_on_missing_lkg(tmp_path, monkeypatch):
    from src.control import maintenance_scheduler as ms
    monkeypatch.setattr(ms, "_active_mean_reversion_symbols", lambda: ["IWM"])  # no baseline params
    sched = ms.MaintenanceScheduler(vault_path=tmp_path / "rv.db")
    with pytest.raises(RuntimeError, match="missing an LKG champion"):
        sched.assert_readiness_lkg_present()


def test_startup_assertion_self_heals_baseline_legs(tmp_path, monkeypatch):
    from src.control import maintenance_scheduler as ms
    monkeypatch.setattr(ms, "_active_mean_reversion_symbols", lambda: ["QQQ"])  # baseline-seedable
    sched = ms.MaintenanceScheduler(vault_path=tmp_path / "rv.db")
    sched.assert_readiness_lkg_present()                       # seeds + snapshots -> passes, no raise
    assert "QQQ" in db.champion_lkg_symbols(tmp_path / "rv.db")


def test_self_heal_never_seeds_lkg_from_a_stale_champion(tmp_path, monkeypatch):
    """R5 SEED-CONDITION FIX (INCIDENT-20260722): a STALE present champion is NOT re-seeded by
    auto-seed and must NOT be snapshotted as 'known good' -- with no valid prior LKG it fails LOUD."""
    from datetime import datetime, timedelta, timezone
    from src.control import maintenance_scheduler as ms
    v = tmp_path / "rv.db"
    db.ensure_regime_champions_table(v)
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()   # past the 14d TTL
    with sqlite3.connect(v) as c:
        c.execute("INSERT INTO regime_champions (symbol,regime,params_json,composite_score,promoted_at,run_id)"
                  " VALUES ('QQQ','CALM_MR','{}',2.5,?,0)", (old,))
    monkeypatch.setattr(ms, "_active_mean_reversion_symbols", lambda: ["QQQ"])
    sched = ms.MaintenanceScheduler(vault_path=v)
    with pytest.raises(RuntimeError, match="missing an LKG champion"):
        sched.assert_readiness_lkg_present()
    assert "QQQ" not in db.champion_lkg_symbols(v)             # stale state was NOT written to the LKG
