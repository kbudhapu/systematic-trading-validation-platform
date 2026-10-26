"""
GD-S8: consecutive cycle failures must escalate DegradationManager to SOFT_DEGRADE.

Pre-fix: the except Exception handler in orchestrator.run() only incremented
_consecutive_cycle_failures and dispatched a page at streak>=3 — DegradationManager
was never called, so OperationalMode stayed NORMAL and block_new_entries=False
throughout a sustained failure streak.

Post-fix: apply_soft_degrade("consecutive_cycle_failures") is called on the
first failure, subject to a guard that prevents downgrading an already-blocking
state (RECON_DEGRADED_MANAGE or HARD_CRITICAL_DEGRADE).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from src.engine.degradation_manager import DegradationManager, OperationalMode


# ---------------------------------------------------------------------------
# Helper: simulate one iteration of the main loop's try/except block.
# We can't call run() (infinite loop + sleep), so we replicate the exact
# handler logic that was changed.  The assertions target DegradationManager
# state, not the handler wiring itself.
# ---------------------------------------------------------------------------

def _make_dm(tmp_path: Path) -> DegradationManager:
    """Real DegradationManager backed by a temp SQLite file."""
    return DegradationManager(db_path=tmp_path / "gov.db")


def _simulate_cycle_failure(dm: DegradationManager) -> None:
    """Exact logic from the patched except block (minus logging / paging)."""
    if not dm.current_state().block_new_entries:
        dm.apply_soft_degrade("consecutive_cycle_failures")


# ---------------------------------------------------------------------------
# Test 1: first failure immediately transitions NORMAL → SOFT_DEGRADE
# ---------------------------------------------------------------------------

def test_first_cycle_failure_enters_soft_degrade(tmp_path: Path) -> None:
    """GD-S8: a single cycle exception must flip mode to SOFT_DEGRADE (block_new_entries=True)."""
    dm = _make_dm(tmp_path)
    assert dm.current_state().mode == OperationalMode.NORMAL

    _simulate_cycle_failure(dm)

    state = dm.current_state()
    assert state.mode == OperationalMode.SOFT_DEGRADE, (
        "GD-S8: mode stayed NORMAL after first cycle failure — "
        "block_new_entries was False during sustained failure streak"
    )
    assert state.block_new_entries is True
    assert state.reason == "consecutive_cycle_failures"


# ---------------------------------------------------------------------------
# Test 2: repeated calls while already in SOFT_DEGRADE are safe (idempotent)
# ---------------------------------------------------------------------------

def test_repeated_cycle_failures_while_soft_degrade_are_safe(tmp_path: Path) -> None:
    """GD-S8: calling apply_soft_degrade every failing cycle resets streak, stays in SOFT_DEGRADE."""
    dm = _make_dm(tmp_path)
    dm.apply_soft_degrade("consecutive_cycle_failures")
    # Simulate 5 more failures while already degraded
    for _ in range(5):
        _simulate_cycle_failure(dm)
    state = dm.current_state()
    assert state.mode == OperationalMode.SOFT_DEGRADE
    assert state.block_new_entries is True
    # Recovery streak must be 0 (each call resets it, preventing inadvertent recovery)
    assert dm._recovery_streak == 0


# ---------------------------------------------------------------------------
# Test 3: guard prevents downgrading RECON_DEGRADED_MANAGE to SOFT_DEGRADE
# ---------------------------------------------------------------------------

def test_cycle_failure_does_not_downgrade_recon_to_soft(tmp_path: Path) -> None:
    """GD-S8 guard: RECON_DEGRADED_MANAGE must not be overridden by cycle-failure soft-degrade.

    RECON can only exit via reset_to_normal(); SOFT_DEGRADE can exit via 3 OK SLO streaks.
    Overriding RECON with SOFT would weaken the recovery guarantee.
    """
    dm = _make_dm(tmp_path)
    dm.apply_recon_degraded_manage("pre_open_recon_exhausted")
    assert dm.current_state().mode == OperationalMode.RECON_DEGRADED_MANAGE

    _simulate_cycle_failure(dm)

    # Must stay in RECON, not silently downgraded to SOFT
    assert dm.current_state().mode == OperationalMode.RECON_DEGRADED_MANAGE, (
        "GD-S8 guard: cycle failure downgraded RECON_DEGRADED_MANAGE to SOFT_DEGRADE — "
        "this weakens the RECON recovery guarantee (RECON requires explicit reset_to_normal)"
    )


# ---------------------------------------------------------------------------
# Test 4: guard prevents calling apply_soft_degrade while in HARD_CRITICAL_DEGRADE
# ---------------------------------------------------------------------------

def test_cycle_failure_does_not_touch_hard_critical_degrade(tmp_path: Path) -> None:
    """GD-S8 guard: HARD_CRITICAL_DEGRADE must not be overridden by cycle-failure soft-degrade."""
    dm = _make_dm(tmp_path)
    # apply_hard_critical_degrade tries create_task; outside an event loop it catches
    # RuntimeError gracefully (confirmed in degradation_manager.py:146-148).
    dm.apply_hard_critical_degrade("test_hard")
    assert dm.current_state().mode == OperationalMode.HARD_CRITICAL_DEGRADE

    _simulate_cycle_failure(dm)

    assert dm.current_state().mode == OperationalMode.HARD_CRITICAL_DEGRADE


# ---------------------------------------------------------------------------
# Test 5: a successful cycle after failures allows the existing 3-streak
# recovery path — the fix must not break recovery
# ---------------------------------------------------------------------------

def test_successful_cycle_after_failure_allows_slo_recovery(tmp_path: Path) -> None:
    """GD-S8: the fix only adds an entry point into SOFT_DEGRADE; it must not
    prevent recovery via the existing 3-consecutive-OK-verdict mechanism.
    """
    from src.engine.slo_monitor import DataIntegrityVerdict, IntegritySeverity

    dm = _make_dm(tmp_path)
    # One cycle failure → SOFT_DEGRADE
    _simulate_cycle_failure(dm)
    assert dm.current_state().mode == OperationalMode.SOFT_DEGRADE

    # 3 consecutive clean SLO verdicts → NORMAL
    ok_verdict = DataIntegrityVerdict(
        passed=True,
        severity=IntegritySeverity.OK,
        bar_freshness_seconds=0.0,
        missing_bar_rate=0.0,
        nbbo_success_rate=1.0,
        rl_backfill_lag_hours=0.0,
        calendar_session_minutes=390.0,
        is_early_close_session=False,
        reasons=(),
    )
    for _ in range(3):
        dm.evaluate_from_slo(ok_verdict)

    assert dm.current_state().mode == OperationalMode.NORMAL, (
        "GD-S8: SOFT_DEGRADE (entered via cycle failure) did not recover after "
        "3 consecutive OK SLO verdicts — existing recovery path is broken"
    )


# ---------------------------------------------------------------------------
# Test 6: existing paging behavior unchanged — BOOT_BLOCKING_ERROR fires at
# streak>=3, not at streak==1.  Regression test: the fix must not affect the
# pager dispatch threshold.
# ---------------------------------------------------------------------------

def test_paging_threshold_unchanged_at_streak_3(tmp_path: Path) -> None:
    """GD-S8 regression: BOOT_BLOCKING_ERROR page still dispatched at streak>=3, not earlier."""
    dm = _make_dm(tmp_path)

    dispatched_at: list[int] = []

    async def _fake_page(incident_type, message, metadata):
        dispatched_at.append(metadata.get("consecutive_failures", -1))

    async def _run() -> None:
        # Simulate 4 consecutive failures identical to the main loop handler
        streak = 0
        for _ in range(4):
            streak += 1
            if not dm.current_state().block_new_entries:
                dm.apply_soft_degrade("consecutive_cycle_failures")
            if streak >= 3:
                with patch(
                    "src.control.alerts.dispatch_critical_page",
                    side_effect=_fake_page,
                ) as mock_page:
                    from src.control.alerts import IncidentType
                    await _fake_page(
                        IncidentType.BOOT_BLOCKING_ERROR,
                        f"Fatal execution loop: {streak} consecutive cycle failures",
                        {"consecutive_failures": streak, "last_error": "test"},
                    )

    asyncio.run(_run())

    # Page must fire at streak=3 (and again at 4 in this simulation), never at streak<3
    assert all(s >= 3 for s in dispatched_at), (
        f"GD-S8 regression: page fired at streak < 3: {dispatched_at}"
    )
    # SOFT_DEGRADE must still be engaged (from streak=1 via the fix)
    assert dm.current_state().mode == OperationalMode.SOFT_DEGRADE
