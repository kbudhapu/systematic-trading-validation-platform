"""Unit tests for reconciliation math (no network)."""

from __future__ import annotations

import pytest

from src.router.risk_manager import (
    SESSION_CLOSING_IMBALANCE,
    SESSION_MIDDAY_DOLDRUMS,
    SESSION_OPENING_CROSS,
)
from src.engine.attribution import (
    EXEC_DIRECTION_LONG_EXIT,
    classify_execution_direction,
)
from src.engine.slippage_calibration import EXEC_DIRECTION_LONG_ENTRY
from src.engine.attribution_reconcile import (
    ReplayResult,
    SlippageModelCalibrator,
    TrackingErrorAnalyzer,
    aligned_daily_variance,
)


def test_aligned_daily_variance() -> None:
    live = {"2026-01-01": 100.0, "2026-01-02": -50.0, "2026-01-03": 25.0}
    replay = {"2026-01-01": 90.0, "2026-01-02": -40.0, "2026-01-03": 20.0}
    variance, breakdown = aligned_daily_variance(live, replay)
    assert variance > 0.0
    assert len(breakdown) == 3


def test_tracking_error_analyzer_breach() -> None:
    analyzer = TrackingErrorAnalyzer(variance_tolerance=1.0, pnl_tolerance=10.0)
    live_rows = [
        {
            "symbol": "QQQ",
            "pnl": 500.0,
            "session_type": SESSION_OPENING_CROSS,
            "regime_id": "CALM_MR",
            "execution_tactic": "PASSIVE",
            "timestamp": "2026-01-15T15:00:00+00:00",
        },
        {
            "symbol": "QQQ",
            "pnl": -100.0,
            "session_type": SESSION_MIDDAY_DOLDRUMS,
            "regime_id": "CALM_MR",
            "execution_tactic": "AGGRESSIVE",
            "timestamp": "2026-01-16T15:00:00+00:00",
        },
    ]
    replay = [
        ReplayResult(
            symbol="QQQ",
            regime="CALM_MR",
            period_pnl=50.0,
            period_return=0.0005,
            trades=4,
            effective_slippage_pct=0.0006,
            spread_proxy_pct=0.0002,
            participation_cap_pct=0.95,
            daily_pnl={"2026-01-15": 200.0, "2026-01-16": -150.0},
        )
    ]
    report = analyzer.analyze(
        lookback_days=30,
        live_rows=live_rows,
        replay_results=replay,
    )
    assert report.live_total_pnl == 400.0
    assert report.replay_total_pnl == 50.0
    assert report.breach is True


def test_slippage_calibrator_median_multipliers() -> None:
    calibrator = SlippageModelCalibrator(base_slippage_pct=0.0005)
    rows = [
        {
            "session_type": SESSION_OPENING_CROSS,
            "execution_direction_type": EXEC_DIRECTION_LONG_EXIT,
            "slippage_pct": 0.0010,
            "slip_direction_long_exit": 0.0010,
            "markout_5bar": -0.0008,
        },
        {
            "session_type": SESSION_OPENING_CROSS,
            "execution_direction_type": EXEC_DIRECTION_LONG_EXIT,
            "slippage_pct": 0.0012,
            "slip_direction_long_exit": 0.0012,
            "markout_5bar": -0.0010,
        },
        {
            "session_type": SESSION_MIDDAY_DOLDRUMS,
            "execution_direction_type": EXEC_DIRECTION_LONG_ENTRY,
            "slippage_pct": 0.0005,
            "slip_direction_long_entry": 0.0005,
            "markout_5bar": -0.0003,
        },
        {
            "session_type": SESSION_CLOSING_IMBALANCE,
            "execution_direction_type": "short_exit",
            "slippage_pct": 0.00075,
            "slip_direction_short_exit": 0.00075,
            "markout_5bar": None,
        },
    ]
    result = calibrator.calibrate(rows)
    assert result.calibrated_multipliers[SESSION_OPENING_CROSS]["long_exit"] > 1.5
    assert result.sample_counts[SESSION_OPENING_CROSS]["long_exit"] == 2
    assert result.calibrated_multipliers[SESSION_MIDDAY_DOLDRUMS]["long_entry"] == pytest.approx(
        0.8
    )
    assert (
        result.calibrated_multipliers[SESSION_OPENING_CROSS]["long_exit"]
        > result.calibrated_multipliers[SESSION_MIDDAY_DOLDRUMS]["long_entry"]
    )


def test_classify_execution_direction_buckets() -> None:
    assert classify_execution_direction("sell", position_side_before="long") == (
        EXEC_DIRECTION_LONG_EXIT
    )
    assert classify_execution_direction("buy", position_side_before="flat") == (
        EXEC_DIRECTION_LONG_ENTRY
    )
