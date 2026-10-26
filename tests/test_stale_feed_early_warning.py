"""
Fix 5/5 — Live data staleness window: early-warning observability.

Tests verify:
1. Warning IS emitted when lag crosses STALE_FEED_WARNING_FRACTION of max_bar_age
   but NOT when lag is below that threshold.
2. Warning fires ONCE per staleness event (de-duplication): two consecutive cycles
   with lag above the threshold produce exactly one warning, not two.
3. Warning resets after freshness is restored — a new staleness event fires again.
4. wall_clock_freshness_ok() return value is completely unchanged — the gate's
   actual blocking behavior is identical before and after this fix.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import structlog.testing

from src.engine.cold_start_gate import (
    ColdStartGateConfig,
    ColdStartGateEngine,
    ColdStartGateState,
    STALE_FEED_WARNING_FRACTION,
    StreamHealthSnapshot,
    max_bar_age_for_timeframe,
    wall_clock_freshness_ok,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CFG = ColdStartGateConfig(
    max_cold_start_wait_seconds=0.0,  # skip time-bound wait in all tests
    max_bar_age_seconds=65.0,
)

_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _ts(lag_seconds: float) -> datetime:
    """A bar timestamp `lag_seconds` in the past relative to _NOW."""
    return _NOW - timedelta(seconds=lag_seconds)


def _health(lag_seconds: float, bar_count: int = 2) -> StreamHealthSnapshot:
    latest = _ts(lag_seconds)
    prior = latest - timedelta(minutes=15)
    return StreamHealthSnapshot(
        latest_bar_timestamp=latest,
        prior_bar_timestamp=prior,
        latest_volume=1000.0,
        prior_volume=900.0,
        bar_count=bar_count,
    )


def _engine() -> ColdStartGateEngine:
    engine = ColdStartGateEngine(config=_CFG)
    engine.begin_cold_start(["spy"], symbols={"spy": "SPY"})
    # Manually promote to STABILIZED_VERIFIED so tick_time_bound exercises
    # the freshness-monitoring path rather than the cold-start path.
    engine._state_by_strategy["spy"] = ColdStartGateState.STABILIZED_VERIFIED
    return engine


def _tick(engine: ColdStartGateEngine, lag: float) -> None:
    engine.tick_time_bound(
        "spy",
        symbol="SPY",
        timeframe="15Min",
        stream_health=_health(lag),
        matrix_aligned=True,
        background_verified=True,
        # Pass now= so wall_clock_freshness_ok uses our fixed clock.
        # tick_time_bound doesn't accept now= directly; we work around
        # this by pre-computing fresh ourselves and calling _evaluate_time_bound.
    )


def _max_age() -> float:
    return max_bar_age_for_timeframe("15Min", _CFG)  # 905.0s


def _warning_threshold() -> float:
    return _max_age() * STALE_FEED_WARNING_FRACTION  # ~452.5s


# ---------------------------------------------------------------------------
# Part 1 — warning emitted above threshold, NOT below
# ---------------------------------------------------------------------------

def test_no_warning_when_lag_below_threshold() -> None:
    """Lag well below 50% of max_bar_age → no warning."""
    engine = ColdStartGateEngine(config=_CFG)
    engine.begin_cold_start(["spy"], symbols={"spy": "SPY"})
    engine._state_by_strategy["spy"] = ColdStartGateState.STABILIZED_VERIFIED

    threshold = _warning_threshold()
    lag_below = threshold * 0.5  # clearly below

    with structlog.testing.capture_logs() as logs:
        engine._check_stale_feed_early_warning(
            "spy",
            symbol="SPY",
            timeframe="15Min",
            lag=lag_below,
            max_age_seconds=_max_age(),
            fresh=True,
        )

    warning_logs = [
        e for e in logs if e.get("event") == "cold_start_gate_stale_feed_early_warning"
    ]
    assert len(warning_logs) == 0


def test_warning_emitted_when_lag_above_threshold_but_still_fresh() -> None:
    """Lag above 50% of max_bar_age but still below max_age → warning fires."""
    engine = ColdStartGateEngine(config=_CFG)
    engine.begin_cold_start(["spy"], symbols={"spy": "SPY"})

    threshold = _warning_threshold()
    lag_above = threshold + 10.0  # above threshold, still below max_age=905

    with structlog.testing.capture_logs() as logs:
        engine._check_stale_feed_early_warning(
            "spy",
            symbol="SPY",
            timeframe="15Min",
            lag=lag_above,
            max_age_seconds=_max_age(),
            fresh=True,  # still fresh — gate hasn't fired yet
        )

    warning_logs = [
        e for e in logs if e.get("event") == "cold_start_gate_stale_feed_early_warning"
    ]
    assert len(warning_logs) == 1
    entry = warning_logs[0]
    assert entry["log_level"] == "warning"
    assert entry["strategy_id"] == "spy"
    assert entry["symbol"] == "SPY"
    assert entry["timeframe"] == "15Min"
    assert abs(entry["lag_seconds"] - lag_above) < 1e-6
    assert abs(entry["warning_threshold_seconds"] - threshold) < 1e-6
    assert abs(entry["demotion_threshold_seconds"] - _max_age()) < 1e-6


def test_no_warning_when_lag_exactly_at_threshold() -> None:
    """Lag exactly at threshold (not strictly above) → no warning."""
    engine = ColdStartGateEngine(config=_CFG)
    engine.begin_cold_start(["spy"], symbols={"spy": "SPY"})

    threshold = _warning_threshold()

    with structlog.testing.capture_logs() as logs:
        engine._check_stale_feed_early_warning(
            "spy",
            symbol="SPY",
            timeframe="15Min",
            lag=threshold,
            max_age_seconds=_max_age(),
            fresh=True,
        )

    warning_logs = [
        e for e in logs if e.get("event") == "cold_start_gate_stale_feed_early_warning"
    ]
    assert len(warning_logs) == 0


# ---------------------------------------------------------------------------
# Part 2 — de-duplication: only ONE warning per staleness event
# ---------------------------------------------------------------------------

def test_warning_fires_only_once_per_staleness_event() -> None:
    """Two consecutive calls with lag above threshold produce exactly one warning."""
    engine = ColdStartGateEngine(config=_CFG)
    engine.begin_cold_start(["spy"], symbols={"spy": "SPY"})

    lag_above = _warning_threshold() + 30.0

    with structlog.testing.capture_logs() as logs:
        # First call — should emit warning
        engine._check_stale_feed_early_warning(
            "spy", symbol="SPY", timeframe="15Min",
            lag=lag_above, max_age_seconds=_max_age(), fresh=True,
        )
        # Second call with same leg, still stale — should NOT emit again
        engine._check_stale_feed_early_warning(
            "spy", symbol="SPY", timeframe="15Min",
            lag=lag_above + 5.0, max_age_seconds=_max_age(), fresh=True,
        )

    warning_logs = [
        e for e in logs if e.get("event") == "cold_start_gate_stale_feed_early_warning"
    ]
    assert len(warning_logs) == 1, (
        f"Expected exactly 1 warning (de-duplication), got {len(warning_logs)}"
    )


def test_deduplication_is_per_strategy_id() -> None:
    """Two different strategy_ids each get their own warning."""
    engine = ColdStartGateEngine(config=_CFG)
    engine.begin_cold_start(["spy", "qqq"], symbols={"spy": "SPY", "qqq": "QQQ"})

    lag_above = _warning_threshold() + 50.0

    with structlog.testing.capture_logs() as logs:
        engine._check_stale_feed_early_warning(
            "spy", symbol="SPY", timeframe="15Min",
            lag=lag_above, max_age_seconds=_max_age(), fresh=True,
        )
        engine._check_stale_feed_early_warning(
            "qqq", symbol="QQQ", timeframe="15Min",
            lag=lag_above, max_age_seconds=_max_age(), fresh=True,
        )

    warning_logs = [
        e for e in logs if e.get("event") == "cold_start_gate_stale_feed_early_warning"
    ]
    assert len(warning_logs) == 2
    symbols_warned = {e["symbol"] for e in warning_logs}
    assert symbols_warned == {"SPY", "QQQ"}


# ---------------------------------------------------------------------------
# Part 3 — reset after freshness recovery
# ---------------------------------------------------------------------------

def test_warning_resets_after_freshness_recovery() -> None:
    """After lag drops below threshold, a new staleness event fires the warning again."""
    engine = ColdStartGateEngine(config=_CFG)
    engine.begin_cold_start(["spy"], symbols={"spy": "SPY"})

    lag_above = _warning_threshold() + 30.0
    lag_below = _warning_threshold() * 0.3

    with structlog.testing.capture_logs() as logs:
        # First staleness event — fires warning
        engine._check_stale_feed_early_warning(
            "spy", symbol="SPY", timeframe="15Min",
            lag=lag_above, max_age_seconds=_max_age(), fresh=True,
        )
        # Freshness recovered — lag drops below threshold → warning state reset
        engine._check_stale_feed_early_warning(
            "spy", symbol="SPY", timeframe="15Min",
            lag=lag_below, max_age_seconds=_max_age(), fresh=True,
        )
        # New staleness event — must fire warning again
        engine._check_stale_feed_early_warning(
            "spy", symbol="SPY", timeframe="15Min",
            lag=lag_above, max_age_seconds=_max_age(), fresh=True,
        )

    warning_logs = [
        e for e in logs if e.get("event") == "cold_start_gate_stale_feed_early_warning"
    ]
    assert len(warning_logs) == 2, (
        f"Expected 2 warnings (one per staleness event), got {len(warning_logs)}"
    )


def test_warning_resets_when_gate_actually_fires() -> None:
    """When fresh=False (gate fires), the warned flag is also cleared."""
    engine = ColdStartGateEngine(config=_CFG)
    engine.begin_cold_start(["spy"], symbols={"spy": "SPY"})

    lag_above = _warning_threshold() + 30.0

    # Arm the early warning
    engine._check_stale_feed_early_warning(
        "spy", symbol="SPY", timeframe="15Min",
        lag=lag_above, max_age_seconds=_max_age(), fresh=True,
    )
    assert "spy" in engine._stale_feed_warned

    # Gate fires (fresh=False) — reset
    engine._check_stale_feed_early_warning(
        "spy", symbol="SPY", timeframe="15Min",
        lag=_max_age() + 10.0, max_age_seconds=_max_age(), fresh=False,
    )
    assert "spy" not in engine._stale_feed_warned


# ---------------------------------------------------------------------------
# Part 4 — wall_clock_freshness_ok() return value unchanged
# ---------------------------------------------------------------------------

def test_wall_clock_freshness_ok_still_returns_true_below_max_age() -> None:
    """Below max_age → (True, lag) — unchanged."""
    now = _NOW
    ts = _ts(100.0)
    # H2: bar_period_seconds=0 isolates the pure lag-vs-threshold comparison (close==open).
    ok, lag = wall_clock_freshness_ok(ts, bar_period_seconds=0.0, now=now, max_age_seconds=905.0)
    assert ok is True
    assert abs(lag - 100.0) < 1.0


def test_wall_clock_freshness_ok_still_returns_false_above_max_age() -> None:
    """Above max_age → (False, lag) — unchanged."""
    now = _NOW
    ts = _ts(1000.0)
    ok, lag = wall_clock_freshness_ok(ts, bar_period_seconds=0.0, now=now, max_age_seconds=905.0)
    assert ok is False
    assert abs(lag - 1000.0) < 1.0


def test_wall_clock_freshness_ok_unchanged_when_lag_above_warning_threshold() -> None:
    """
    Lag above STALE_FEED_WARNING_FRACTION*max_age but below max_age → still (True, lag).
    The early-warning addition must not affect this return value.
    """
    threshold = _warning_threshold()
    lag = threshold + 50.0  # above warning threshold, below 905s gate
    now = _NOW
    ts = _ts(lag)
    ok, returned_lag = wall_clock_freshness_ok(ts, bar_period_seconds=0.0, now=now, max_age_seconds=905.0)
    assert ok is True, "Gate must not fire before max_age is crossed"
    assert abs(returned_lag - lag) < 1.0


def test_gate_demotes_to_stale_reverify_not_early_warning() -> None:
    """
    When lag exceeds max_age, the gate demotes to STALE_REVERIFY.
    The early-warning path does not prevent demotion.
    """
    engine = ColdStartGateEngine(config=_CFG)
    engine.begin_cold_start(["spy"], symbols={"spy": "SPY"})
    engine._state_by_strategy["spy"] = ColdStartGateState.STABILIZED_VERIFIED

    # Push lag well above max_age (905s for 15Min)
    stale_lag = 1000.0
    health = StreamHealthSnapshot(
        latest_bar_timestamp=_ts(stale_lag),
        prior_bar_timestamp=_ts(stale_lag + 900),
        latest_volume=1000.0,
        prior_volume=900.0,
        bar_count=2,
    )

    with structlog.testing.capture_logs():
        result = engine.tick_time_bound(
            "spy",
            symbol="SPY",
            timeframe="15Min",
            stream_health=health,
            matrix_aligned=True,
            background_verified=True,
        )

    assert engine.state("spy") == ColdStartGateState.STALE_REVERIFY
    assert result is not None
    assert result.new_state == ColdStartGateState.STALE_REVERIFY
