"""Tests for SLO monitor and degradation manager."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.engine.degradation_manager import (
    DegradationManager,
    OperationalMode,
)
from src.engine.slo_monitor import (
    BackpressureVerdict,
    CycleLatencyVerdict,
    IntegritySeverity,
    MarketSessionCalendar,
    SLOMonitor,
    compute_missing_bar_rate,
)
from src.models import SignalAction

ET = ZoneInfo("America/New_York")


def _bar_ts(hour: int, minute: int, day: int = 24) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=ET).astimezone(timezone.utc)


def test_calendar_early_close_session_minutes() -> None:
    calendar = MarketSessionCalendar()
    bounds = calendar.session_bounds(date(2026, 11, 27))
    assert bounds.early_close is True
    assert bounds.session_minutes == 3.5 * 60.0


def test_calendar_holiday_not_trading_day() -> None:
    calendar = MarketSessionCalendar()
    assert calendar.is_trading_day(date(2026, 12, 25)) is False


def test_missing_bar_rate_ignores_overnight_gap() -> None:
    calendar = MarketSessionCalendar()
    bars = [
        _bar_ts(10, 0),
        _bar_ts(10, 15),
        _bar_ts(10, 45),
    ]
    rate = compute_missing_bar_rate(
        bars,
        bar_minutes=15,
        calendar=calendar,
        lookback_bars=10,
    )
    assert 0.0 < rate < 0.5


def test_evaluate_data_integrity_fresh_bars_pass() -> None:
    monitor = SLOMonitor()
    now = _bar_ts(11, 0) + timedelta(minutes=5)
    latest = _bar_ts(11, 0)
    bars = [latest - timedelta(minutes=15 * i) for i in range(6)]
    verdict = monitor.evaluate_data_integrity(
        {
            "exchange_reference_ts": now,
            "asset_class": "stock",
            "timeframe": "15Min",
            "latest_bar_timestamp": latest,
            "bar_timestamps": bars,
            "nbbo_success_rate": 0.95,
            "rl_backfill_lag_hours": 2.0,
        }
    )
    assert verdict.passed
    assert verdict.severity == IntegritySeverity.OK


def test_evaluate_data_integrity_stale_bar_soft_breach() -> None:
    monitor = SLOMonitor()
    now = _bar_ts(11, 0)
    # T1: freshness is period-relative. For a 15Min bar the SOFT threshold is period+150s=1050s of
    # staleness-since-close. A bar OPENED 35 min ago CLOSED 20 min ago -> 1200s stale -> SOFT (a bar
    # merely 5 min old is NOT stale for a 15Min strategy and is now correctly OK).
    latest = now - timedelta(minutes=35)
    verdict = monitor.evaluate_data_integrity(
        {
            "exchange_reference_ts": now,
            "asset_class": "stock",
            "timeframe": "15Min",
            "latest_bar_timestamp": latest,
            "bar_timestamps": [latest],
            "nbbo_success_rate": 0.95,
            "rl_backfill_lag_hours": 1.0,
        }
    )
    assert verdict.severity == IntegritySeverity.SOFT_BREACH
    assert "bar_freshness_drift" in verdict.reasons


def test_evaluate_data_integrity_critical_nbbo_and_backfill() -> None:
    monitor = SLOMonitor()
    now = _bar_ts(11, 0)
    latest = now - timedelta(hours=2)
    verdict = monitor.evaluate_data_integrity(
        {
            "exchange_reference_ts": now,
            "asset_class": "stock",
            "timeframe": "15Min",
            "latest_bar_timestamp": latest,
            "bar_timestamps": [latest],
            "nbbo_success_rate": 0.40,
            "rl_backfill_lag_hours": 40.0,
        }
    )
    assert verdict.severity == IntegritySeverity.HARD_BREACH


def test_cycle_latency_violation_flags_budget_overrun() -> None:
    monitor = SLOMonitor(max_cycle_budget_ms=100.0)
    verdict = monitor.evaluate_cycle_latency(
        phase_a_ms=40.0,
        phase_b_ms=35.0,
        phase_c_ms=45.0,
        total_cycle_ms=120.0,
    )
    assert isinstance(verdict, CycleLatencyVerdict)
    assert verdict.violated is True
    assert verdict.max_cycle_budget_ms == 100.0


def test_fill_sieve_backpressure_blocks_new_entries() -> None:
    monitor = SLOMonitor(max_pending_bundles=5)
    verdict = monitor.evaluate_fill_sieve_backpressure(6)
    assert isinstance(verdict, BackpressureVerdict)
    assert verdict.blocks_new_entries is True
    assert verdict.pending_bundles == 6


def test_soft_degrade_blocks_entries_only() -> None:
    manager = DegradationManager()
    state = manager.apply_soft_degrade("bar_freshness_drift")
    assert state.mode == OperationalMode.SOFT_DEGRADE
    assert state.block_new_entries is True
    assert state.ignore_model_signals is False
    assert state.force_emergency_flatten is False
    assert manager.should_block_entry_action(SignalAction.LONG) is True
    assert manager.should_block_entry_action(SignalAction.EXIT) is False


def test_hard_critical_degrade_flattens_and_ignores_signals() -> None:
    manager = DegradationManager()
    state = manager.apply_hard_critical_degrade("missing_bar_rate_critical")
    assert state.mode == OperationalMode.HARD_CRITICAL_DEGRADE
    assert state.force_emergency_flatten is True
    assert state.ignore_model_signals is True
    assert state.bypass_standard_timers is True
    assert manager.consume_flatten_request() is True
    assert manager.consume_flatten_request() is False


def test_soft_degrade_recovers_after_clean_slo_streak() -> None:
    manager = DegradationManager()
    manager.apply_soft_degrade("missing_bar_rate_elevated")
    from src.engine.slo_monitor import DataIntegrityVerdict

    for _ in range(3):
        state = manager.evaluate_from_slo(
            DataIntegrityVerdict(
                passed=True,
                severity=IntegritySeverity.OK,
                bar_freshness_seconds=30.0,
                missing_bar_rate=0.0,
                nbbo_success_rate=1.0,
                rl_backfill_lag_hours=1.0,
                calendar_session_minutes=390.0,
                is_early_close_session=False,
                reasons=(),
            )
        )
    assert state.mode == OperationalMode.NORMAL


def test_hard_degrade_not_downgraded_by_recovery() -> None:
    manager = DegradationManager()
    manager.apply_hard_critical_degrade("bar_freshness_critical")
    from src.engine.slo_monitor import DataIntegrityVerdict

    state = manager.evaluate_from_slo(
        DataIntegrityVerdict(
            passed=True,
            severity=IntegritySeverity.OK,
            bar_freshness_seconds=10.0,
            missing_bar_rate=0.0,
            nbbo_success_rate=1.0,
            rl_backfill_lag_hours=0.0,
            calendar_session_minutes=390.0,
            is_early_close_session=False,
            reasons=(),
        )
    )
    assert state.mode == OperationalMode.HARD_CRITICAL_DEGRADE
