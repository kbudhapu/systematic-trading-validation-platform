from __future__ import annotations

from collections import deque
from pathlib import Path

import numpy as np

from src.config import StrategyConfig
from src.engine.portfolio_risk_governor import PortfolioRiskGovernor
from src.models import Position, SignalAction


class _Window:
    def __init__(self, closes: np.ndarray) -> None:
        self._closes = closes

    def closes_array(self) -> np.ndarray:
        return self._closes


def _cfg(strategy_id: str, symbol: str) -> StrategyConfig:
    return StrategyConfig(
        strategy_id=strategy_id,
        module="mean_reversion_qqq",
        symbol=symbol,
        timeframe="15Min",
        poll_interval_seconds=900,
        params={},
        enabled=True,
        environment="paper",
    )


def test_correlation_clamp_engages_for_highly_correlated_legs() -> None:
    # FIX-B backstop: this is the CASE-1/CASE-3 live home for the retired QQQ/SPY price ladder --
    # QQQ/SPY strategy-return correlation >0.85 -> BOTH legs clamped to 0.6x (stricter + gentler
    # than the old disable-cliff). Proves nothing falls through when the ladder is removed.
    governor = PortfolioRiskGovernor(max_safe_correlation=0.85, clamp_multiplier=0.6)
    closes = np.linspace(100.0, 120.0, 64, dtype=np.float64)
    verdict = governor.evaluate(
        enabled_configs=[
            _cfg("mean_reversion_qqq", "QQQ"),
            _cfg("mean_reversion_spy", "SPY"),
        ],
        windows={
            "mean_reversion_qqq": _Window(closes),
            "mean_reversion_spy": _Window(closes * 1.01),
        },
        positions=[],
        realized_pnls={"mean_reversion_qqq": 0.0, "mean_reversion_spy": 0.0},
        broker_equity=50_000.0,
        allocation_fractions={"mean_reversion_qqq": 1.0, "mean_reversion_spy": 1.0},
    )
    assert verdict.sizing_multipliers["mean_reversion_qqq"] == 0.6
    assert verdict.sizing_multipliers["mean_reversion_spy"] == 0.6


def test_strategy_drawdown_breaker_sets_exit_only() -> None:
    governor = PortfolioRiskGovernor(strategy_max_drawdown=0.06)
    governor._equity_history_by_strategy["mean_reversion_qqq"] = deque(
        [100_000.0, 97_500.0, 92_000.0],
        maxlen=256,
    )
    verdict = governor.evaluate(
        enabled_configs=[_cfg("mean_reversion_qqq", "QQQ")],
        windows={"mean_reversion_qqq": _Window(np.linspace(100.0, 95.0, 64))},
        positions=[Position(symbol="QQQ", qty=10.0, side="long", avg_entry_price=100.0, unrealized_pl=-500.0)],
        realized_pnls={"mean_reversion_qqq": -7_500.0},
        broker_equity=100_000.0,
        allocation_fractions={"mean_reversion_qqq": 1.0},
    )
    assert "mean_reversion_qqq" in verdict.exit_only_strategy_ids
    assert governor.blocks_new_entries(
        "mean_reversion_qqq",
        signal_action=SignalAction.LONG,
        verdict=verdict,
    )
    assert not governor.blocks_new_entries(
        "mean_reversion_qqq",
        signal_action=SignalAction.EXIT,
        verdict=verdict,
    )


def test_serialize_and_rehydrate_risk_state(tmp_path: Path) -> None:
    governor = PortfolioRiskGovernor(strategy_max_drawdown=0.06)
    governor._strategy_peaks["mean_reversion_qqq"] = 100_000.0
    governor._strategy_drawdowns["mean_reversion_qqq"] = 0.08
    governor._strategy_exit_only.add("mean_reversion_qqq")

    payload = governor.serialize_risk_state()
    assert payload["strategies"]["mean_reversion_qqq"] == {
        "peak_equity": 100_000.0,
        "trailing_drawdown_pct": 0.08,
        "exit_only_mode": True,
    }

    fresh = PortfolioRiskGovernor(strategy_max_drawdown=0.06)
    assert fresh.rehydrate_risk_state(payload) is True
    assert fresh._strategy_peaks["mean_reversion_qqq"] == 100_000.0
    assert fresh._strategy_drawdowns["mean_reversion_qqq"] == 0.08
    assert "mean_reversion_qqq" in fresh._strategy_exit_only


def test_rehydrate_without_snapshot_initializes_baselines() -> None:
    governor = PortfolioRiskGovernor()
    assert governor.rehydrate_risk_state(
        None,
        strategy_baselines={"mean_reversion_qqq": 50_000.0},
    ) is False
    # GV-3: a baseline is a NOTIONAL capital figure and the tracked curve is now PnL, so the
    # correct seed for a leg that has not traded is peak PnL 0.0. Seeding 50_000 here would assert
    # a peak PROFIT of 50k and report an instant ~49% drawdown on the first flat reading. The key
    # set is still honoured — the leg is registered — only the value is (deliberately) ignored.
    assert governor._strategy_peaks["mean_reversion_qqq"] == 0.0
    assert governor._strategy_drawdowns["mean_reversion_qqq"] == 0.0
    assert "mean_reversion_qqq" not in governor._strategy_exit_only


def test_maybe_enqueue_persist_writes_snapshot(tmp_path: Path) -> None:
    from src.persistence.db_queue import get_async_db_writer, stop_async_db_writer
    from src.persistence.portfolio_risk_state_store import load_portfolio_risk_state

    db_path = tmp_path / "trading.db"
    governor = PortfolioRiskGovernor()
    governor._strategy_peaks["mean_reversion_qqq"] = 100_000.0
    governor._strategy_drawdowns["mean_reversion_qqq"] = 0.02
    governor.maybe_enqueue_persist(db_path=db_path)

    writer = get_async_db_writer()
    writer.start()
    writer.stop(timeout_seconds=2.0)
    stop_async_db_writer()

    stored = load_portfolio_risk_state(db_path)
    assert stored is not None
    assert stored["strategies"]["mean_reversion_qqq"]["peak_equity"] == 100_000.0
    assert stored["strategies"]["mean_reversion_qqq"]["trailing_drawdown_pct"] == 0.02


def test_cold_start_clamp_applied_when_bars_below_threshold() -> None:
    """Legs with < MIN_CORRELATION_SAMPLES bars get CORRELATION_CLAMP_MULTIPLIER."""
    from src.engine.portfolio_risk_governor import CORRELATION_CLAMP_MULTIPLIER, MIN_CORRELATION_SAMPLES

    governor = PortfolioRiskGovernor()
    cfg = _cfg("mean_reversion_spy", "SPY")

    # Window with fewer bars than threshold
    short_window = _Window(np.ones(MIN_CORRELATION_SAMPLES - 1))
    verdict = governor.evaluate(
        enabled_configs=[cfg],
        windows={"mean_reversion_spy": short_window},
        positions=[],
        realized_pnls={},
        broker_equity=100_000.0,
        allocation_fractions={"mean_reversion_spy": 1.0},
    )

    assert verdict.sizing_multipliers["mean_reversion_spy"] == CORRELATION_CLAMP_MULTIPLIER
    assert "mean_reversion_spy" in verdict.cold_start_clamped_strategy_ids


def test_cold_start_clamp_not_applied_when_bars_sufficient() -> None:
    """Legs with >= MIN_CORRELATION_SAMPLES bars keep sizing_multiplier = 1.0."""
    from src.engine.portfolio_risk_governor import MIN_CORRELATION_SAMPLES

    governor = PortfolioRiskGovernor()
    cfg = _cfg("mean_reversion_spy", "SPY")

    # Window with enough bars
    full_window = _Window(np.linspace(100.0, 110.0, MIN_CORRELATION_SAMPLES))
    verdict = governor.evaluate(
        enabled_configs=[cfg],
        windows={"mean_reversion_spy": full_window},
        positions=[],
        realized_pnls={},
        broker_equity=100_000.0,
        allocation_fractions={"mean_reversion_spy": 1.0},
    )

    assert verdict.sizing_multipliers["mean_reversion_spy"] == 1.0
    assert "mean_reversion_spy" not in verdict.cold_start_clamped_strategy_ids


def test_drawdown_invariant_to_leg_enablement_count_and_order() -> None:
    """E5 regression (the screenshot pathology): a leg's trailing drawdown / exit_only depends
    ONLY on (its allocation fraction, its PnL, its position) — NOT on how many OTHER legs are
    enabled, nor the enable ORDER. The retired equity/n_enabled base made a leg's risk gate move
    when unrelated legs were toggled; broker_equity × the leg's own intended fraction does not."""
    broker_equity = 100_000.0
    qqq = _cfg("mean_reversion_qqq", "QQQ")
    spy = _cfg("mean_reversion_spy", "SPY")
    gld = _cfg("trend_gld", "GLD")

    def run(configs):
        gov = PortfolioRiskGovernor(strategy_max_drawdown=0.06)
        # GV-3: this deque is now a PnL curve, not a notional-equity curve. The old seed of
        # 20_000.0 (QQQ's 0.2 fraction of broker equity) would now assert a peak PROFIT of 20k and
        # score the -1_300 against it as a 21_300 fall. A leg that has not profited seeds at 0.0.
        gov._equity_history_by_strategy["mean_reversion_qqq"] = deque([0.0], maxlen=256)
        # Every leg carries its OWN fixed intended fraction — QQQ's is 0.2 in every scenario.
        fractions = {"mean_reversion_qqq": 0.2, "mean_reversion_spy": 0.3, "trend_gld": 0.25}
        return gov.evaluate(
            enabled_configs=configs,
            windows={c.strategy_id: _Window(np.linspace(100.0, 95.0, 64)) for c in configs},
            positions=[],
            realized_pnls={"mean_reversion_qqq": -1_300.0},  # 20_000 → 18_700 = 6.5% dd
            broker_equity=broker_equity,
            allocation_fractions=fractions,
        )

    solo = run([qqq]).strategy_drawdowns["mean_reversion_qqq"]
    qqq_first = run([qqq, spy, gld]).strategy_drawdowns["mean_reversion_qqq"]
    qqq_last = run([gld, spy, qqq]).strategy_drawdowns["mean_reversion_qqq"]

    # GV-3 SENSITIVITY CHANGE — READ BEFORE MERGE. This assertion moved 0.065 → 0.013 and that is
    # a real change in what the 0.06 gate catches, not a fixture detail.
    #   old: -1_300 ÷ 20_000 leg capital  (0.2 × broker equity) = 0.065  → TRIPPED the 0.06 clamp
    #   new: -1_300 ÷ 100_000 peak account equity                = 0.013 → does NOT trip
    # The 0.06 constant is untouched per the ruling, but the denominator had to change: leg
    # capital is *defined* by the allocation fraction GV-3 bars from the measurement, so no
    # allocation-free per-leg capital reference exists to divide by. Net effect: a leg must now
    # lose 6% of the ACCOUNT, not 6% of its own slice, to be clamped — materially less sensitive
    # for small-allocation legs (a 0.2-allocation leg needs a 30% loss of its own capital).
    # Flagged for operator ratification; if tighter per-leg sensitivity is wanted the honest lever
    # is lowering strategy_max_drawdown, NOT restoring an allocation-derived denominator.
    assert abs(solo - 0.013) < 1e-9
    # identical regardless of the enabled set size or the enable order → pathology fixed
    # (this invariance — the test's actual subject — holds under both formulas)
    assert solo == qqq_first == qqq_last

