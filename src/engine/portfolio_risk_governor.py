from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import structlog

from src.config import DB_PATH, StrategyConfig
from src.core.rolling_window import RollingWindow
from src.engine.leg_performance import match_position
from src.engine.portfolio_brain import ONE_RISK_UNIT_CORRELATION, compute_return_correlation
from src.models import Position, SignalAction
from src.persistence.portfolio_risk_state_store import payload_fingerprint

log = structlog.get_logger()

# Unified with the coordinator's opposing-signal gate -- see portfolio_brain.ONE_RISK_UNIT_CORRELATION.
# Same "one risk unit" fact (0.85); here it clamps BOTH sizes on SAME-direction concentration.
MAX_SAFE_CORRELATION = ONE_RISK_UNIT_CORRELATION
CORRELATION_CLAMP_MULTIPLIER = 0.6
STRATEGY_MAX_DRAWDOWN = 0.06
MIN_CORRELATION_SAMPLES = 20
EQUITY_HISTORY_LIMIT = 256

# GV2-3 (operator-signed 2026-07-17): the formula stamp for persisted governor state. Peaks/
# drawdowns are only meaningful under the equity formula that produced them — the pre-E5 fossils
# (peaks = retired 75%/50% capital-base splits) rehydrated under E5's broker_equity × PAD-fraction
# input latched both legs exit_only for a day. State stamped under a RETIRED (or absent) formula
# version is REFUSED at rehydration: fresh-peak from the first real reading + a CRITICAL-visible
# log. A formula change MUST bump this constant — that is the point: formula changes invalidate
# the state they orphan.
#
# GV-3 (operator-signed 2026-07-20): bumped from "v-E5-padfrac". Drawdown is now computed on the
# leg's cumulative realized+unrealized PnL curve — see _compute_drawdown_from_peak. The bump is
# load-bearing: it makes the GV2-3 refusal path retire every v-E5-padfrac row automatically, so
# the poisoned peaks (both legs carried inflated peak_equity rows with large phantom drawdowns
# from the 2026-07-20 commissioning incident) are refused at first rehydration rather than
# immortalized by the ratchet.
FORMULA_VERSION = "v-GV3-pnlcurve"


@dataclass(frozen=True)
class GovernorVerdict:
    sizing_multipliers: dict[str, float]
    exit_only_strategy_ids: frozenset[str]
    correlation_pairs: dict[tuple[str, str], float]
    strategy_drawdowns: dict[str, float]
    cold_start_clamped_strategy_ids: frozenset[str] = field(default_factory=frozenset)  # type: ignore[assignment]


@dataclass
class PortfolioRiskGovernor:
    max_safe_correlation: float = MAX_SAFE_CORRELATION
    clamp_multiplier: float = CORRELATION_CLAMP_MULTIPLIER
    strategy_max_drawdown: float = STRATEGY_MAX_DRAWDOWN
    min_correlation_samples: int = MIN_CORRELATION_SAMPLES
    _equity_history_by_strategy: dict[str, deque[float]] = field(default_factory=dict)
    _strategy_peaks: dict[str, float] = field(default_factory=dict)
    _strategy_drawdowns: dict[str, float] = field(default_factory=dict)
    _strategy_exit_only: set[str] = field(default_factory=set)
    _last_persisted_fingerprint: str = ""
    # GV-3: the drawdown denominator. Monotonic high-water of BROKER equity — broker truth, and
    # allocation-free by construction. A ratchet (not spot equity) so that a loss cannot shrink
    # its own denominator and inflate the ratio it is measured by.
    _peak_broker_equity: float = 0.0

    def evaluate(
        self,
        *,
        enabled_configs: list[StrategyConfig],
        windows: Mapping[str, RollingWindow],
        positions: list[Position],
        realized_pnls: Mapping[str, float],
        broker_equity: float,
        allocation_fractions: Mapping[str, float],
    ) -> GovernorVerdict:
        sizing_multipliers = {cfg.strategy_id: 1.0 for cfg in enabled_configs if cfg.enabled}
        exit_only: set[str] = set()
        correlations = self._compute_correlation_pairs(enabled_configs, windows)
        strategy_drawdowns: dict[str, float] = {}
        # GV-3: ratchet the drawdown denominator before any leg is measured, so every leg in this
        # cycle is scored against the same account high-water.
        self._peak_broker_equity = max(self._peak_broker_equity, float(broker_equity))

        for cfg in enabled_configs:
            if not cfg.enabled:
                continue
            # GV2-1 (A2, operator-signed 2026-07-17): a leg ABSENT from this cycle's allocation
            # fractions was NOT DUE — its allocation is UNKNOWN, never zero. The old
            # `.get(sid, 0.0)` manufactured equity=0 → drawdown=1.0 → exit_only on every non-due
            # cycle (QQQ 14/15, BTC 59/60 — GV-1 Finding A2). Design choice: SKIP the update and
            # carry prior state into the verdict, rather than carry-forward a stale fraction —
            # the equity history deque feeds _resolve_peak and must contain only REAL readings; a
            # stale fraction × a moving broker equity is a synthetic sample, the same fabricated-
            # input class this remediation exists to remove. No reading → no update.
            #
            # GV-3: this skip is no longer load-bearing for CORRECTNESS — the PnL curve below is
            # readable on every cycle, so an absent fraction can no longer fabricate anything. It
            # is retained deliberately as the not-due CADENCE gate (measure a leg on the cycles it
            # is due, matching the behaviour the A2 tests pin) and is the ONLY remaining use of
            # allocation_fractions in this method. It decides WHETHER to take a reading; it is
            # never part of one.
            if cfg.strategy_id not in allocation_fractions:
                prior_dd = self._strategy_drawdowns.get(cfg.strategy_id, 0.0)
                strategy_drawdowns[cfg.strategy_id] = prior_dd
                if cfg.strategy_id in self._strategy_exit_only:
                    exit_only.add(cfg.strategy_id)
                continue
            # GV-3 (operator-signed 2026-07-20) — THE FIX. Drawdown is computed on the leg's
            # cumulative PnL curve (realized accumulator + unrealized from the broker position),
            # which is BROKER TRUTH and invariant to how much capital the coordinator currently
            # assigns the leg.
            #
            # What this replaces, and why: E5 set the base to broker_equity × plan.risk_fraction.
            # risk_fraction is a COORDINATOR PLAN, not an observation — and it moves every cycle.
            # Live on 2026-07-20 it oscillated between a high and a low allocation fraction on
            # consecutive cycles nine minutes apart, with zero trades. Fed into the monotonic peak
            # ratchet below, the peak latched at the high allocation and every lower-allocation cycle
            # read as a loss: both legs exit_only, on an account with ZERO real fills. The drawdown
            # reconstructs exactly from the stale-fraction x moving-broker-equity product — i.e. a
            # synthetic sample, not a real loss.
            #
            # This is the SAME synthetic-input class the GV2-1 comment above names ("a stale
            # fraction × a moving broker equity is a synthetic sample"). GV-2 applied that doctrine
            # to the ABSENT-allocation door only and left the PRESENT-but-VARYING door open; the
            # clamp re-poisoned within hours of the operator-signed reset (CLAMP_ENGAGED counts
            # 115/117/117 on 07-17/18/19). allocation_fractions is now barred from the measurement
            # entirely — it survives below only as a not-due CADENCE gate, never as an input.
            pos = match_position(cfg.symbol, positions)
            unrealized = pos.unrealized_pl if pos is not None else 0.0
            leg_pnl = float(realized_pnls.get(cfg.strategy_id, 0.0)) + float(unrealized)
            history = self._equity_history_by_strategy.setdefault(
                cfg.strategy_id,
                deque(maxlen=EQUITY_HISTORY_LIMIT),
            )
            history.append(leg_pnl)
            # The ratchet is CORRECT on a PnL curve and stays: peak profit only ever rises, and a
            # leg that has never profited has peak 0.0 — so the 0.0 defaults in _resolve_peak are
            # now the semantically right floor rather than a fabricated equity reading.
            peak = self._resolve_peak(cfg.strategy_id, leg_pnl, history)
            drawdown = self._compute_drawdown_from_peak(
                peak, leg_pnl, self._peak_broker_equity
            )
            strategy_drawdowns[cfg.strategy_id] = drawdown
            self._strategy_drawdowns[cfg.strategy_id] = drawdown
            if drawdown > self.strategy_max_drawdown:
                exit_only.add(cfg.strategy_id)
                self._strategy_exit_only.add(cfg.strategy_id)
            else:
                self._strategy_exit_only.discard(cfg.strategy_id)

        for (strategy_a, strategy_b), corr in correlations.items():
            if abs(corr) < self.max_safe_correlation:
                continue
            sizing_multipliers[strategy_a] = min(
                sizing_multipliers.get(strategy_a, 1.0),
                self.clamp_multiplier,
            )
            sizing_multipliers[strategy_b] = min(
                sizing_multipliers.get(strategy_b, 1.0),
                self.clamp_multiplier,
            )

        cold_start_clamped: set[str] = set()
        for cfg in enabled_configs:
            if not cfg.enabled:
                continue
            window = windows.get(cfg.strategy_id)
            bars_available = (
                len(window.closes_array()) if window is not None else 0
            )
            if bars_available < self.min_correlation_samples:
                sizing_multipliers[cfg.strategy_id] = min(
                    sizing_multipliers.get(cfg.strategy_id, 1.0),
                    CORRELATION_CLAMP_MULTIPLIER,
                )
                cold_start_clamped.add(cfg.strategy_id)

        if exit_only or any(mult < 1.0 for mult in sizing_multipliers.values()):
            log.warning(
                "risk_governor: CLAMP_ENGAGED",
                sizing_multipliers=sizing_multipliers,
                exit_only_strategy_ids=sorted(exit_only),
                correlation_pairs={
                    f"{left}:{right}": value for (left, right), value in correlations.items()
                },
                strategy_drawdowns=strategy_drawdowns,
                max_safe_correlation=self.max_safe_correlation,
                strategy_max_drawdown=self.strategy_max_drawdown,
            )

        return GovernorVerdict(
            sizing_multipliers=sizing_multipliers,
            exit_only_strategy_ids=frozenset(exit_only),
            correlation_pairs=correlations,
            strategy_drawdowns=strategy_drawdowns,
            cold_start_clamped_strategy_ids=frozenset(cold_start_clamped),
        )

    def _compute_correlation_pairs(
        self,
        enabled_configs: list[StrategyConfig],
        windows: Mapping[str, RollingWindow],
    ) -> dict[tuple[str, str], float]:
        correlations: dict[tuple[str, str], float] = {}
        ordered = [cfg for cfg in enabled_configs if cfg.enabled]
        for idx, left_cfg in enumerate(ordered):
            left_window = windows.get(left_cfg.strategy_id)
            left_closes = (
                left_window.closes_array()
                if left_window is not None
                else np.asarray([], dtype=np.float64)
            )
            if len(left_closes) < self.min_correlation_samples:
                continue
            for right_cfg in ordered[idx + 1 :]:
                right_window = windows.get(right_cfg.strategy_id)
                right_closes = (
                    right_window.closes_array()
                    if right_window is not None
                    else np.asarray([], dtype=np.float64)
                )
                if len(right_closes) < self.min_correlation_samples:
                    continue
                corr = compute_return_correlation(left_closes, right_closes)
                correlations[(left_cfg.strategy_id, right_cfg.strategy_id)] = corr
                correlations[(right_cfg.strategy_id, left_cfg.strategy_id)] = corr
        return correlations

    def _resolve_peak(
        self,
        strategy_id: str,
        equity: float,
        history: deque[float],
    ) -> float:
        history_peak = max((float(value) for value in history), default=0.0)
        peak = max(
            float(self._strategy_peaks.get(strategy_id, 0.0)),
            history_peak,
            float(equity),
        )
        self._strategy_peaks[strategy_id] = peak
        return peak

    @staticmethod
    def _compute_drawdown_from_peak(
        peak_pnl: float, leg_pnl: float, peak_broker_equity: float
    ) -> float:
        """Trailing drawdown as a fraction of the account high-water.

        GV-3: numerator is the fall from peak cumulative PnL; denominator is peak BROKER equity.
        Both terms are broker truth. The old form divided by the peak of a leg-notional series
        whose base was the coordinator's allocation fraction — so a reallocation alone produced a
        drawdown (see evaluate()).

        Denominator note for the merge review: this is peak ACCOUNT equity, not per-leg capital,
        because no allocation-free per-leg capital reference exists — leg capital is *defined* by
        the allocation fraction we just barred. strategy_max_drawdown (0.06) is unchanged per the
        ruling, and on the fixture the old suite encoded (constant fraction, equity falls 8%) this
        form returns the identical 0.08. It diverges only where the old one was measuring the
        allocation rather than the trading.
        """
        if peak_broker_equity <= 0.0:
            return 0.0
        return max(0.0, (float(peak_pnl) - float(leg_pnl)) / float(peak_broker_equity))

    def serialize_risk_state(self) -> dict[str, Any]:
        strategies: dict[str, dict[str, Any]] = {}
        strategy_ids = set(self._strategy_peaks) | set(self._strategy_drawdowns) | set(
            self._strategy_exit_only
        )
        for strategy_id in sorted(strategy_ids):
            strategies[strategy_id] = {
                # Schema note: the column is named `peak_equity` and stays so (renaming it is a
                # migration, out of scope). Under v-GV3-pnlcurve it holds PEAK CUMULATIVE PnL, not
                # a notional equity. The formula_version stamp is what disambiguates the two.
                "peak_equity": float(self._strategy_peaks.get(strategy_id, 0.0)),
                "trailing_drawdown_pct": float(
                    self._strategy_drawdowns.get(strategy_id, 0.0)
                ),
                "exit_only_mode": strategy_id in self._strategy_exit_only,
            }
        return {
            "version": 2,
            "formula_version": FORMULA_VERSION,
            "peak_broker_equity": float(self._peak_broker_equity),
            "strategies": strategies,
        }

    def rehydrate_risk_state(
        self,
        payload: Mapping[str, Any] | None,
        *,
        strategy_baselines: Mapping[str, float] | None = None,
    ) -> bool:
        strategies = {}
        if payload is not None:
            raw = payload.get("strategies", {})
            if isinstance(raw, Mapping):
                strategies = dict(raw)
        # GV2-3: REFUSE state stamped under a retired (or absent) formula version. The values are
        # not merely stale — they are meaningless under the current equity formula, and the peak
        # ratchet would immortalize them (the pre-E5 fossil incident, GV-1 Finding A). Loud by
        # design: CRITICAL-visible, never silent. Fresh peaks seed from the first REAL per-leg
        # reading in evaluate() — deliberately NOT from strategy_baselines, whose equal-split
        # arithmetic is the same retired formula (equity/leg_count ≈ the 50% fossil).
        if strategies:
            stamped = payload.get("formula_version") if payload is not None else None
            if stamped != FORMULA_VERSION:
                log.critical(
                    "risk_governor: STATE_REFUSED_RETIRED_FORMULA",
                    stamped_formula_version=stamped,
                    current_formula_version=FORMULA_VERSION,
                    refused_strategies={
                        sid: dict(row) if isinstance(row, Mapping) else row
                        for sid, row in strategies.items()
                    },
                    action="fresh-peak from first real reading; refused state left archived in store history",
                )
                self._equity_history_by_strategy.clear()
                self._strategy_peaks.clear()
                self._strategy_drawdowns.clear()
                self._strategy_exit_only.clear()
                self._peak_broker_equity = 0.0
                self._last_persisted_fingerprint = ""
                return False
        if not strategies:
            if strategy_baselines:
                for strategy_id in strategy_baselines:
                    # GV-3: a baseline is a NOTIONAL capital figure; the curve we track is PnL.
                    # The correct seed for a leg that has not traded is peak PnL 0.0 — seeding the
                    # baseline here would manufacture a peak profit equal to the leg's capital and
                    # report an instant ~100% drawdown. The baseline values are deliberately
                    # ignored; the key set is honoured so callers still get their legs registered.
                    self._strategy_peaks[strategy_id] = 0.0
                    self._strategy_drawdowns[strategy_id] = 0.0
                    self._strategy_exit_only.discard(strategy_id)
                    self._equity_history_by_strategy[strategy_id] = deque(
                        [0.0],
                        maxlen=EQUITY_HISTORY_LIMIT,
                    )
            self._last_persisted_fingerprint = ""
            return False

        if payload is not None:
            self._peak_broker_equity = float(payload.get("peak_broker_equity", 0.0) or 0.0)
        for strategy_id, row in strategies.items():
            if not isinstance(row, Mapping):
                continue
            peak = float(row.get("peak_equity", 0.0))
            drawdown = float(row.get("trailing_drawdown_pct", 0.0))
            exit_only = bool(row.get("exit_only_mode"))
            self._strategy_peaks[strategy_id] = peak
            self._strategy_drawdowns[strategy_id] = drawdown
            if exit_only:
                self._strategy_exit_only.add(strategy_id)
            else:
                self._strategy_exit_only.discard(strategy_id)
            # GV-3: the old code back-solved a current equity as peak × (1 - dd) and seeded it into
            # the history. That inverse only holds when the denominator IS the peak, which it no
            # longer is — under the PnL form it would fabricate a sample. The persisted peak is a
            # real high-water and is seeded alone; the next evaluate() appends the true reading.
            self._equity_history_by_strategy[strategy_id] = deque(
                [peak],
                maxlen=EQUITY_HISTORY_LIMIT,
            )
        self._last_persisted_fingerprint = payload_fingerprint(self.serialize_risk_state())
        return True

    def maybe_enqueue_persist(self, *, db_path: Path = DB_PATH) -> None:
        payload = self.serialize_risk_state()
        if not payload.get("strategies"):
            return
        fingerprint = payload_fingerprint(payload)
        if fingerprint == self._last_persisted_fingerprint:
            return
        self._last_persisted_fingerprint = fingerprint
        from src.persistence.db_queue import enqueue_portfolio_risk_state

        enqueue_portfolio_risk_state(payload, db_path=str(db_path))

    def blocks_new_entries(
        self,
        strategy_id: str,
        *,
        signal_action: SignalAction | None,
        verdict: GovernorVerdict,
    ) -> bool:
        if signal_action not in (SignalAction.LONG, SignalAction.SHORT):
            return False
        return strategy_id in verdict.exit_only_strategy_ids
