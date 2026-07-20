"""Tests for macro velocity shock detection and pre-emptive AI demotion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.engine.policy_lifecycle import (
    AIPolicyLifecycleManager,
    MACRO_VELOCITY_SHOCK_REASON,
    RollbackTier,
)
from src.engine.regime_intelligence import (
    CrossAssetStressDashboard,
    HYSTERESIS_RECOVERY_SIGMA,
    MacroVelocityReading,
    MacroVelocityTracker,
    SHOCK_COOL_OFF_REQUIRED_BARS,
    VELOCITY_MIN_SAMPLES,
    VELOCITY_OUTLIER_SIGMA,
)
from src.persistence import db as persistence
from src.router.risk_manager import (
    AI_POLICY_PASSIVE_SHADOW,
    AI_POLICY_PROBATIONAL,
    AI_POLICY_SOVEREIGN,
)


def _ingest_stable_samples(
    dashboard: CrossAssetStressDashboard,
    *,
    count: int,
    anchor: datetime,
    vix_proxy: float = 18.0,
) -> None:
    for i in range(count):
        snapshot = dashboard.proxy_snapshot(
            {
                "vix_proxy": vix_proxy + i * 0.02,
                "vix_term_proxy": 20.0,
                "vix_closes": (vix_proxy + i * 0.02 - 0.1, vix_proxy + i * 0.02),
                "hyg_close": 76.0,
                "lqd_close": 108.0,
                "spy_closes": tuple(400.0 + j * 0.1 for j in range(25)),
                "qqq_closes": tuple(350.0 + j * 0.1 for j in range(25)),
                "tlt_closes": tuple(90.0 - j * 0.01 for j in range(25)),
            }
        )
        stress = dashboard.evaluate(snapshot)
        dashboard.record_macro_velocity_sample(
            snapshot,
            stress,
            anchor_time=anchor + timedelta(minutes=20 * i),
        )


def test_velocity_shock_not_triggered_on_stable_window() -> None:
    dashboard = CrossAssetStressDashboard(
        velocity_tracker=MacroVelocityTracker(
            min_samples=VELOCITY_MIN_SAMPLES,
            outlier_sigma=VELOCITY_OUTLIER_SIGMA,
        )
    )
    anchor = datetime(2026, 6, 24, 14, 0, tzinfo=timezone.utc)
    _ingest_stable_samples(dashboard, count=10, anchor=anchor)
    verdict = dashboard.detect_velocity_shock_event(
        anchor_time=anchor + timedelta(hours=3)
    )
    assert verdict.shock_detected is False
    assert verdict.reason == "velocity_within_bounds"


def test_velocity_shock_detects_vix_day_surge() -> None:
    dashboard = CrossAssetStressDashboard(
        velocity_tracker=MacroVelocityTracker(
            min_samples=VELOCITY_MIN_SAMPLES,
            outlier_sigma=VELOCITY_OUTLIER_SIGMA,
        )
    )
    anchor = datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc)
    _ingest_stable_samples(dashboard, count=8, anchor=anchor, vix_proxy=18.0)
    shock_time = anchor + timedelta(hours=3)
    shock_snapshot = dashboard.proxy_snapshot(
        {
            "vix_proxy": 28.0,
            "vix_term_proxy": 22.0,
            "vix_closes": (18.0, 28.0),
            "hyg_close": 72.0,
            "lqd_close": 110.0,
            "spy_closes": tuple(390.0 - i for i in range(25)),
            "qqq_closes": tuple(340.0 - i * 1.2 for i in range(25)),
            "tlt_closes": tuple(95.0 - i * 0.2 for i in range(25)),
        }
    )
    shock_stress = dashboard.evaluate(shock_snapshot)
    dashboard.record_macro_velocity_sample(
        shock_snapshot,
        shock_stress,
        anchor_time=shock_time,
    )
    verdict = dashboard.detect_velocity_shock_event(anchor_time=shock_time)
    assert verdict.shock_detected is True
    assert any("vix_day_pct_surge" in metric for metric in verdict.triggered_metrics)


def test_velocity_shock_insufficient_samples() -> None:
    tracker = MacroVelocityTracker(min_samples=6)
    verdict = tracker.detect_velocity_shock_event()
    assert verdict.shock_detected is False
    assert verdict.reason == "insufficient_velocity_samples"


def test_velocity_shock_demotes_sovereign_immediately(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    persistence.ensure_ai_policy_lifecycle_table(db_path)
    persistence.upsert_ai_policy_lifecycle_state(
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        execution_state=AI_POLICY_SOVEREIGN,
        probation_started_at=None,
        probation_clean_trading_days=14,
        db_path=db_path,
    )
    manager = AIPolicyLifecycleManager(db_path=db_path)
    rollback = manager.execute_velocity_shock_demotion(
        AI_POLICY_SOVEREIGN,
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        shock_reason="vix_day_pct_surge:hard_floor:0.55",
    )
    assert rollback.tier == RollbackTier.VELOCITY_SHOCK_DEMOTION
    assert rollback.new_state == AI_POLICY_PASSIVE_SHADOW
    stored = persistence.get_ai_policy_lifecycle_state("mean_reversion_qqq", db_path)
    assert stored is not None
    assert stored["execution_state"] == AI_POLICY_PASSIVE_SHADOW


def test_velocity_shock_demotes_probational_via_batch_handler(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    persistence.ensure_ai_policy_lifecycle_table(db_path)
    persistence.upsert_ai_policy_lifecycle_state(
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        execution_state=AI_POLICY_PROBATIONAL,
        probation_started_at=datetime.now(timezone.utc).isoformat(),
        probation_clean_trading_days=3,
        db_path=db_path,
    )
    manager = AIPolicyLifecycleManager(db_path=db_path)
    result = manager.handle_macro_velocity_shock(
        {
            "shock_detected": True,
            "reason": "avg_pairwise_correlation:intraday_spike:0.20",
            "triggered_metrics": ("avg_pairwise_correlation:intraday_spike:0.20",),
            "max_z_score": 4.2,
        },
        [("mean_reversion_qqq", "QQQ", {"ai_policy_execution_state": AI_POLICY_PROBATIONAL})],
    )
    assert result.demoted is True
    assert len(result.transitions) == 1
    assert result.transitions[0].from_state == AI_POLICY_PROBATIONAL
    assert result.transitions[0].to_state == AI_POLICY_PASSIVE_SHADOW
    assert result.transitions[0].metadata["bypass"] == "macro_velocity_shock"


def test_velocity_shock_skips_passive_legs(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    persistence.ensure_ai_policy_lifecycle_table(db_path)
    manager = AIPolicyLifecycleManager(db_path=db_path)
    result = manager.handle_macro_velocity_shock(
        {
            "shock_detected": True,
            "reason": MACRO_VELOCITY_SHOCK_REASON,
            "triggered_metrics": ("composite_stress:level:3.5σ",),
            "max_z_score": 3.5,
        },
        [("mean_reversion_qqq", "QQQ", {"ai_policy_execution_state": AI_POLICY_PASSIVE_SHADOW})],
    )
    assert result.demoted is False
    assert result.transitions == ()


def test_regime_stabilization_requires_full_cool_off_window() -> None:
    dashboard = CrossAssetStressDashboard(
        velocity_tracker=MacroVelocityTracker(
            min_samples=VELOCITY_MIN_SAMPLES,
            outlier_sigma=VELOCITY_OUTLIER_SIGMA,
            shock_cool_off_required_bars=5,
        )
    )
    anchor = datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc)
    _ingest_stable_samples(dashboard, count=8, anchor=anchor, vix_proxy=18.0)
    shock_time = anchor + timedelta(hours=3)
    shock_snapshot = dashboard.proxy_snapshot(
        {
            "vix_proxy": 28.0,
            "vix_term_proxy": 22.0,
            "vix_closes": (18.0, 28.0),
            "hyg_close": 72.0,
            "lqd_close": 110.0,
            "spy_closes": tuple(390.0 - i for i in range(25)),
            "qqq_closes": tuple(340.0 - i * 1.2 for i in range(25)),
            "tlt_closes": tuple(95.0 - i * 0.2 for i in range(25)),
        }
    )
    shock_stress = dashboard.evaluate(shock_snapshot)
    dashboard.record_macro_velocity_sample(
        shock_snapshot,
        shock_stress,
        anchor_time=shock_time,
    )
    dashboard.detect_velocity_shock_event(anchor_time=shock_time)
    verdict = dashboard.is_regime_stabilized()
    assert verdict.stabilized is False
    assert verdict.reason == "cool_off_window_incomplete"

    for i in range(5):
        sample_time = shock_time + timedelta(minutes=20 * (i + 1))
        snapshot = dashboard.proxy_snapshot(
            {
                "vix_proxy": 18.0 + i * 0.02,
                "vix_term_proxy": 20.0,
                "vix_closes": (18.0 + i * 0.02 - 0.1, 18.0 + i * 0.02),
                "hyg_close": 76.0,
                "lqd_close": 108.0,
                "spy_closes": tuple(400.0 + j * 0.1 for j in range(25)),
                "qqq_closes": tuple(350.0 + j * 0.1 for j in range(25)),
                "tlt_closes": tuple(90.0 - j * 0.01 for j in range(25)),
            }
        )
        stress = dashboard.evaluate(snapshot)
        dashboard.record_macro_velocity_sample(snapshot, stress, anchor_time=sample_time)

    stabilized = dashboard.is_regime_stabilized()
    assert stabilized.stabilized is True
    assert stabilized.stable_sub_sigma_bars >= 5
    assert stabilized.composite_stress_z_score < HYSTERESIS_RECOVERY_SIGMA
    assert stabilized.recovery_sigma_threshold == HYSTERESIS_RECOVERY_SIGMA


def test_hysteresis_blocks_stabilization_above_recovery_sigma() -> None:
    tracker = MacroVelocityTracker(
        min_samples=VELOCITY_MIN_SAMPLES,
        shock_cool_off_required_bars=3,
    )
    anchor = datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc)
    tracker.register_shock_event(anchor)
    for i in range(4):
        reading = MacroVelocityReading(
            timestamp=anchor + timedelta(minutes=20 * (i + 1)),
            vix_day_pct_surge=0.02,
            vix_term_spread=0.01,
            credit_spread_change=0.001,
            avg_pairwise_correlation=0.55,
            composite_stress=0.5,
        )
        tracker._samples.append(reading)
    tracker._stable_sub_sigma_bars = 3
    tracker.composite_stress_z_score = lambda: 2.0  # type: ignore[method-assign]
    verdict = tracker.is_regime_stabilized(anchor_time=anchor + timedelta(hours=2))
    assert verdict.stabilized is False
    assert verdict.reason == "composite_stress_above_hysteresis_threshold"


def test_escalating_cool_off_penalty_after_repeat_demotions() -> None:
    tracker = MacroVelocityTracker(shock_cool_off_required_bars=40)
    t0 = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    tracker.register_velocity_shock_demotion(anchor_time=t0 - timedelta(days=2))
    tracker.register_velocity_shock_demotion(anchor_time=t0 - timedelta(days=1))
    assert tracker.effective_cool_off_required_bars(anchor_time=t0) == 80
    assert tracker.velocity_shock_demotion_count_7d(anchor_time=t0) == 2


def test_shock_cool_off_default_is_forty_bars() -> None:
    tracker = MacroVelocityTracker()
    assert tracker.shock_cool_off_required_bars == SHOCK_COOL_OFF_REQUIRED_BARS
    assert SHOCK_COOL_OFF_REQUIRED_BARS == 40
