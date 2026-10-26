"""INCIDENT 2026-08-07 latch fix — re-validate-once after an in-cycle LKG restore.

Artifact: on 2026-08-07T12:30:12Z the maintenance daemon logged `LKG CHAMPION RESTORED` for QQQ+SPY
(the stale-champion TTL restore re-stamped both fresh), yet the SAME run recorded
`MAINTENANCE_JOB_FAILED` and latched the live-consumption gate. The soak then early-returned every
cycle (inner heartbeat frozen) for 1437 blocked cycles until the NEXT daily readiness run at
2026-08-08T12:31Z re-validated on the already-fresh champion — a ~24h freeze for a failure that was
healed in the first ~1s.

The fix: after `restore_on_failure()` returns True, re-run the worker once and re-validate; a pass
takes the normal success/release path. Guards proven here:
  1. auto-clear only when restore returned True (restore==False stays blocked, no re-validate);
  2. only a clean-baseline champion is auto-restamped (a real tuned stale champion stays blocked);
  3. release goes through on_release_consumption (the caller that clears the in-memory latch).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.control.maintenance_scheduler import (
    MaintenanceScheduler,
    _validate_pre_open_payload,
    audit_champion_staleness,
)
from src.engine.control_plane import (
    ControlPlaneSupervisor,
    RunbookStep,
    SupervisorState,
)
from src.persistence import db

PRE_OPEN = RunbookStep.PRE_OPEN_HYDRATION


def _ledger(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS maintenance_job_ledger (
                job_id TEXT PRIMARY KEY,
                last_success_at TEXT,
                last_attempt_at TEXT,
                last_status TEXT NOT NULL,
                last_error TEXT,
                validation_passed INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT
            );
            """
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- supervisor-level: the re-validate-once wiring --------------------------------------------


def test_revalidate_after_restore_releases_gate_same_cycle(tmp_path: Path) -> None:
    """Restore heals the failure in-cycle -> re-validate passes -> gate RELEASES now (no ~24h latch)."""
    db_path = tmp_path / "research_vault.db"
    _ledger(db_path)
    state = {"healed": False, "worker": 0, "validator": 0}
    released: list[bool] = []
    blocked: list[str] = []

    async def worker() -> dict:
        state["worker"] += 1
        return {"stale": not state["healed"]}

    def validator(payload) -> tuple[bool, str]:
        state["validator"] += 1
        return (not payload["stale"], "" if not payload["stale"] else "stale champions: QQQ")

    def restore_on_failure() -> bool:
        state["healed"] = True  # the LKG re-stamp that clears staleness this cycle
        return True

    async def run() -> None:
        sup = ControlPlaneSupervisor(
            vault_path=db_path,
            workers={PRE_OPEN: worker},
            validators={PRE_OPEN: validator},
            restore_on_failure=restore_on_failure,
            on_block_consumption=blocked.append,
            on_release_consumption=lambda: released.append(True),
        )
        await sup.start()
        with patch("src.engine.control_plane.dispatch_system_alert"):
            result = await sup.execute_runbook(PRE_OPEN)
        assert result.success is True and result.validation_passed is True
        assert released == [True]  # Guard 3: released via on_release_consumption (clears the latch)
        assert blocked == []  # never latched
        assert state["worker"] == 2 and state["validator"] == 2  # exactly one bounded re-validate
        assert result.payload.get("revalidated_after_restore") is True
        assert sup.state == SupervisorState.RUNNING

    asyncio.run(run())


def test_restore_false_stays_blocked_no_revalidate(tmp_path: Path) -> None:
    """Guard 1: restore returns False (genuinely bad / no LKG) -> NO re-validate, gate stays blocked."""
    db_path = tmp_path / "research_vault.db"
    _ledger(db_path)
    calls = {"worker": 0, "validator": 0}
    blocked: list[str] = []
    released: list[bool] = []

    async def worker() -> dict:
        calls["worker"] += 1
        return {"stale": True}

    def validator(payload) -> tuple[bool, str]:
        calls["validator"] += 1
        return (False, "stale champions: QQQ")

    async def run() -> None:
        sup = ControlPlaneSupervisor(
            vault_path=db_path,
            workers={PRE_OPEN: worker},
            validators={PRE_OPEN: validator},
            restore_on_failure=lambda: False,  # no valid LKG -> genuinely bad
            on_block_consumption=blocked.append,
            on_release_consumption=lambda: released.append(True),
        )
        await sup.start()
        with patch("src.engine.control_plane.dispatch_system_alert"):
            result = await sup.execute_runbook(PRE_OPEN)
        assert result.success is False
        assert blocked == ["stale champions: QQQ"] and released == []
        assert calls == {"worker": 1, "validator": 1}  # no worker re-run when restore returns False
        assert sup.state == SupervisorState.DEGRADED

    asyncio.run(run())


def test_restore_true_but_revalidate_still_fails_stays_blocked(tmp_path: Path) -> None:
    """Restore returns True but the failure persists (e.g. a second symbol genuinely bad) -> the one
    bounded re-validate still fails -> gate stays blocked; never released."""
    db_path = tmp_path / "research_vault.db"
    _ledger(db_path)
    calls = {"worker": 0, "validator": 0}
    blocked: list[str] = []
    released: list[bool] = []

    async def worker() -> dict:
        calls["worker"] += 1
        return {"stale": True}  # never heals

    def validator(payload) -> tuple[bool, str]:
        calls["validator"] += 1
        return (False, "stale champions: SPY")

    async def run() -> None:
        sup = ControlPlaneSupervisor(
            vault_path=db_path,
            workers={PRE_OPEN: worker},
            validators={PRE_OPEN: validator},
            restore_on_failure=lambda: True,  # restored something, but staleness persists
            on_block_consumption=blocked.append,
            on_release_consumption=lambda: released.append(True),
        )
        await sup.start()
        with patch("src.engine.control_plane.dispatch_system_alert"):
            result = await sup.execute_runbook(PRE_OPEN)
        assert result.success is False and released == []
        assert blocked == ["stale champions: SPY"]
        assert calls == {"worker": 2, "validator": 2}  # one bounded re-validate, then block (no loop)
        assert sup.state == SupervisorState.DEGRADED

    asyncio.run(run())


# --- Guard 2: clean-baseline-only auto-restore -------------------------------------------------


def test_baseline_safe_true_for_baseline_and_absent(tmp_path: Path) -> None:
    v = tmp_path / "rv.db"
    db.upsert_baseline_regime_champion(
        symbol="QQQ", params=dict(db.BASELINE_CHAMPION_PARAMS["QQQ"]), promoted_at=_now(), db_path=v
    )
    assert db.champion_restore_is_baseline_safe("QQQ", v) is True  # clean baseline
    assert db.champion_restore_is_baseline_safe("SPY", v) is True  # absent -> baseline-seedable


def test_baseline_safe_false_for_real_tuned_champion(tmp_path: Path) -> None:
    v = tmp_path / "rv.db"
    db.ensure_regime_champions_table(v)
    with sqlite3.connect(v) as c:
        c.execute(
            "INSERT INTO regime_champions (symbol,regime,params_json,composite_score,promoted_at,run_id)"
            " VALUES (?,?,?,?,?,?)",
            ("QQQ", "CALM_MR", '{"sma_period_long": 999}', 3.7, _now(), 7),
        )
    assert db.champion_restore_is_baseline_safe("QQQ", v) is False


def test_baseline_safe_false_for_baseline_params_but_nonzero_run_id(tmp_path: Path) -> None:
    """A real tuner that happens to land on baseline-shaped params is still NOT a baseline: run_id
    is the authoritative discriminator."""
    v = tmp_path / "rv.db"
    db.ensure_regime_champions_table(v)
    with sqlite3.connect(v) as c:
        c.execute(
            "INSERT INTO regime_champions (symbol,regime,params_json,composite_score,promoted_at,run_id)"
            " VALUES (?,?,?,?,?,?)",
            ("QQQ", "CALM_MR", json.dumps(db.BASELINE_CHAMPION_PARAMS["QQQ"]),
             db.BASELINE_CHAMPION_SCORE, _now(), 5),
        )
    assert db.champion_restore_is_baseline_safe("QQQ", v) is False


def test_restore_skips_non_baseline_champion(tmp_path: Path, monkeypatch) -> None:
    """_restore_last_known_good_configs must NOT re-stamp a non-baseline stale champion even when a
    valid LKG exists to pull from -- it stays stale so re-validation still fails and the gate holds."""
    v = tmp_path / "research_vault.db"
    _ledger(v)
    # seed a baseline (this ALSO writes a valid LKG a naive restore would pull), then turn the LIVE
    # champion into a REAL tuned one (run_id=9). Guard 2 must refuse to launder it back to baseline.
    db.upsert_baseline_regime_champion(
        symbol="QQQ", params=dict(db.BASELINE_CHAMPION_PARAMS["QQQ"]), promoted_at=_now(), db_path=v
    )
    with sqlite3.connect(v) as c:
        c.execute("UPDATE regime_champions SET run_id=9, composite_score=4.2 WHERE symbol='QQQ'")
    monkeypatch.setattr(
        "src.control.maintenance_scheduler._active_mean_reversion_symbols", lambda: ["QQQ"]
    )
    monkeypatch.setattr("src.control.maintenance_scheduler.STRATEGY_YAML", {})  # skip config-file loop
    sched = MaintenanceScheduler(vault_path=v)
    sched._restore_last_known_good_configs()
    with sqlite3.connect(v) as c:
        run_id = c.execute("SELECT run_id FROM regime_champions WHERE symbol='QQQ'").fetchone()[0]
    assert run_id == 9  # NOT re-stamped to baseline (run_id 0) -> stays stale -> gate stays blocked


# --- integration: reproduce the confirmed 2026-08-07 freeze, release in ONE cycle --------------


def test_restore_from_lkg_preserves_run_id_no_flatten(tmp_path: Path) -> None:
    """The flattening fix: a real tuned champion (run_id != 0) round-tripped through the LKG restore
    KEEPS its run_id. Restore must not relabel it AUTO_SEED_RUN_ID -- that relabelling was the hole
    that let Guard 2 (which reads run_id) approve a laundered real champion."""
    v = tmp_path / "rv.db"
    db.ensure_regime_champions_table(v)
    with sqlite3.connect(v) as c:
        c.execute(
            "INSERT INTO regime_champions (symbol,regime,params_json,composite_score,promoted_at,run_id)"
            " VALUES (?,?,?,?,?,?)",
            ("QQQ", "CALM_MR", '{"sma_period_long": 999}', 3.7, _now(), 7),
        )
    db.snapshot_champions_to_lkg(["QQQ"], v)  # LKG now carries run_id=7 (provenance preserved)
    with sqlite3.connect(v) as c:  # wipe the live champion so restore has to rebuild it
        c.execute("DELETE FROM regime_champions")
    assert db.restore_champion_from_lkg("QQQ", v) is True
    with sqlite3.connect(v) as c:
        run_id = c.execute("SELECT run_id FROM regime_champions WHERE symbol='QQQ'").fetchone()[0]
    assert run_id == 7  # preserved, NOT flattened to AUTO_SEED_RUN_ID(0)
    assert db.champion_restore_is_baseline_safe("QQQ", v) is False  # Guard 2 still refuses it


def test_legacy_null_run_id_lkg_backfilled_to_baseline(tmp_path: Path) -> None:
    """Backward compat: an LKG row written before the run_id column existed (NULL) is backfilled to
    AUTO_SEED_RUN_ID on ensure -- correct-by-history (all pre-migration champions are baselines) --
    so restore yields a real baseline and Guard 2 can positively confirm it (no fail-closed re-latch)."""
    v = tmp_path / "rv.db"
    # simulate the PRE-migration schema: regime_champion_lkg WITHOUT the run_id column, with a row
    with sqlite3.connect(v) as c:
        c.execute(
            """CREATE TABLE regime_champion_lkg (
                symbol TEXT NOT NULL, regime TEXT NOT NULL, params_json TEXT NOT NULL,
                composite_score REAL NOT NULL, recorded_at TEXT NOT NULL,
                PRIMARY KEY (symbol, regime))"""
        )
        c.execute(
            "INSERT INTO regime_champion_lkg (symbol,regime,params_json,composite_score,recorded_at)"
            " VALUES (?,?,?,?,?)",
            ("QQQ", "CALM_MR", json.dumps(db.BASELINE_CHAMPION_PARAMS["QQQ"]),
             db.BASELINE_CHAMPION_SCORE, _now()),
        )
    db.ensure_regime_champions_table(v)  # runs the additive ALTER + one-time backfill
    with sqlite3.connect(v) as c:
        run_id = c.execute("SELECT run_id FROM regime_champion_lkg WHERE symbol='QQQ'").fetchone()[0]
    assert run_id == db.AUTO_SEED_RUN_ID  # NULL backfilled to baseline
    assert db.restore_champion_from_lkg("QQQ", v) is True
    assert db.champion_restore_is_baseline_safe("QQQ", v) is True  # positively a baseline -> auto-clearable


def test_real_tuned_champion_does_not_auto_clear_gate(tmp_path: Path, monkeypatch) -> None:
    """END-TO-END proof Guard 2 survives the flattening: a REAL tuned champion (run_id=7) gone stale,
    WITH a valid LKG snapshot of itself, must NOT auto-clear the gate. The restore preserves run_id,
    Guard 2 refuses, the re-validate still fails, and the gate stays blocked -- a stale real champion
    needs a genuine re-tune, never a re-stamp."""
    v = tmp_path / "research_vault.db"
    _ledger(v)
    db.ensure_regime_champions_table(v)
    with sqlite3.connect(v) as c:
        c.execute(
            "INSERT INTO regime_champions (symbol,regime,params_json,composite_score,promoted_at,run_id)"
            " VALUES (?,?,?,?,?,?)",
            ("QQQ", "CALM_MR", '{"sma_period_long": 999}', 3.7, _now(), 7),
        )
    db.snapshot_champions_to_lkg(["QQQ"], v)  # LKG carries the real run_id=7
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    with sqlite3.connect(v) as c:
        c.execute("UPDATE regime_champions SET promoted_at=? WHERE symbol='QQQ'", (stale_ts,))
    monkeypatch.setattr(
        "src.control.maintenance_scheduler._active_mean_reversion_symbols", lambda: ["QQQ"]
    )
    monkeypatch.setattr("src.control.maintenance_scheduler.STRATEGY_YAML", {})
    sched = MaintenanceScheduler(vault_path=v)
    released: list[bool] = []
    blocked: list[str] = []

    async def worker() -> dict:
        return {
            "config_ready": True,
            "staleness": audit_champion_staleness(v, ["QQQ"]),
            "inventory": {},
            "auto_seed": db.auto_seed_baseline_champions_if_needed(v, ("QQQ",)),
        }

    async def run() -> None:
        sup = ControlPlaneSupervisor(
            vault_path=v,
            workers={PRE_OPEN: worker},
            validators={PRE_OPEN: _validate_pre_open_payload},
            restore_on_failure=sched._restore_last_known_good_configs,
            on_block_consumption=blocked.append,
            on_release_consumption=lambda: released.append(True),
        )
        await sup.start()
        with patch("src.engine.control_plane.dispatch_system_alert"):
            result = await sup.execute_runbook(PRE_OPEN)
        assert result.success is False  # NOT auto-cleared
        assert released == [] and blocked  # gate stays blocked
        with sqlite3.connect(v) as c:  # the real champion was never laundered to baseline
            run_id = c.execute("SELECT run_id FROM regime_champions WHERE symbol='QQQ'").fetchone()[0]
        assert run_id == 7
        assert audit_champion_staleness(v, ["QQQ"])["QQQ"]["stale"] is True  # still stale -> needs re-tune

    asyncio.run(run())


def test_incident_20260807_stale_baseline_releases_in_one_cycle(tmp_path: Path, monkeypatch) -> None:
    """End-to-end with the REAL validator (_validate_pre_open_payload) and the REAL restore
    (_restore_last_known_good_configs, incl. Guard 2) + real champion re-stamp: a stale BASELINE
    champion with a valid LKG releases the gate in a SINGLE execute_runbook, instead of latching to
    the next daily run. This is the 08-07T12:30Z -> 08-08T12:31Z block collapsed to one cycle."""
    v = tmp_path / "research_vault.db"
    _ledger(v)
    # baseline champion + LKG, then age the LIVE row past the 14d TTL (LKG snapshot stays fresh).
    db.upsert_baseline_regime_champion(
        symbol="QQQ", params=dict(db.BASELINE_CHAMPION_PARAMS["QQQ"]), promoted_at=_now(), db_path=v
    )
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    with sqlite3.connect(v) as c:
        c.execute("UPDATE regime_champions SET promoted_at=? WHERE symbol='QQQ'", (stale_ts,))
    assert audit_champion_staleness(v, ["QQQ"])["QQQ"]["stale"] is True  # precondition: stale

    monkeypatch.setattr(
        "src.control.maintenance_scheduler._active_mean_reversion_symbols", lambda: ["QQQ"]
    )
    monkeypatch.setattr("src.control.maintenance_scheduler.STRATEGY_YAML", {})
    sched = MaintenanceScheduler(vault_path=v)
    released: list[bool] = []
    blocked: list[str] = []

    async def worker() -> dict:
        # mirrors the champion-state portion of _run_pre_open_checks (no Alpaca/config-alignment I/O):
        # reflects the LIVE champion state, so the 2nd call sees the fresh champion the restore stamped.
        return {
            "config_ready": True,
            "staleness": audit_champion_staleness(v, ["QQQ"]),
            "inventory": {},
            "auto_seed": db.auto_seed_baseline_champions_if_needed(v, ("QQQ",)),
        }

    async def run() -> None:
        sup = ControlPlaneSupervisor(
            vault_path=v,
            workers={PRE_OPEN: worker},
            validators={PRE_OPEN: _validate_pre_open_payload},  # the REAL validator
            restore_on_failure=sched._restore_last_known_good_configs,  # the REAL restore (Guard 2)
            on_block_consumption=blocked.append,
            on_release_consumption=lambda: released.append(True),
        )
        await sup.start()
        with patch("src.engine.control_plane.dispatch_system_alert"):
            result = await sup.execute_runbook(PRE_OPEN)
        assert result.success is True, f"expected in-cycle release, got block: {result.error}"
        assert released == [True] and blocked == []
        # the champion the restore re-stamped is now fresh (age 0) -> the block would not recur today
        assert audit_champion_staleness(v, ["QQQ"])["QQQ"]["stale"] is False

    asyncio.run(run())
