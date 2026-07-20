"""Tests for centralized portfolio brain and coordinator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.engine.portfolio_brain import (
    FORCE_LIQUIDATION_EXECUTE,
    FORCED_DURATION_EXHAUSTION,
    GLOBAL_ENTRY_LOCKOUT,
    QQQ_STRATEGY_ID,
    SPY_STRATEGY_ID,
    ActivePositionManifest,
    LegMetrics,
    PORTFOLIO_MODE_DUAL_LEG,
    PORTFOLIO_MODE_SPY_DISABLED,
    PortfolioBrain,
    ProposedPosition,
    _symbol_beta,
    _symbol_factors,
    _symbol_sector,
)
from src.engine.portfolio_coordinator import (
    LegCyclePayload,
    PortfolioCoordinator,
    ensure_portfolio_constraint_schema,
)
from src.models import Signal, SignalAction
from src.router.default import OrderRouter
from src.config import RiskConfig


def _metrics(
    strategy_id: str,
    symbol: str,
    *,
    vol: float = 0.15,
    sharpe: float = 0.5,
    health: float = 1.0,
    enabled: bool = True,
    champion: float = 2.0,
) -> LegMetrics:
    return LegMetrics(
        strategy_id=strategy_id,
        symbol=symbol,
        realized_vol=vol,
        drawdown_contribution=0.1,
        marginal_sharpe=sharpe,
        health_score=health,
        champion_score=champion,
        enabled=enabled,
    )


def test_allocate_risk_budgets_vol_parity() -> None:
    brain = PortfolioBrain()
    metrics = {
        QQQ_STRATEGY_ID: _metrics(QQQ_STRATEGY_ID, "QQQ", vol=0.20),
        SPY_STRATEGY_ID: _metrics(SPY_STRATEGY_ID, "SPY", vol=0.10),
    }
    budgets = brain.allocate_risk_budgets(metrics)
    assert budgets[SPY_STRATEGY_ID] > budgets[QQQ_STRATEGY_ID]
    assert pytest.approx(sum(budgets.values()), rel=1e-6) == 1.0


def test_static_equal_split_ignores_vol_flicker() -> None:
    """Commissioning pin (07-20 oscillation): with static_equal_split, the budget is EQUAL across
    enabled legs regardless of vol -- so a dormant leg whose vol collapses to VOL_FLOOR (the
    data-availability artifact that flipped QQQ 0.845<->0.227) can no longer move the allocation.
    Contrast: the SAME two legs under vol-parity give an UNEQUAL split (asserted above)."""
    from src.engine.portfolio_brain import VOL_FLOOR
    brain = PortfolioBrain(static_equal_split=True)
    # one leg at a real vol, the other collapsed to the floor (the dormant-leg failure mode)
    metrics = {
        QQQ_STRATEGY_ID: _metrics(QQQ_STRATEGY_ID, "QQQ", vol=VOL_FLOOR),   # "no data" -> floor
        SPY_STRATEGY_ID: _metrics(SPY_STRATEGY_ID, "SPY", vol=0.20),
    }
    budgets = brain.allocate_risk_budgets(metrics)
    assert budgets[QQQ_STRATEGY_ID] == pytest.approx(0.5)
    assert budgets[SPY_STRATEGY_ID] == pytest.approx(0.5)
    assert pytest.approx(sum(budgets.values()), rel=1e-6) == 1.0
    # default (flag off) is unchanged: same inputs give the vol-parity (unequal) split
    off = PortfolioBrain().allocate_risk_budgets(metrics)
    assert off[SPY_STRATEGY_ID] != pytest.approx(off[QQQ_STRATEGY_ID])


def test_static_equal_split_disabled_leg_gets_zero() -> None:
    brain = PortfolioBrain(static_equal_split=True)
    metrics = {
        QQQ_STRATEGY_ID: _metrics(QQQ_STRATEGY_ID, "QQQ", vol=0.20),
        SPY_STRATEGY_ID: _metrics(SPY_STRATEGY_ID, "SPY", vol=0.10, enabled=False),
    }
    budgets = brain.allocate_risk_budgets(metrics)
    assert budgets[QQQ_STRATEGY_ID] == pytest.approx(1.0)
    assert budgets[SPY_STRATEGY_ID] == pytest.approx(0.0)


def test_spy_disabled_when_config_off() -> None:
    brain = PortfolioBrain()
    metrics = {
        QQQ_STRATEGY_ID: _metrics(QQQ_STRATEGY_ID, "QQQ"),
        SPY_STRATEGY_ID: _metrics(SPY_STRATEGY_ID, "SPY", enabled=False),
    }
    mode = brain.resolve_portfolio_mode(
        metrics,
        qqq_spy_correlation=0.5,
        enabled_strategy_ids=frozenset({QQQ_STRATEGY_ID}),
    )
    assert mode.spy_leg_enabled is False
    assert mode.single_leg_mode is True
    assert mode.dominant_strategy_id == QQQ_STRATEGY_ID


def test_retired_ladder_high_price_correlation_runs_dual_leg() -> None:
    """FIX-B: the QQQ/SPY PRICE-correlation ladder is RETIRED. A price correlation that the old
    ladder would have acted on (>=0.92 -> SPY disabled, >=0.88 -> single-leg) now leaves BOTH legs
    running -- QQQ/SPY are governed on STRATEGY-return correlation by the governor 0.85 clamp
    (proven in test_portfolio_risk_governor.test_correlation_clamp_engages_for_highly_correlated_legs)
    like any other pair, not by a price cliff."""
    brain = PortfolioBrain()
    metrics = {
        QQQ_STRATEGY_ID: _metrics(QQQ_STRATEGY_ID, "QQQ"),
        SPY_STRATEGY_ID: _metrics(SPY_STRATEGY_ID, "SPY"),
    }
    mode = brain.resolve_portfolio_mode(metrics, qqq_spy_correlation=0.95)
    assert mode.mode == PORTFOLIO_MODE_DUAL_LEG
    assert mode.spy_leg_enabled is True
    assert mode.single_leg_mode is False
    assert mode.reason == "dual_leg_default"


def test_case4_spy_health_floor_still_disables_after_ladder_retired() -> None:
    """CASE 4 (SPY strategy broken) is a HEALTH gate, not the retired price ladder -- it survives.
    Even at near-perfect price correlation, disable comes from health, not correlation."""
    brain = PortfolioBrain()
    metrics = {
        QQQ_STRATEGY_ID: _metrics(QQQ_STRATEGY_ID, "QQQ"),
        SPY_STRATEGY_ID: _metrics(SPY_STRATEGY_ID, "SPY", health=0.2),  # < SPY_DISABLE_HEALTH_FLOOR
    }
    mode = brain.resolve_portfolio_mode(metrics, qqq_spy_correlation=0.99)
    assert mode.mode == PORTFOLIO_MODE_SPY_DISABLED
    assert mode.reason == "spy_health_below_floor"


def test_validate_global_exposure_hard_cap() -> None:
    brain = PortfolioBrain(equity=100_000.0)
    proposed = [
        ProposedPosition(
            strategy_id=QQQ_STRATEGY_ID,
            symbol="QQQ",
            side="long",
            notional=180_000.0,
            beta=1.15,
            sector="US_LARGE_CAP_GROWTH",
            factor_loadings=(1.15, -0.2, 0.3),
        )
    ]
    result = brain.validate_global_exposure(proposed)
    assert result.allowed is False
    assert "gross_exposure_cap" in result.breach_codes or "net_beta_cap" in result.breach_codes
    assert result.sizing_multipliers[QQQ_STRATEGY_ID] == 0.0


# ---------------------------------------------------------------------------
# BTC / GLD / USO beta, sector, factor coverage (previously silently fell
# back to beta=1.0 / sector="US_EQUITY_OTHER" / factors=(1.0, 0.0, 0.0))
# ---------------------------------------------------------------------------

def test_symbol_beta_returns_researched_values_not_equity_fallback() -> None:
    assert _symbol_beta("GLD") == 0.05
    assert _symbol_beta("USO") == 0.30
    assert _symbol_beta("BTC/USD") == 2.00
    # None of these should silently land on the 1.0 fallback meant for
    # symbols with no researched entry at all.
    for symbol in ("GLD", "USO", "BTC/USD"):
        assert _symbol_beta(symbol) != 1.0


def test_symbol_sector_returns_researched_values_not_equity_fallback() -> None:
    assert _symbol_sector("GLD") == "PRECIOUS_METALS"
    assert _symbol_sector("USO") == "ENERGY_COMMODITIES"
    assert _symbol_sector("BTC/USD") == "CRYPTO"
    for symbol in ("GLD", "USO", "BTC/USD"):
        assert _symbol_sector(symbol) != "US_EQUITY_OTHER"


def test_symbol_factors_have_no_fabricated_equity_style_tilt() -> None:
    """GLD/USO/BTC aren't equities -- only the market-beta-like first
    component should be populated, not a fabricated value/momentum tilt."""
    for symbol, expected_beta in (("GLD", 0.05), ("USO", 0.30), ("BTC/USD", 2.00)):
        factors = _symbol_factors(symbol)
        assert factors == (expected_beta, 0.0, 0.0)


def test_unresearched_symbol_still_falls_back_to_defaults() -> None:
    """Confirm the fallback path itself still works for a genuinely unknown
    symbol -- the new entries shouldn't have broken the .get(..., default)."""
    assert _symbol_beta("XYZ_UNKNOWN") == 1.0
    assert _symbol_sector("XYZ_UNKNOWN") == "US_EQUITY_OTHER"
    assert _symbol_factors("XYZ_UNKNOWN") == (1.0, 0.0, 0.0)


def test_validate_global_exposure_mixed_five_leg_portfolio_sane_net_beta() -> None:
    """A mixed SPY/QQQ/BTC/GLD/USO portfolio should NOT compute net_beta as
    if every leg had equity-like beta -- confirms the researched values are
    actually being used, not silently falling back to 1.0 for the three
    new symbols."""
    brain = PortfolioBrain(equity=1_000_000.0)
    proposed = [
        ProposedPosition(
            strategy_id="mean_reversion_spy", symbol="SPY", side="long",
            notional=100_000.0, beta=_symbol_beta("SPY"),
            sector=_symbol_sector("SPY"), factor_loadings=_symbol_factors("SPY"),
        ),
        ProposedPosition(
            strategy_id="mean_reversion_qqq", symbol="QQQ", side="long",
            notional=100_000.0, beta=_symbol_beta("QQQ"),
            sector=_symbol_sector("QQQ"), factor_loadings=_symbol_factors("QQQ"),
        ),
        ProposedPosition(
            strategy_id="btc_leg", symbol="BTC/USD", side="long",
            notional=50_000.0, beta=_symbol_beta("BTC/USD"),
            sector=_symbol_sector("BTC/USD"), factor_loadings=_symbol_factors("BTC/USD"),
        ),
        ProposedPosition(
            strategy_id="gld_leg", symbol="GLD", side="long",
            notional=50_000.0, beta=_symbol_beta("GLD"),
            sector=_symbol_sector("GLD"), factor_loadings=_symbol_factors("GLD"),
        ),
        ProposedPosition(
            strategy_id="uso_leg", symbol="USO", side="long",
            notional=50_000.0, beta=_symbol_beta("USO"),
            sector=_symbol_sector("USO"), factor_loadings=_symbol_factors("USO"),
        ),
    ]
    result = brain.validate_global_exposure(proposed)

    # If GLD/USO/BTC silently used the 1.0 equity fallback, net_beta would
    # be (100k*1.0 + 100k*1.15 + 50k*1.0 + 50k*1.0 + 50k*1.0) / 1,000,000
    # = 0.365. With the researched values it should be meaningfully lower.
    naive_fallback_net_beta = (
        100_000 * 1.00 + 100_000 * 1.15 + 50_000 * 1.0 + 50_000 * 1.0 + 50_000 * 1.0
    ) / 1_000_000.0
    assert result.net_beta < naive_fallback_net_beta
    assert result.net_beta > 0.0  # still net-long, just not equity-scale


def test_validate_global_exposure_hard_cap_fires_for_gld() -> None:
    brain = PortfolioBrain(equity=100_000.0)
    proposed = [
        ProposedPosition(
            strategy_id="gld_leg", symbol="GLD", side="long",
            notional=180_000.0,  # 1.8x gross on its own -> breaches gross cap
            beta=_symbol_beta("GLD"), sector=_symbol_sector("GLD"),
            factor_loadings=_symbol_factors("GLD"),
        )
    ]
    result = brain.validate_global_exposure(proposed)
    assert result.allowed is False
    assert "gross_exposure_cap" in result.breach_codes
    assert result.sizing_multipliers["gld_leg"] == 0.0


def test_validate_global_exposure_hard_cap_fires_for_uso() -> None:
    brain = PortfolioBrain(equity=100_000.0)
    proposed = [
        ProposedPosition(
            strategy_id="uso_leg", symbol="USO", side="long",
            notional=180_000.0,
            beta=_symbol_beta("USO"), sector=_symbol_sector("USO"),
            factor_loadings=_symbol_factors("USO"),
        )
    ]
    result = brain.validate_global_exposure(proposed)
    assert result.allowed is False
    assert "gross_exposure_cap" in result.breach_codes
    assert result.sizing_multipliers["uso_leg"] == 0.0


def test_resolve_opposing_correlated_signals() -> None:
    brain = PortfolioBrain()
    brain.allocate_risk_budgets(
        {
            QQQ_STRATEGY_ID: _metrics(QQQ_STRATEGY_ID, "QQQ", sharpe=0.8),
            SPY_STRATEGY_ID: _metrics(SPY_STRATEGY_ID, "SPY", sharpe=0.3),
        }
    )
    brain.resolve_portfolio_mode(
        {
            QQQ_STRATEGY_ID: _metrics(QQQ_STRATEGY_ID, "QQQ"),
            SPY_STRATEGY_ID: _metrics(SPY_STRATEGY_ID, "SPY"),
        },
        qqq_spy_correlation=0.9,
    )
    ts = datetime.now(timezone.utc)
    signals = {
        QQQ_STRATEGY_ID: Signal("QQQ", SignalAction.LONG, 500.0, ts, QQQ_STRATEGY_ID),
        SPY_STRATEGY_ID: Signal("SPY", SignalAction.SHORT, 600.0, ts, SPY_STRATEGY_ID),
    }
    resolution = brain.resolve_signal_conflicts(
        signals,
        leg_metrics={
            QQQ_STRATEGY_ID: _metrics(QQQ_STRATEGY_ID, "QQQ", sharpe=0.8),
            SPY_STRATEGY_ID: _metrics(SPY_STRATEGY_ID, "SPY", sharpe=0.3),
        },
        symbol_correlations={("QQQ", "SPY"): 0.9, ("SPY", "QQQ"): 0.9},
    )
    assert SPY_STRATEGY_ID in resolution.blocked_strategy_ids
    assert resolution.approved_signals[QQQ_STRATEGY_ID] is not None


def test_coordinator_logs_constraint_cycle(tmp_path) -> None:
    router = OrderRouter(RiskConfig())
    coordinator = PortfolioCoordinator(router, db_path=tmp_path / "vault.db")
    from src.config import StrategyConfig

    enabled = [
        StrategyConfig(
            strategy_id=QQQ_STRATEGY_ID,
            module="mean_reversion_qqq",
            symbol="QQQ",
            timeframe="15Min",
            poll_interval_seconds=900,
            params={},
            enabled=True,
            environment="paper",
        )
    ]
    from src.models import Account

    account = Account(equity=100_000.0, cash=100_000.0, buying_power=200_000.0)
    coordinator.begin_cycle(account=account)
    result = coordinator.coordinate_cycle(
        enabled_configs=enabled,
        account=account,
        positions=[],
    )
    assert result.cycle_id
    # Single enabled leg, no position history, no promotion records:
    #   vol = VOL_FLOOR = 0.05  →  inv_vol = 20.0
    #   dd_contrib = 0.0        →  dd_penalty = 1.0
    #   marginal_sharpe = 0.25 (default, no promotion_pnls)
    #   raw = 20.0 * 1.0 * 0.25 = 5.0  →  normalized single-leg budget = 1.0
    assert result.risk_budgets[QQQ_STRATEGY_ID] == pytest.approx(1.0)


def test_check_inventory_path_dependency_exposure_cap() -> None:
    brain = PortfolioBrain(
        equity=100_000.0,
        max_multiday_directional_exposure_fraction=0.40,
    )
    manifest = [
        ActivePositionManifest(
            strategy_id=QQQ_STRATEGY_ID,
            symbol="QQQ",
            side="long",
            notional=45_000.0,
            calendar_days_held=2,
            opened_session_date="2026-06-22",
        ),
        ActivePositionManifest(
            strategy_id=SPY_STRATEGY_ID,
            symbol="SPY",
            side="long",
            notional=10_000.0,
            calendar_days_held=3,
            opened_session_date="2026-06-21",
        ),
    ]
    verdict = brain.check_inventory_path_dependency(manifest, 100_000.0)
    assert verdict.directive == GLOBAL_ENTRY_LOCKOUT
    assert verdict.allowed is False
    assert "MULTIDAY_LONG_EXPOSURE_CAP" in verdict.breach_codes
    assert verdict.is_entry_locked("QQQ", "long")
    assert verdict.is_entry_locked("SPY", "long")
    assert verdict.multiday_net_exposure == pytest.approx(55_000.0)


def test_check_inventory_path_dependency_allows_exits_only_direction() -> None:
    brain = PortfolioBrain(equity=100_000.0)
    manifest = [
        ActivePositionManifest(
            strategy_id=QQQ_STRATEGY_ID,
            symbol="QQQ",
            side="short",
            notional=20_000.0,
            calendar_days_held=1,
            opened_session_date="2026-06-23",
        ),
    ]
    verdict = brain.check_inventory_path_dependency(manifest, 100_000.0)
    assert verdict.directive is None
    assert verdict.allowed is True
    assert not verdict.is_entry_locked("QQQ", "short")


def test_check_inventory_path_dependency_holding_duration_breach() -> None:
    brain = PortfolioBrain(
        equity=100_000.0,
        max_multiday_holding_calendar_days=3,
    )
    manifest = [
        ActivePositionManifest(
            strategy_id=QQQ_STRATEGY_ID,
            symbol="QQQ",
            side="short",
            notional=15_000.0,
            calendar_days_held=5,
            opened_session_date="2026-06-19",
        ),
    ]
    verdict = brain.check_inventory_path_dependency(manifest, 100_000.0)
    assert verdict.has_directive(GLOBAL_ENTRY_LOCKOUT)
    assert verdict.has_directive(FORCE_LIQUIDATION_EXECUTE)
    assert "MULTIDAY_HOLDING_DURATION" in verdict.breach_codes
    assert verdict.is_entry_locked("QQQ", "short")
    assert len(verdict.force_liquidation_targets) == 1
    target = verdict.force_liquidation_targets[0]
    assert target.strategy_id == QQQ_STRATEGY_ID
    assert target.transition_reason == FORCED_DURATION_EXHAUSTION


def test_coordinator_applies_inventory_lockout_before_conflict_resolution(
    tmp_path,
) -> None:
    import sqlite3

    router = OrderRouter(RiskConfig())
    db_path = tmp_path / "vault.db"
    coordinator = PortfolioCoordinator(router, db_path=db_path)
    from src.config import StrategyConfig
    from src.core.rolling_window import RollingWindow
    from src.models import Account, Bar, Position

    enabled = [
        StrategyConfig(
            strategy_id=QQQ_STRATEGY_ID,
            module="mean_reversion_qqq",
            symbol="QQQ",
            timeframe="15Min",
            poll_interval_seconds=900,
            params={},
            enabled=True,
            environment="paper",
        )
    ]
    account = Account(equity=100_000.0, cash=100_000.0, buying_power=200_000.0)
    positions = [
        Position(
            symbol="QQQ",
            qty=500.0,
            side="long",
            avg_entry_price=100.0,
            unrealized_pl=0.0,
        )
    ]
    today = datetime.now(timezone.utc).date()
    opened_date = (today - timedelta(days=2)).isoformat()
    last_seen_date = (today - timedelta(days=1)).isoformat()
    ensure_portfolio_constraint_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO portfolio_inventory_registry (
                strategy_id, symbol, side, opened_session_date,
                last_seen_at, net_directional_exposure
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                QQQ_STRATEGY_ID,
                "QQQ",
                "long",
                opened_date,
                last_seen_date,
                50_000.0,
            ),
        )

    coordinator.begin_cycle(account=account)
    window = RollingWindow(maxlen=120)
    ts = datetime.now(timezone.utc)
    for i in range(80):
        window.append(
            Bar(
                timestamp=ts,
                open=100.0 + i * 0.01,
                high=101.0,
                low=99.0,
                close=100.0 + i * 0.01,
                volume=1_000_000.0,
                symbol="QQQ",
            )
        )
    coordinator.register_leg_evaluation(
        LegCyclePayload(
            strategy_id=QQQ_STRATEGY_ID,
            symbol="QQQ",
            signal=Signal(
                "QQQ",
                SignalAction.LONG,
                500.0,
                ts,
                QQQ_STRATEGY_ID,
            ),
            routing_params={"max_position_pct": 0.95},
            base_risk_fraction=0.5,
            window=window,
            enabled=True,
            bars_in_trade=12,
            position_side="long",
        )
    )
    result = coordinator.coordinate_cycle(
        enabled_configs=enabled,
        account=account,
        positions=positions,
    )
    plan = result.plans[QQQ_STRATEGY_ID]
    assert plan.blocked is True
    assert plan.signal is None
    assert GLOBAL_ENTRY_LOCKOUT in plan.block_reason


def _register_long_leg(coordinator, account):
    """Minimal helper: register one enabled QQQ leg with a fresh LONG entry signal."""
    from src.config import StrategyConfig
    from src.core.rolling_window import RollingWindow
    from src.models import Bar

    enabled = [
        StrategyConfig(
            strategy_id=QQQ_STRATEGY_ID, module="mean_reversion_qqq", symbol="QQQ",
            timeframe="15Min", poll_interval_seconds=900, params={},
            enabled=True, environment="paper",
        )
    ]
    coordinator.begin_cycle(account=account)
    window = RollingWindow(maxlen=120)
    ts = datetime.now(timezone.utc)
    for i in range(80):
        window.append(Bar(timestamp=ts, open=100.0 + i * 0.01, high=101.0, low=99.0,
                          close=100.0 + i * 0.01, volume=1_000_000.0, symbol="QQQ"))
    coordinator.register_leg_evaluation(
        LegCyclePayload(
            strategy_id=QQQ_STRATEGY_ID, symbol="QQQ",
            signal=Signal("QQQ", SignalAction.LONG, 100.0, ts, QQQ_STRATEGY_ID),
            routing_params={"max_position_pct": 0.95}, base_risk_fraction=0.5,
            window=window, enabled=True, bars_in_trade=0, position_side=None,
        )
    )
    return enabled


def _allow_all_exposure(coordinator, monkeypatch):
    """Stub global-exposure validation to ALLOWED so a single-leg fixture (otherwise 100% sector-
    concentrated) isolates the macro-block path as the only differentiator."""
    from src.engine.portfolio_brain import ExposureValidation

    def _ok(proposed, *, equity):
        return ExposureValidation(
            allowed=True,
            sizing_multipliers={
                str(getattr(p, "strategy_id", p)): 1.0 for p in proposed
            },
            breach_codes=(), net_beta=0.0, gross_exposure_ratio=0.1,
            sector_concentration=0.0, factor_crowding=0.0,
        )

    monkeypatch.setattr(coordinator.brain, "validate_global_exposure", _ok)


def test_macro_block_new_entries_blocks_regardless_of_gross_cap(tmp_path, monkeypatch) -> None:
    """Y5 — the risk mode's block_new_entries flag is ENFORCED, not decorative. With the gross cap at
    1.0 (WELL ABOVE the 0.65 breach line, so the exposure path would NOT block) and exposure otherwise
    ALLOWED, macro_block_new_entries alone must still block a new LONG entry. This closes the landmine
    where the block worked only because RISK_OFF_GROSS_EXPOSURE_CAP happened to sit below 0.65 — raise
    the cap and the flag would have lied."""
    from src.models import Account

    router = OrderRouter(RiskConfig())
    coordinator = PortfolioCoordinator(router, db_path=tmp_path / "vault.db")
    account = Account(equity=100_000.0, cash=100_000.0, buying_power=200_000.0)
    enabled = _register_long_leg(coordinator, account)
    _allow_all_exposure(coordinator, monkeypatch)
    result = coordinator.coordinate_cycle(
        enabled_configs=enabled, account=account, positions=[],
        gross_exposure_cap_multiplier=1.0,        # above 0.65 -> exposure path does NOT block
        macro_block_new_entries=True,             # the flag alone must block
    )
    plan = result.plans[QQQ_STRATEGY_ID]
    assert plan.blocked is True
    assert plan.signal is None
    assert "macro_risk_off_block" in plan.block_reason


def test_no_macro_block_allows_entry_at_full_cap(tmp_path, monkeypatch) -> None:
    """Companion: flag False + cap 1.0 + exposure allowed -> the same LONG entry is NOT blocked, proving
    the block is the flag itself and it is off by default."""
    from src.models import Account

    router = OrderRouter(RiskConfig())
    coordinator = PortfolioCoordinator(router, db_path=tmp_path / "vault.db")
    account = Account(equity=100_000.0, cash=100_000.0, buying_power=200_000.0)
    enabled = _register_long_leg(coordinator, account)
    _allow_all_exposure(coordinator, monkeypatch)
    result = coordinator.coordinate_cycle(
        enabled_configs=enabled, account=account, positions=[],
        gross_exposure_cap_multiplier=1.0, macro_block_new_entries=False,
    )
    plan = result.plans[QQQ_STRATEGY_ID]
    assert plan.blocked is False
    assert "macro_risk_off_block" not in (plan.block_reason or "")


def test_coordinator_force_liquidation_on_duration_exhaustion(tmp_path) -> None:
    import sqlite3

    router = OrderRouter(RiskConfig())
    db_path = tmp_path / "vault.db"
    coordinator = PortfolioCoordinator(router, db_path=db_path)
    from src.config import StrategyConfig
    from src.core.rolling_window import RollingWindow
    from src.models import Account, Bar, Position

    enabled = [
        StrategyConfig(
            strategy_id=QQQ_STRATEGY_ID,
            module="mean_reversion_qqq",
            symbol="QQQ",
            timeframe="15Min",
            poll_interval_seconds=900,
            params={},
            enabled=True,
            environment="paper",
        )
    ]
    account = Account(equity=100_000.0, cash=100_000.0, buying_power=200_000.0)
    positions = [
        Position(
            symbol="QQQ",
            qty=100.0,
            side="long",
            avg_entry_price=500.0,
            unrealized_pl=0.0,
        )
    ]
    ensure_portfolio_constraint_schema(db_path)
    coordinator.brain.max_multiday_holding_calendar_days = 3
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO portfolio_inventory_registry (
                strategy_id, symbol, side, opened_session_date,
                last_seen_at, net_directional_exposure
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                QQQ_STRATEGY_ID,
                "QQQ",
                "long",
                "2026-06-15",
                "2026-06-24",
                50_000.0,
            ),
        )

    coordinator.begin_cycle(account=account)
    window = RollingWindow(maxlen=120)
    ts = datetime.now(timezone.utc)
    for i in range(80):
        window.append(
            Bar(
                timestamp=ts,
                open=500.0,
                high=501.0,
                low=499.0,
                close=500.0,
                volume=1_000_000.0,
                symbol="QQQ",
            )
        )
    coordinator.register_leg_evaluation(
        LegCyclePayload(
            strategy_id=QQQ_STRATEGY_ID,
            symbol="QQQ",
            signal=None,
            routing_params={"max_position_pct": 0.95, "symbol": "QQQ"},
            base_risk_fraction=0.5,
            window=window,
            enabled=True,
            bars_in_trade=40,
            position_side="long",
        )
    )
    result = coordinator.coordinate_cycle(
        enabled_configs=enabled,
        account=account,
        positions=positions,
    )
    plan = result.plans[QQQ_STRATEGY_ID]
    assert plan.force_liquidation is True
    assert plan.cancel_pending_orders is True
    assert plan.blocked is False
    assert plan.signal is not None
    assert plan.signal.action == SignalAction.EXIT
    assert plan.routing_params["force_terminal_liquidation"] is True
    assert plan.force_liquidation_reason == FORCED_DURATION_EXHAUSTION

    orders, _ = coordinator.route_coordinated_plan(
        plan,
        account,
        positions,
        __import__("src.models", fromlist=["PortfolioState"]).PortfolioState(
            peak_equity=account.equity
        ),
    )
    assert len(orders) == 1
    assert orders[0].order_type == "market"
    assert orders[0].qty == 100.0

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT action_taken, metadata_json
            FROM portfolio_constraint_ledger
            WHERE constraint_type = 'forced_duration_liquidation'
            ORDER BY log_id DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    assert row[0] == FORCE_LIQUIDATION_EXECUTE
    assert FORCED_DURATION_EXHAUSTION in row[1]
