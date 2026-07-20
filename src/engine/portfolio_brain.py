"""
Centralized portfolio risk brain — cross-leg budget allocation, exposure caps,
signal conflict resolution, and structural SPY / single-leg mode governance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

import numpy as np

from src.models import Position, Signal, SignalAction

GLOBAL_ENTRY_LOCKOUT = "GLOBAL_ENTRY_LOCKOUT"
FORCE_LIQUIDATION_EXECUTE = "FORCE_LIQUIDATION_EXECUTE"
FORCED_DURATION_EXHAUSTION = "FORCED_DURATION_EXHAUSTION"
DEFAULT_MAX_MULTIDAY_DIRECTIONAL_EXPOSURE_FRACTION = 0.40
DEFAULT_MAX_MULTIDAY_HOLDING_CALENDAR_DAYS = 5
MIN_MULTIDAY_CALENDAR_DAYS = 1

QQQ_STRATEGY_ID = "mean_reversion_qqq"
SPY_STRATEGY_ID = "mean_reversion_spy"

PORTFOLIO_MODE_DUAL_LEG = "DUAL_LEG"
PORTFOLIO_MODE_SINGLE_LEG_QQQ = "SINGLE_LEG_QQQ"
PORTFOLIO_MODE_SINGLE_LEG_SPY = "SINGLE_LEG_SPY"
PORTFOLIO_MODE_SPY_DISABLED = "SPY_DISABLED"

MAX_NET_BETA_EXPOSURE = 1.25
MAX_GROSS_EXPOSURE_RATIO = 1.50
MAX_SECTOR_CONCENTRATION = 0.85
MAX_FACTOR_CROWDING = 0.90
# RETIRED (FIX-B, registry STANDING-QQQSPYLADDER): CORRELATION_SHUTOFF=0.92 and
# SINGLE_LEG_CORRELATION_THRESHOLD=0.88 -- the QQQ/SPY PRICE-correlation ladder -- are removed.
# They gated on price correlation (permanently ~0.92, no room to spike; fired 44-63% of the time,
# at/below baseline) rather than STRATEGY-RETURN correlation (measured 0.589, genuinely diversifying).
# See resolve_portfolio_mode for the five-case live-home mapping. SPY_DISABLE_HEALTH_FLOOR (a HEALTH
# gate, CASE 4) is NOT the ladder and stays.
SPY_DISABLE_HEALTH_FLOOR = 0.45
DOMINANT_LEG_BUDGET_SHARE = 0.95
# ONE_RISK_UNIT_CORRELATION -- the field-standard "two symbols correlated enough to be ONE risk
# unit" line (BTC/ETH ~0.85+, mega-cap tech ~0.75+). SINGLE SOURCE for the two 0.85 gates, which
# apply the SAME fact to OPPOSITE situations and MUST NOT silently diverge (a future tuner sees
# they are linked):
#   - governor MAX_SAFE_CORRELATION (portfolio_risk_governor.py): SAME-direction correlation ->
#     concentration -> clamp BOTH sizes (x CORRELATION_CLAMP_MULTIPLIER).
#   - OPPOSING_SIGNAL_CORRELATION_FLOOR (below): OPPOSING-direction correlation on the same
#     underlying -> wash / cost-bleed -> block the weaker leg.
ONE_RISK_UNIT_CORRELATION = 0.85
OPPOSING_SIGNAL_CORRELATION_FLOOR = ONE_RISK_UNIT_CORRELATION
MIN_MARGINAL_SHARPE = 0.05
VOL_FLOOR = 0.05

SECTOR_BY_SYMBOL: dict[str, str] = {
    "QQQ": "US_LARGE_CAP_GROWTH",
    "SPY": "US_LARGE_CAP_BLEND",
    "GLD": "PRECIOUS_METALS",
    "USO": "ENERGY_COMMODITIES",
    "BTC/USD": "CRYPTO",
}

# Third tuple element is inferred from the existing SPY/QQQ pattern, not
# documented anywhere in this codebase: (market beta, value/growth tilt
# [negative=growth-tilted], momentum-like tilt) -- QQQ's negative 2nd /
# positive 3rd match its growth+momentum profile, SPY's near-zero 2nd/3rd
# match a neutral blend index. GLD/USO/BTC are not equities and have no
# coherent equity-style value-vs-growth or momentum factor exposure, so
# their 2nd/3rd components are 0.0 rather than a fabricated number for a
# model dimension that doesn't conceptually apply to them -- only the
# market-beta-like first component is populated, mirroring BETA_BY_SYMBOL.
FACTOR_LOADINGS_BY_SYMBOL: dict[str, tuple[float, float, float]] = {
    "QQQ": (1.15, -0.20, 0.30),
    "SPY": (1.00, 0.00, 0.05),
    "GLD": (0.05, 0.0, 0.0),
    "USO": (0.30, 0.0, 0.0),
    "BTC/USD": (2.00, 0.0, 0.0),
}

# GLD: gold's beta to broad US equities has historically sat close to zero
# across most multi-year sample periods -- sometimes mildly negative during
# risk-off/flight-to-safety episodes, sometimes mildly positive otherwise.
# 0.05 reflects "near-zero, slightly positive on average" rather than
# asserting a negative number that isn't reliably true across regimes.
#
# USO: oil has a low but meaningfully positive and more regime-dependent
# beta to equities than gold -- it decouples during idiosyncratic
# supply/demand shocks but has shown real co-movement with equities during
# broad macro risk-on/risk-off periods (e.g. the 2020 COVID crash), since
# oil demand is tied to economic activity in a way gold demand is not. 0.30
# is a conservative point estimate above gold's near-zero and well below an
# equity-like 1.0; USO specifically also carries roll-cost/contango drag
# not present in spot WTI, which this estimate does not attempt to isolate.
#
# BTC/USD: explicitly NOT a settled figure -- flag this loudly rather than
# assert false precision. Historical BTC-vs-equity correlation has shifted
# by regime: near-zero in 2013-2017 ("uncorrelated digital gold" era),
# materially higher (documented periods of 0.5-0.7+ correlation to
# SPY/QQQ) since ~2020-2022 as institutional/ETF flows made BTC trade more
# like a high-beta risk asset, especially during macro-driven selloffs.
# Combined with BTC's much higher volatility than equities (beta =
# correlation x vol_ratio, and BTC's annualized vol is commonly several
# times SPY's), even a moderate correlation implies a HIGH numeric beta.
# 2.00 is a rough point estimate in a plausible 1.5-2.5 range, not a
# confident number -- treat it as needing empirical validation against
# this project's own BTC/SPY/QQQ return series before being trusted for
# any real risk decision, the same way GLD/USO's estimates should also
# eventually be validated but are more defensible as first-pass numbers.
BETA_BY_SYMBOL: dict[str, float] = {
    "QQQ": 1.15,
    "SPY": 1.00,
    "GLD": 0.05,
    "USO": 0.30,
    "BTC/USD": 2.00,
}

"""
Structural SPY disable vs single-leg mode
-----------------------------------------

The QQQ/SPY PRICE-correlation ladder (old CORRELATION_SHUTOFF/SINGLE_LEG thresholds) was RETIRED
in FIX-B; QQQ/SPY strategy-return correlation is now governed like any leg pair (governor 0.85
clamp / 0.75 aggregate shutter / PAD convergence-watch). The remaining disable/single-leg triggers:

SPY leg is **entirely disabled** (no entries; exits still pass) when ANY holds:

1. Strategy config ``enabled: false`` for ``mean_reversion_spy``.
2. Resolved portfolio mode is ``SPY_DISABLED``.
3. SPY unified health score < ``SPY_DISABLE_HEALTH_FLOOR`` (0.45).
4. Proposed portfolio net beta would exceed ``MAX_NET_BETA_EXPOSURE`` if SPY
   entry were sized at its vol-parity budget.
5. Capital-entry prioritization names a single winner that is not SPY.

Portfolio is in **intentional single-leg mode** when ANY holds:

1. Exactly one strategy leg is enabled in config.
2. Vol-parity allocation assigns >= ``DOMINANT_LEG_BUDGET_SHARE`` (95%) to one leg.
3. Capital-entry prioritization allows only one leg this cycle.
4. Explicit coordinator override ``single_leg_symbol`` is set (QQQ or SPY).
"""


@dataclass(frozen=True)
class ActivePositionManifest:
    """Cross-session position snapshot for path-dependent inventory governance."""

    strategy_id: str
    symbol: str
    side: str
    notional: float
    calendar_days_held: int
    bars_in_trade: int = 0
    opened_session_date: str = ""


@dataclass(frozen=True)
class ForceLiquidationTarget:
    """Stale inventory leg requiring immediate terminal flatten."""

    strategy_id: str
    symbol: str
    side: str
    notional: float
    calendar_days_held: int
    opened_session_date: str
    transition_reason: str = FORCED_DURATION_EXHAUSTION


@dataclass(frozen=True)
class InventoryPathDependencyVerdict:
    """Result of multi-day inventory concentration and holding-time checks."""

    directive: str | None
    allowed: bool
    net_long_exposure: float
    net_short_exposure: float
    multiday_net_exposure: float
    multiday_exposure_ratio: float
    cap_notional: float
    blocked_entry_by_symbol: dict[str, frozenset[str]]
    breach_codes: tuple[str, ...]
    reasons: dict[str, str]
    directives: tuple[str, ...] = ()
    force_liquidation_targets: tuple[ForceLiquidationTarget, ...] = ()

    def is_entry_locked(self, symbol: str, direction: str) -> bool:
        blocked = self.blocked_entry_by_symbol.get(symbol.upper(), frozenset())
        return direction.lower() in blocked

    def has_directive(self, directive: str) -> bool:
        if self.directive == directive:
            return True
        return directive in self.directives


@dataclass(frozen=True)
class LegMetrics:
    strategy_id: str
    symbol: str
    realized_vol: float
    drawdown_contribution: float
    marginal_sharpe: float
    notional_exposure: float = 0.0
    health_score: float = 1.0
    champion_score: float = 0.0
    enabled: bool = True


@dataclass(frozen=True)
class ProposedPosition:
    strategy_id: str
    symbol: str
    side: str
    notional: float
    beta: float
    sector: str
    factor_loadings: tuple[float, float, float]


@dataclass(frozen=True)
class ExposureValidation:
    allowed: bool
    sizing_multipliers: dict[str, float]
    breach_codes: tuple[str, ...]
    net_beta: float
    gross_exposure_ratio: float
    sector_concentration: float
    factor_crowding: float


@dataclass(frozen=True)
class SignalConflictResolution:
    approved_signals: dict[str, Signal | None]
    blocked_strategy_ids: frozenset[str]
    scale_multipliers: dict[str, float]
    reasons: dict[str, str]


@dataclass(frozen=True)
class PortfolioModeDecision:
    mode: str
    spy_leg_enabled: bool
    single_leg_mode: bool
    dominant_strategy_id: str | None
    reason: str


@dataclass
class PortfolioBrainState:
    risk_budgets: dict[str, float] = field(default_factory=dict)
    mode: PortfolioModeDecision | None = None
    last_exposure: ExposureValidation | None = None
    last_conflict_resolution: SignalConflictResolution | None = None
    last_inventory_verdict: InventoryPathDependencyVerdict | None = None


def _symbol_beta(symbol: str) -> float:
    return BETA_BY_SYMBOL.get(symbol.upper(), 1.0)


def _symbol_sector(symbol: str) -> str:
    return SECTOR_BY_SYMBOL.get(symbol.upper(), "US_EQUITY_OTHER")


def _symbol_factors(symbol: str) -> tuple[float, float, float]:
    return FACTOR_LOADINGS_BY_SYMBOL.get(symbol.upper(), (1.0, 0.0, 0.0))


def _is_entry_action(action: SignalAction) -> bool:
    return action in (SignalAction.LONG, SignalAction.SHORT)


def _signal_direction(action: SignalAction) -> int:
    if action == SignalAction.LONG:
        return 1
    if action == SignalAction.SHORT:
        return -1
    return 0


def _normalize_weights(raw: dict[str, float]) -> dict[str, float]:
    positive = {k: max(v, 0.0) for k, v in raw.items()}
    total = sum(positive.values())
    if total <= 0.0:
        n = len(positive) or 1
        return {k: 1.0 / n for k in positive}
    return {k: v / total for k, v in positive.items()}


def compute_return_correlation(
    closes_a: np.ndarray,
    closes_b: np.ndarray,
    lookback: int = 60,
) -> float:
    if len(closes_a) < lookback + 1 or len(closes_b) < lookback + 1:
        return 0.0
    ra = np.diff(np.log(closes_a[-lookback - 1 :]))
    rb = np.diff(np.log(closes_b[-lookback - 1 :]))
    n = min(len(ra), len(rb))
    if n < 5:
        return 0.0
    ra = ra[-n:]
    rb = rb[-n:]
    std_a = float(np.std(ra))
    std_b = float(np.std(rb))
    if std_a < 1e-12 or std_b < 1e-12:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


class PortfolioBrain:
    """Global portfolio coordinator for multi-leg mean-reversion books."""

    def __init__(
        self,
        *,
        equity: float = 100_000.0,
        single_leg_symbol: str | None = None,
        static_equal_split: bool = False,
        max_multiday_directional_exposure_fraction: float = (
            DEFAULT_MAX_MULTIDAY_DIRECTIONAL_EXPOSURE_FRACTION
        ),
        max_multiday_holding_calendar_days: int = DEFAULT_MAX_MULTIDAY_HOLDING_CALENDAR_DAYS,
    ) -> None:
        self.equity = max(equity, 1.0)
        self.single_leg_symbol = single_leg_symbol.upper() if single_leg_symbol else None
        # COMMISSIONING PIN (default OFF): when True, allocate_risk_budgets returns an EQUAL split
        # across enabled legs instead of the vol-parity formula, making soak sizing deterministic.
        # Rationale (07-20 oscillation diagnosis): post-close, a dormant leg's closes window drops
        # <3 bars, so annualized_vol_from_closes returns VOL_FLOOR and the inverse-vol allocator
        # reads "no data" as "lowest risk", handing that leg the max weight — a data-availability
        # artifact that flips the split (e.g. QQQ 0.845<->0.227) and makes the SAME signal's size
        # ~4x non-deterministic. Off by default so this changes nothing until the operator enables it.
        self.static_equal_split = bool(static_equal_split)
        self.max_multiday_directional_exposure_fraction = (
            max_multiday_directional_exposure_fraction
        )
        self.max_multiday_holding_calendar_days = max_multiday_holding_calendar_days
        self.state = PortfolioBrainState()

    def allocate_risk_budgets(
        self,
        leg_metrics: Mapping[str, LegMetrics],
    ) -> dict[str, float]:
        """
        Vol-parity base weights adjusted for drawdown contribution and marginal Sharpe.

        w_i ∝ (1 / max(σ_i, VOL_FLOOR))
              × (1 - min(dd_contrib_i, 0.95))
              × max(marginal_sharpe_i, MIN_MARGINAL_SHARPE)
        """
        if not leg_metrics:
            return {}

        if self.static_equal_split:
            # Deterministic commissioning split: equal weight across ENABLED legs, disabled -> 0.0.
            # Bypasses the vol-parity inputs entirely, so the dormant-leg VOL_FLOOR flicker cannot
            # move the allocation. Uses the same _normalize_weights path for a single source of truth.
            raw_equal = {sid: (1.0 if m.enabled else 0.0) for sid, m in leg_metrics.items()}
            budgets = _normalize_weights(raw_equal)
            self.state.risk_budgets = dict(budgets)
            return dict(budgets)

        raw: dict[str, float] = {}
        for strategy_id, metrics in leg_metrics.items():
            if not metrics.enabled:
                raw[strategy_id] = 0.0
                continue
            inv_vol = 1.0 / max(metrics.realized_vol, VOL_FLOOR)
            dd_penalty = 1.0 - min(max(metrics.drawdown_contribution, 0.0), 0.95)
            sharpe_boost = max(metrics.marginal_sharpe, MIN_MARGINAL_SHARPE)
            raw[strategy_id] = inv_vol * dd_penalty * sharpe_boost

        budgets = _normalize_weights(raw)
        self.state.risk_budgets = dict(budgets)
        return dict(budgets)

    def resolve_portfolio_mode(
        self,
        leg_metrics: Mapping[str, LegMetrics],
        *,
        qqq_spy_correlation: float,
        capital_winner_strategy_id: str | None = None,
        enabled_strategy_ids: frozenset[str] | None = None,
    ) -> PortfolioModeDecision:
        enabled = enabled_strategy_ids or frozenset(leg_metrics)
        spy_metrics = leg_metrics.get(SPY_STRATEGY_ID)
        qqq_metrics = leg_metrics.get(QQQ_STRATEGY_ID)
        spy_enabled_config = spy_metrics.enabled if spy_metrics is not None else False
        only_one_enabled = len(enabled) == 1

        if self.single_leg_symbol == "QQQ":
            decision = PortfolioModeDecision(
                mode=PORTFOLIO_MODE_SINGLE_LEG_QQQ,
                spy_leg_enabled=False,
                single_leg_mode=True,
                dominant_strategy_id=QQQ_STRATEGY_ID,
                reason="explicit_single_leg_symbol_qqq",
            )
            self.state.mode = decision
            return decision
        if self.single_leg_symbol == "SPY":
            decision = PortfolioModeDecision(
                mode=PORTFOLIO_MODE_SINGLE_LEG_SPY,
                spy_leg_enabled=True,
                single_leg_mode=True,
                dominant_strategy_id=SPY_STRATEGY_ID,
                reason="explicit_single_leg_symbol_spy",
            )
            self.state.mode = decision
            return decision

        if only_one_enabled:
            dominant = next(iter(enabled))
            mode = (
                PORTFOLIO_MODE_SINGLE_LEG_QQQ
                if dominant == QQQ_STRATEGY_ID
                else PORTFOLIO_MODE_SINGLE_LEG_SPY
                if dominant == SPY_STRATEGY_ID
                else PORTFOLIO_MODE_DUAL_LEG
            )
            decision = PortfolioModeDecision(
                mode=mode,
                spy_leg_enabled=SPY_STRATEGY_ID in enabled,
                single_leg_mode=True,
                dominant_strategy_id=dominant,
                reason="single_enabled_leg_config",
            )
            self.state.mode = decision
            return decision

        if not spy_enabled_config:
            decision = PortfolioModeDecision(
                mode=PORTFOLIO_MODE_SPY_DISABLED,
                spy_leg_enabled=False,
                single_leg_mode=True,
                dominant_strategy_id=QQQ_STRATEGY_ID,
                reason="spy_config_disabled",
            )
            self.state.mode = decision
            return decision

        if spy_metrics is not None and spy_metrics.health_score < SPY_DISABLE_HEALTH_FLOOR:
            decision = PortfolioModeDecision(
                mode=PORTFOLIO_MODE_SPY_DISABLED,
                spy_leg_enabled=False,
                single_leg_mode=True,
                dominant_strategy_id=QQQ_STRATEGY_ID,
                reason="spy_health_below_floor",
            )
            self.state.mode = decision
            return decision

        if capital_winner_strategy_id and capital_winner_strategy_id != SPY_STRATEGY_ID:
            if SPY_STRATEGY_ID in enabled and QQQ_STRATEGY_ID in enabled:
                decision = PortfolioModeDecision(
                    mode=PORTFOLIO_MODE_SPY_DISABLED,
                    spy_leg_enabled=False,
                    single_leg_mode=True,
                    dominant_strategy_id=capital_winner_strategy_id,
                    reason="capital_prioritization_non_spy",
                )
                self.state.mode = decision
                return decision

        dominant_strategy_id: str | None = None
        single_leg_mode = False
        mode = PORTFOLIO_MODE_DUAL_LEG
        reason = "dual_leg_default"

        # FIX-B: the 0.88/0.92 QQQ/SPY PRICE-correlation ladder is RETIRED. It gated leg-enablement
        # on PRICE correlation (permanently ~0.92, no room to spike -- fired 44-63% of the time,
        # below its own baseline) when the decision should be about STRATEGY-RETURN correlation
        # (measured 0.589 for these two mean-reversion legs -- genuinely diversifying). Every job it
        # did has a live home: CASE1 moderate return-corr -> governor 0.85 clamp (stricter + gentler);
        # CASE2 whole-book converging -> 0.75 aggregate shutter; CASE3 near-perfect redundancy ->
        # PAD convergence-watch demote-to-cash (governor clamp is the adequate interim); CASE4 SPY
        # broken -> SPY_DISABLE_HEALTH_FLOOR (above, preserved); CASE5 deliberate single-leg -> config
        # override (above, preserved). qqq_spy_correlation is retained for telemetry only, no longer
        # a gate. QQQ/SPY are now two ordinary legs on STRATEGY-return correlation like every pair.

        budgets = self.state.risk_budgets
        if budgets:
            top_id = max(budgets, key=budgets.get)
            top_share = budgets.get(top_id, 0.0)
            if top_share >= DOMINANT_LEG_BUDGET_SHARE:
                single_leg_mode = True
                dominant_strategy_id = top_id
                if top_id == QQQ_STRATEGY_ID:
                    mode = PORTFOLIO_MODE_SINGLE_LEG_QQQ
                elif top_id == SPY_STRATEGY_ID:
                    mode = PORTFOLIO_MODE_SINGLE_LEG_SPY
                reason = "vol_parity_dominant_budget"

        spy_leg_enabled = mode != PORTFOLIO_MODE_SPY_DISABLED and spy_enabled_config
        decision = PortfolioModeDecision(
            mode=mode,
            spy_leg_enabled=spy_leg_enabled,
            single_leg_mode=single_leg_mode,
            dominant_strategy_id=dominant_strategy_id,
            reason=reason,
        )
        self.state.mode = decision
        return decision

    def validate_global_exposure(
        self,
        proposed_positions: list[ProposedPosition],
        *,
        equity: float | None = None,
    ) -> ExposureValidation:
        """
        Hard-cap gate on net beta, gross exposure, sector concentration, and
        factor crowding. Returns ``allowed=False`` when a new entry would breach
        an absolute limit; exits are always permitted upstream.
        """
        equity_base = max(equity or self.equity, 1.0)
        net_beta_num = 0.0
        gross = 0.0
        sector_notionals: dict[str, float] = {}
        factor_vectors: list[np.ndarray] = []
        notionals: list[float] = []

        for pos in proposed_positions:
            if pos.notional <= 0.0 or pos.side == "flat":
                continue
            signed = pos.notional if pos.side == "long" else -pos.notional
            gross += abs(pos.notional)
            net_beta_num += signed * pos.beta
            sector_notionals[pos.sector] = (
                sector_notionals.get(pos.sector, 0.0) + abs(pos.notional)
            )
            factor_vectors.append(np.asarray(pos.factor_loadings, dtype=np.float64))
            notionals.append(abs(pos.notional))

        net_beta = net_beta_num / equity_base
        gross_ratio = gross / equity_base
        sector_total = sum(sector_notionals.values()) or 1.0
        sector_concentration = max(sector_notionals.values(), default=0.0) / sector_total

        factor_crowding = 0.0
        if len(factor_vectors) >= 2:
            weights = np.asarray(notionals, dtype=np.float64)
            weight_sum = float(weights.sum()) or 1.0
            weights = weights / weight_sum
            stacked = np.vstack(factor_vectors)
            weighted = stacked * weights[:, np.newaxis]
            centroid = weighted.sum(axis=0)
            dispersions = np.linalg.norm(stacked - centroid, axis=1)
            factor_crowding = float(1.0 - np.average(dispersions, weights=weights))
            factor_crowding = max(0.0, min(1.0, factor_crowding))

        breach_codes: list[str] = []
        if abs(net_beta) > MAX_NET_BETA_EXPOSURE:
            breach_codes.append("net_beta_cap")
        if gross_ratio > MAX_GROSS_EXPOSURE_RATIO:
            breach_codes.append("gross_exposure_cap")
        if sector_concentration > MAX_SECTOR_CONCENTRATION:
            breach_codes.append("sector_concentration_cap")
        if factor_crowding > MAX_FACTOR_CROWDING:
            breach_codes.append("factor_crowding_cap")

        multipliers = {pos.strategy_id: 1.0 for pos in proposed_positions}
        allowed = len(breach_codes) == 0

        if not allowed:
            severity = max(
                abs(net_beta) / MAX_NET_BETA_EXPOSURE,
                gross_ratio / MAX_GROSS_EXPOSURE_RATIO,
                sector_concentration / MAX_SECTOR_CONCENTRATION,
                factor_crowding / MAX_FACTOR_CROWDING if factor_crowding > 0 else 0.0,
            )
            scale = max(0.0, min(1.0, 1.0 / severity))
            for strategy_id in multipliers:
                multipliers[strategy_id] = scale
            if severity > 1.0:
                for strategy_id in multipliers:
                    multipliers[strategy_id] = 0.0

        result = ExposureValidation(
            allowed=allowed,
            sizing_multipliers=multipliers,
            breach_codes=tuple(breach_codes),
            net_beta=net_beta,
            gross_exposure_ratio=gross_ratio,
            sector_concentration=sector_concentration,
            factor_crowding=factor_crowding,
        )
        self.state.last_exposure = result
        return result

    def check_inventory_path_dependency(
        self,
        active_positions_manifest: Sequence[ActivePositionManifest],
        current_equity: float,
    ) -> InventoryPathDependencyVerdict:
        """Aggregate multi-day directional exposure and enforce cross-session caps."""
        equity = max(float(current_equity), 1.0)
        cap_notional = equity * self.max_multiday_directional_exposure_fraction
        multiday_positions = [
            row
            for row in active_positions_manifest
            if row.side in {"long", "short"}
            and row.notional > 0.0
            and int(row.calendar_days_held) >= MIN_MULTIDAY_CALENDAR_DAYS
        ]

        net_long = 0.0
        net_short = 0.0
        long_by_symbol: dict[str, float] = {}
        short_by_symbol: dict[str, float] = {}
        holding_breach_by_symbol: dict[str, str] = {}
        force_liquidation_targets: list[ForceLiquidationTarget] = []

        for row in multiday_positions:
            symbol = row.symbol.upper()
            notional = abs(float(row.notional))
            if row.side == "long":
                net_long += notional
                long_by_symbol[symbol] = long_by_symbol.get(symbol, 0.0) + notional
            elif row.side == "short":
                net_short += notional
                short_by_symbol[symbol] = short_by_symbol.get(symbol, 0.0) + notional

            if int(row.calendar_days_held) >= self.max_multiday_holding_calendar_days:
                holding_breach_by_symbol[symbol] = (
                    f"{symbol} held {row.calendar_days_held} calendar days "
                    f"(max {self.max_multiday_holding_calendar_days})"
                )
                force_liquidation_targets.append(
                    ForceLiquidationTarget(
                        strategy_id=row.strategy_id,
                        symbol=symbol,
                        side=row.side,
                        notional=notional,
                        calendar_days_held=int(row.calendar_days_held),
                        opened_session_date=row.opened_session_date,
                    )
                )

        multiday_net_exposure = max(net_long, net_short)
        multiday_exposure_ratio = multiday_net_exposure / equity
        blocked_entry_by_symbol: dict[str, set[str]] = {}
        breach_codes: list[str] = []
        reasons: dict[str, str] = {}

        if net_long > cap_notional + 1e-9:
            breach_codes.append("MULTIDAY_LONG_EXPOSURE_CAP")
            reasons["MULTIDAY_LONG_EXPOSURE_CAP"] = (
                f"net long multi-day exposure ${net_long:,.0f} exceeds cap "
                f"${cap_notional:,.0f} ({self.max_multiday_directional_exposure_fraction:.0%} equity)"
            )
            for symbol, notional in long_by_symbol.items():
                if notional <= 0.0:
                    continue
                blocked = blocked_entry_by_symbol.setdefault(symbol, set())
                blocked.add("long")

        if net_short > cap_notional + 1e-9:
            breach_codes.append("MULTIDAY_SHORT_EXPOSURE_CAP")
            reasons["MULTIDAY_SHORT_EXPOSURE_CAP"] = (
                f"net short multi-day exposure ${net_short:,.0f} exceeds cap "
                f"${cap_notional:,.0f} ({self.max_multiday_directional_exposure_fraction:.0%} equity)"
            )
            for symbol, notional in short_by_symbol.items():
                if notional <= 0.0:
                    continue
                blocked = blocked_entry_by_symbol.setdefault(symbol, set())
                blocked.add("short")

        if holding_breach_by_symbol:
            breach_codes.append("MULTIDAY_HOLDING_DURATION")
            reasons["MULTIDAY_HOLDING_DURATION"] = "; ".join(
                holding_breach_by_symbol.values()
            )
            for row in multiday_positions:
                if int(row.calendar_days_held) < self.max_multiday_holding_calendar_days:
                    continue
                symbol = row.symbol.upper()
                blocked = blocked_entry_by_symbol.setdefault(symbol, set())
                blocked.add(row.side)

        directives: list[str] = []
        directive: str | None = None
        allowed = True
        if breach_codes:
            directives.append(GLOBAL_ENTRY_LOCKOUT)
            allowed = False
        if force_liquidation_targets:
            directives.append(FORCE_LIQUIDATION_EXECUTE)
            allowed = False
        if directives:
            directive = directives[0]

        verdict = InventoryPathDependencyVerdict(
            directive=directive,
            allowed=allowed,
            net_long_exposure=net_long,
            net_short_exposure=net_short,
            multiday_net_exposure=multiday_net_exposure,
            multiday_exposure_ratio=multiday_exposure_ratio,
            cap_notional=cap_notional,
            blocked_entry_by_symbol={
                symbol: frozenset(directions)
                for symbol, directions in blocked_entry_by_symbol.items()
            },
            breach_codes=tuple(breach_codes),
            reasons=reasons,
            directives=tuple(directives),
            force_liquidation_targets=tuple(force_liquidation_targets),
        )
        self.state.last_inventory_verdict = verdict
        return verdict

    @staticmethod
    def calendar_days_between(opened_session_date: str, as_of: date | None = None) -> int:
        """Inclusive calendar-day count from first held session through as_of."""
        if not opened_session_date:
            return 0
        try:
            opened = date.fromisoformat(opened_session_date[:10])
        except ValueError:
            return 0
        today = as_of or datetime.now(timezone.utc).date()
        return max(1, (today - opened).days + 1)

    def resolve_signal_conflicts(
        self,
        active_signals: Mapping[str, Signal | None],
        *,
        leg_metrics: Mapping[str, LegMetrics],
        symbol_correlations: Mapping[tuple[str, str], float] | None = None,
    ) -> SignalConflictResolution:
        """
        Resolve cross-leg collisions:

        - Same symbol, opposing entry directions → block lower marginal Sharpe leg.
        - Highly correlated symbols with opposing entry directions → block weaker leg.
        - Concurrent same-direction correlated entries → keep higher marginal Sharpe leg
          when portfolio mode is single-leg.
        """
        approved: dict[str, Signal | None] = dict(active_signals)
        blocked: set[str] = set()
        scales: dict[str, float] = {k: 1.0 for k in active_signals}
        reasons: dict[str, str] = {}
        correlations = symbol_correlations or {}

        entries: dict[str, Signal] = {
            sid: sig
            for sid, sig in active_signals.items()
            if sig is not None and _is_entry_action(sig.action)
        }

        by_symbol: dict[str, list[tuple[str, Signal]]] = {}
        for sid, sig in entries.items():
            by_symbol.setdefault(sig.symbol.upper(), []).append((sid, sig))

        for symbol, group in by_symbol.items():
            if len(group) < 2:
                continue
            directions = {_signal_direction(sig.action) for _, sig in group}
            if len(directions) == 1:
                continue
            ranked = sorted(
                group,
                key=lambda item: leg_metrics.get(
                    item[0],
                    LegMetrics(item[0], symbol, VOL_FLOOR, 0.0, MIN_MARGINAL_SHARPE),
                ).marginal_sharpe,
                reverse=True,
            )
            winner_id = ranked[0][0]
            for sid, _ in ranked[1:]:
                blocked.add(sid)
                approved[sid] = None
                reasons[sid] = f"same_symbol_opposing_entry:{symbol}:kept={winner_id}"

        remaining = {
            sid: sig
            for sid, sig in entries.items()
            if sid not in blocked and sig is not None
        }
        remaining_ids = list(remaining.keys())
        for i, sid_a in enumerate(remaining_ids):
            sig_a = remaining[sid_a]
            if sig_a is None:
                continue
            for sid_b in remaining_ids[i + 1 :]:
                sig_b = remaining[sid_b]
                if sig_b is None:
                    continue
                sym_a = sig_a.symbol.upper()
                sym_b = sig_b.symbol.upper()
                if sym_a == sym_b:
                    continue
                corr = correlations.get((sym_a, sym_b), correlations.get((sym_b, sym_a), 0.0))
                dir_a = _signal_direction(sig_a.action)
                dir_b = _signal_direction(sig_b.action)
                if dir_a == 0 or dir_b == 0:
                    continue
                if corr < OPPOSING_SIGNAL_CORRELATION_FLOOR:
                    continue

                metrics_a = leg_metrics.get(sid_a)
                metrics_b = leg_metrics.get(sid_b)
                sharpe_a = metrics_a.marginal_sharpe if metrics_a else MIN_MARGINAL_SHARPE
                sharpe_b = metrics_b.marginal_sharpe if metrics_b else MIN_MARGINAL_SHARPE

                if dir_a != dir_b:
                    loser = sid_b if sharpe_a >= sharpe_b else sid_a
                    winner = sid_a if loser == sid_b else sid_b
                    blocked.add(loser)
                    approved[loser] = None
                    reasons[loser] = (
                        f"correlated_opposing_entry:corr={corr:.3f}:kept={winner}"
                    )
                elif self.state.mode is not None and self.state.mode.single_leg_mode:
                    loser = sid_b if sharpe_a >= sharpe_b else sid_a
                    winner = sid_a if loser == sid_b else sid_b
                    blocked.add(loser)
                    approved[loser] = None
                    reasons[loser] = (
                        f"single_leg_correlated_same_direction:kept={winner}"
                    )

        mode = self.state.mode
        if mode is not None and not mode.spy_leg_enabled:
            if SPY_STRATEGY_ID in approved and approved[SPY_STRATEGY_ID] is not None:
                sig = approved[SPY_STRATEGY_ID]
                if sig is not None and _is_entry_action(sig.action):
                    blocked.add(SPY_STRATEGY_ID)
                    approved[SPY_STRATEGY_ID] = None
                    reasons[SPY_STRATEGY_ID] = f"spy_disabled:{mode.reason}"

        if mode is not None and mode.single_leg_mode and mode.dominant_strategy_id:
            for sid, sig in list(approved.items()):
                if sig is None or not _is_entry_action(sig.action):
                    continue
                if sid != mode.dominant_strategy_id:
                    blocked.add(sid)
                    approved[sid] = None
                    reasons[sid] = f"single_leg_mode:dominant={mode.dominant_strategy_id}"

        resolution = SignalConflictResolution(
            approved_signals=approved,
            blocked_strategy_ids=frozenset(blocked),
            scale_multipliers=scales,
            reasons=reasons,
        )
        self.state.last_conflict_resolution = resolution
        return resolution

    def estimate_proposed_positions(
        self,
        signals: Mapping[str, Signal | None],
        positions: list[Position],
        leg_metrics: Mapping[str, LegMetrics],
        *,
        equity: float,
    ) -> list[ProposedPosition]:
        pos_map = {p.symbol.upper(): p for p in positions}
        proposed: list[ProposedPosition] = []

        for strategy_id, metrics in leg_metrics.items():
            symbol = metrics.symbol.upper()
            existing = pos_map.get(symbol)
            if existing is not None:
                notional = abs(existing.qty) * max(existing.avg_entry_price, 0.0)
                proposed.append(
                    ProposedPosition(
                        strategy_id=strategy_id,
                        symbol=symbol,
                        side=existing.side,
                        notional=notional,
                        beta=_symbol_beta(symbol),
                        sector=_symbol_sector(symbol),
                        factor_loadings=_symbol_factors(symbol),
                    )
                )

        budgets = self.state.risk_budgets or _normalize_weights(
            {sid: 1.0 for sid in leg_metrics}
        )
        for strategy_id, signal in signals.items():
            if signal is None or not _is_entry_action(signal.action):
                continue
            metrics = leg_metrics.get(strategy_id)
            if metrics is None:
                continue
            budget = budgets.get(strategy_id, 0.0)
            notional = equity * budget * 0.95
            side = "long" if signal.action == SignalAction.LONG else "short"
            symbol = signal.symbol.upper()
            proposed.append(
                ProposedPosition(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    side=side,
                    notional=notional,
                    beta=_symbol_beta(symbol),
                    sector=_symbol_sector(symbol),
                    factor_loadings=_symbol_factors(symbol),
                )
            )
        return proposed

    def annualized_vol_from_closes(self, closes: np.ndarray, bars_per_year: int = 252 * 26) -> float:
        if len(closes) < 3:
            return VOL_FLOOR
        returns = np.diff(np.log(closes))
        return max(float(np.std(returns)) * math.sqrt(bars_per_year), VOL_FLOOR)
