"""R1: HumanOverrideRegistry.release() persistence regression suite.

The defect: release() wrote active=0 to the DB, then _sync_state_file() called
snapshot() -> _hydrate_from_state_file(), which RE-ACTIVATED the just-released
halt from the stale circuit_breaker_state.json. The DB write was silently
reverted and release() reported success anyway. These tests pin the fix:

  * a release persists on a fresh connection (the core regression),
  * even when the state file still says the halt is active (the exact fixture),
  * a journal row is written only on verified persistence,
  * releasing a nonexistent halt reports failure loudly (no success journal),
  * engage -> release -> snapshot is coherent,
  * writer and reader share one db_path (path-consistency),
  * double-release is idempotent.
"""

from __future__ import annotations

import json
import sqlite3

import structlog

from src.engine.governance import (
    EVENT_KILL_SWITCH_RELEASED,
    HumanOverrideRegistry,
    KillLevel,
)


def _registry(tmp_path):
    return HumanOverrideRegistry(
        db_path=tmp_path / "vault.db",
        state_file=tmp_path / "circuit_breaker_state.json",
    )


def _db_active(db_path, kill_level, scope_key="GLOBAL") -> int | None:
    """Read active straight from the DB on a FRESH connection (no registry cache)."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT active FROM human_override_registry "
            "WHERE kill_level = ? AND scope_key = ?",
            (kill_level.value, scope_key),
        ).fetchone()
    return None if row is None else int(row[0])


def test_release_persists_on_fresh_connection(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.engage(KillLevel.PORTFOLIO_HALT, operator="op", rationale="engage")
    reg.release(KillLevel.PORTFOLIO_HALT, operator="op", rationale="release")
    # Fresh DB read AND a fresh registry both see it released.
    assert _db_active(reg.db_path, KillLevel.PORTFOLIO_HALT) == 0
    assert _registry(tmp_path).is_portfolio_halted() is False


def test_release_persists_against_stale_active_state_file(tmp_path) -> None:
    """The exact R1 fixture: the state file still marks the halt active. Release
    must NOT be reverted by _hydrate_from_state_file() on the next snapshot()."""
    reg = _registry(tmp_path)
    reg.engage(KillLevel.PORTFOLIO_HALT, operator="op", rationale="engage")
    # Simulate the stale mirror that caused the revert loop.
    reg.state_file.write_text(
        json.dumps(
            {
                "portfolio_halt": True,
                "research_halt": False,
                "ai_halt": False,
                "strategy_halts": [],
                "states": [
                    {
                        "kill_level": "PORTFOLIO_HALT",
                        "scope_key": "GLOBAL",
                        "active": True,
                        "engaged_at": "2026-07-06T09:50:48+00:00",
                        "engaged_by": "risk",
                        "rationale_hash": "x",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reg.release(KillLevel.PORTFOLIO_HALT, operator="op", rationale="release")
    # snapshot() runs the hydrate step -- must stay released, not re-seed active.
    assert reg.is_portfolio_halted() is False
    assert _db_active(reg.db_path, KillLevel.PORTFOLIO_HALT) == 0
    # and the state-file mirror is now coherent with the DB.
    assert json.loads(reg.state_file.read_text())["portfolio_halt"] is False


def test_release_writes_journal_row_on_verified_persistence(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.engage(KillLevel.PORTFOLIO_HALT, operator="op", rationale="engage")
    reg.release(KillLevel.PORTFOLIO_HALT, operator="alice", rationale="cleared")
    entries = reg.journal.fetch_recent(event_type=EVENT_KILL_SWITCH_RELEASED)
    assert len(entries) == 1
    assert entries[0].scope_key == "PORTFOLIO_HALT:GLOBAL"


def test_release_of_nonexistent_halt_reports_failure_loudly(tmp_path) -> None:
    """No matching row -> log a warning and write NO release journal row
    (never report an unverified success)."""
    reg = _registry(tmp_path)
    with structlog.testing.capture_logs() as logs:
        state = reg.release(
            KillLevel.PORTFOLIO_HALT, operator="op", rationale="nothing to release"
        )
    assert state.active is False
    assert any(e.get("event") == "kill_switch_release_no_row" for e in logs)
    assert reg.journal.fetch_recent(event_type=EVENT_KILL_SWITCH_RELEASED) == []


def test_engage_release_snapshot_consistency(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.engage(KillLevel.PORTFOLIO_HALT, operator="op", rationale="engage")
    assert reg.is_portfolio_halted() is True
    assert any(
        s.kill_level == KillLevel.PORTFOLIO_HALT and s.active
        for s in reg.snapshot().states
    )
    reg.release(KillLevel.PORTFOLIO_HALT, operator="op", rationale="release")
    assert reg.is_portfolio_halted() is False
    assert all(
        s.kill_level != KillLevel.PORTFOLIO_HALT for s in reg.snapshot().states
    )


def test_writer_and_reader_share_one_db_path(tmp_path) -> None:
    """Path-consistency: the release UPDATE lands in exactly the db_path the
    readers (snapshot/get_state/journal) use -- no independent path is opened."""
    reg = _registry(tmp_path)
    assert reg.journal.db_path == reg.db_path
    reg.engage(KillLevel.RESEARCH_HALT, operator="op", rationale="engage")
    reg.release(KillLevel.RESEARCH_HALT, operator="op", rationale="release")
    # the persisted change is visible via a raw open of that same path.
    assert _db_active(reg.db_path, KillLevel.RESEARCH_HALT) == 0
    assert reg.get_state(KillLevel.RESEARCH_HALT).active is False


def test_double_release_is_idempotent(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.engage(KillLevel.PORTFOLIO_HALT, operator="op", rationale="engage")
    first = reg.release(KillLevel.PORTFOLIO_HALT, operator="op", rationale="release-1")
    second = reg.release(KillLevel.PORTFOLIO_HALT, operator="op", rationale="release-2")
    assert first.active is False and second.active is False
    assert reg.is_portfolio_halted() is False
    # the second (no-op) release must not add a spurious success journal row.
    assert len(reg.journal.fetch_recent(event_type=EVENT_KILL_SWITCH_RELEASED)) == 1
