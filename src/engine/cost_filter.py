"""
Frictional cost optimization filter for parameter selection and portfolio allocation.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from src.persistence.db import RESEARCH_VAULT_PATH

from src.engine.slippage_calibration import (
    DEFAULT_CALIBRATION_PATH,
    estimate_exit_stress_execution_bps,
    load_asymmetric_slippage_multipliers,
    resolve_directional_slippage_multiplier,
)

TURNOVER_PENALTY_WEIGHT = 0.35
EXECUTION_PENALTY_WEIGHT = 0.30
BORROW_FEE_PENALTY_WEIGHT = 0.20
SHORT_RECALL_PENALTY_WEIGHT = 0.25
THIN_AVAILABILITY_PENALTY_WEIGHT = 0.15

DEFAULT_BORROW_FEE_ANNUAL = 0.03
ELEVATED_BORROW_FEE_THRESHOLD = 0.05
HIGH_RECALL_RISK_THRESHOLD = 0.65
SHORT_BIAS_THRESHOLD = 1.05

FRICTIONAL_COST_ADJUSTMENTS_DDL = """
CREATE TABLE IF NOT EXISTS frictional_cost_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    composite_score_raw REAL NOT NULL,
    composite_score_adjusted REAL NOT NULL,
    turnover_tax REAL NOT NULL,
    execution_bps REAL NOT NULL,
    borrow_fee_rate REAL NOT NULL,
    short_recall_risk_premium REAL NOT NULL,
    penalty_breakdown_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_frictional_cost_scope
    ON frictional_cost_adjustments(scope_key, timestamp);
"""


@dataclass(frozen=True)
class ShortSideCostContext:
    borrow_fee_rate: float = DEFAULT_BORROW_FEE_ANNUAL
    easy_to_borrow: bool = True
    thin_availability: bool = False
    recall_risk_score: float = 0.0
    short_interest_pressure: float = 0.0
    borrow_stable: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> ShortSideCostContext:
        if data is None:
            return cls()
        return cls(
            borrow_fee_rate=float(
                data.get("borrow_fee_rate", DEFAULT_BORROW_FEE_ANNUAL)
            ),
            easy_to_borrow=bool(data.get("easy_to_borrow", True)),
            thin_availability=bool(data.get("thin_availability", False)),
            recall_risk_score=float(data.get("recall_risk_score", 0.0)),
            short_interest_pressure=float(data.get("short_interest_pressure", 0.0)),
            borrow_stable=bool(data.get("borrow_stable", True)),
        )

    @classmethod
    def from_leg_indicators(
        cls,
        *,
        borrow_stable: bool,
        thin_liquidity_active: bool,
        easy_to_borrow: bool = True,
        borrow_fee_rate: float = DEFAULT_BORROW_FEE_ANNUAL,
    ) -> ShortSideCostContext:
        recall_risk = 0.0
        if not borrow_stable:
            recall_risk += 0.35
        if thin_liquidity_active:
            recall_risk += 0.25
        if not easy_to_borrow:
            recall_risk += 0.40
        recall_risk = min(1.0, recall_risk)
        return cls(
            borrow_fee_rate=borrow_fee_rate,
            easy_to_borrow=easy_to_borrow,
            thin_availability=thin_liquidity_active,
            recall_risk_score=recall_risk,
            short_interest_pressure=recall_risk * 0.5,
            borrow_stable=borrow_stable,
        )


@dataclass(frozen=True)
class CostPenaltyBreakdown:
    turnover_penalty: float
    execution_penalty: float
    borrow_penalty: float
    recall_penalty: float
    thin_availability_penalty: float
    short_operational_multiplier: float
    total_penalty: float


@dataclass(frozen=True)
class CompositeCostAdjustment:
    raw_score: float
    adjusted_score: float
    breakdown: CostPenaltyBreakdown
    short_side_active: bool


@dataclass
class FrictionalCostFilter:
    """Applies fee, turnover, execution, and short-side penalties to selection scores."""

    db_path: Path = RESEARCH_VAULT_PATH
    calibration_path: Path = DEFAULT_CALIBRATION_PATH
    turnover_weight: float = TURNOVER_PENALTY_WEIGHT
    execution_weight: float = EXECUTION_PENALTY_WEIGHT
    borrow_weight: float = BORROW_FEE_PENALTY_WEIGHT
    recall_weight: float = SHORT_RECALL_PENALTY_WEIGHT
    thin_availability_weight: float = THIN_AVAILABILITY_PENALTY_WEIGHT
    _asymmetric_multipliers: dict[str, dict[str, float]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        ensure_frictional_cost_schema(self.db_path)
        self._asymmetric_multipliers = load_asymmetric_slippage_multipliers(
            self.calibration_path
        )

    def reload_slippage_calibration(self) -> dict[str, dict[str, float]]:
        self._asymmetric_multipliers = load_asymmetric_slippage_multipliers(
            self.calibration_path
        )
        return dict(self._asymmetric_multipliers)

    def directional_execution_multiplier(
        self,
        *,
        session: str,
        direction: str,
    ) -> float:
        return resolve_directional_slippage_multiplier(
            self._asymmetric_multipliers,
            session=session,
            direction=direction,
        )

    def apply_composite_cost_penalties(
        self,
        composite_score: float,
        turnover_tax: float,
        execution_bps: float,
        borrow_fee_rate: float,
        short_recall_risk_premium: float,
        *,
        short_context: ShortSideCostContext | None = None,
        short_bias: float = 1.0,
        scope_key: str = "GLOBAL",
        persist: bool = True,
    ) -> CompositeCostAdjustment:
        ctx = short_context or ShortSideCostContext()
        short_side_active = short_bias >= SHORT_BIAS_THRESHOLD

        turnover_penalty = self._turnover_penalty(turnover_tax)
        execution_penalty = self._execution_penalty(execution_bps)
        borrow_penalty = self._borrow_penalty(
            borrow_fee_rate,
            ctx,
            short_side_active=short_side_active,
        )
        recall_penalty = self._recall_penalty(
            short_recall_risk_premium,
            ctx,
            short_side_active=short_side_active,
        )
        thin_penalty = self._thin_availability_penalty(ctx, short_side_active)

        short_multiplier = self._short_operational_multiplier(ctx, short_side_active)
        component_total = (
            turnover_penalty
            + execution_penalty
            + borrow_penalty
            + recall_penalty
            + thin_penalty
        )
        total_penalty = component_total * short_multiplier

        raw = float(composite_score)
        adjusted = raw - total_penalty

        breakdown = CostPenaltyBreakdown(
            turnover_penalty=turnover_penalty,
            execution_penalty=execution_penalty,
            borrow_penalty=borrow_penalty,
            recall_penalty=recall_penalty,
            thin_availability_penalty=thin_penalty,
            short_operational_multiplier=short_multiplier,
            total_penalty=total_penalty,
        )
        result = CompositeCostAdjustment(
            raw_score=raw,
            adjusted_score=adjusted,
            breakdown=breakdown,
            short_side_active=short_side_active,
        )
        if persist:
            self._persist_adjustment(
                scope_key=scope_key,
                turnover_tax=turnover_tax,
                execution_bps=execution_bps,
                borrow_fee_rate=borrow_fee_rate,
                short_recall_risk_premium=short_recall_risk_premium,
                result=result,
            )
        return result

    def adjust_parameter_row(
        self,
        row: Mapping[str, Any],
        *,
        execution_bps: float,
        borrow_fee_rate: float = DEFAULT_BORROW_FEE_ANNUAL,
        short_context: ShortSideCostContext | None = None,
        scope_key: str = "",
        base_slippage_pct: float | None = None,
    ) -> float:
        raw_score = float(row.get("composite_score") or 0.0)
        full_trades = float(row.get("full_trades") or 0.0)
        turnover_tax = min(1.0, full_trades / 250.0)

        long_th = float(row.get("long_threshold_sigma") or 1.0)
        short_th = float(row.get("short_threshold_sigma") or 1.0)
        short_bias = short_th / max(long_th, 1e-6)
        max_bars_in_trade = float(row.get("max_bars_in_trade") or 40.0)

        if base_slippage_pct is not None and base_slippage_pct > 0.0:
            effective_execution_bps = estimate_exit_stress_execution_bps(
                base_slippage_pct,
                multipliers=self._asymmetric_multipliers,
                max_bars_in_trade=max_bars_in_trade,
                short_bias=short_bias,
            )
        else:
            exit_stress_mult = max(
                self.directional_execution_multiplier(
                    session="OPENING_CROSS",
                    direction="long_exit",
                ),
                self.directional_execution_multiplier(
                    session="CLOSING_IMBALANCE",
                    direction="long_exit",
                ),
                self.directional_execution_multiplier(
                    session="OPENING_CROSS",
                    direction="short_exit",
                ),
            )
            urgency = min(1.0, 25.0 / max(max_bars_in_trade, 1.0))
            effective_execution_bps = float(execution_bps) * exit_stress_mult
            effective_execution_bps *= 1.0 + urgency * 0.50

        ctx = short_context or ShortSideCostContext(borrow_fee_rate=borrow_fee_rate)
        recall_premium = ctx.recall_risk_score * borrow_fee_rate

        adjusted = self.apply_composite_cost_penalties(
            raw_score,
            turnover_tax=turnover_tax,
            execution_bps=effective_execution_bps,
            borrow_fee_rate=borrow_fee_rate,
            short_recall_risk_premium=recall_premium,
            short_context=ctx,
            short_bias=short_bias,
            scope_key=scope_key or str(row.get("symbol") or "parameter_row"),
            persist=False,
        )
        return adjusted.adjusted_score

    def _turnover_penalty(self, turnover_tax: float) -> float:
        tax = max(float(turnover_tax), 0.0)
        return self.turnover_weight * tax

    def _execution_penalty(self, execution_bps: float) -> float:
        bps = max(float(execution_bps), 0.0)
        return self.execution_weight * (bps / 100.0)

    def _borrow_penalty(
        self,
        borrow_fee_rate: float,
        ctx: ShortSideCostContext,
        *,
        short_side_active: bool,
    ) -> float:
        if not short_side_active:
            return 0.0
        fee = max(float(borrow_fee_rate), 0.0)
        base = self.borrow_weight * fee
        if not ctx.easy_to_borrow:
            base *= 2.0
        if fee >= ELEVATED_BORROW_FEE_THRESHOLD:
            base *= 1.0 + (fee - ELEVATED_BORROW_FEE_THRESHOLD) * 4.0
        if not ctx.borrow_stable:
            base *= 1.35
        return base

    def _recall_penalty(
        self,
        short_recall_risk_premium: float,
        ctx: ShortSideCostContext,
        *,
        short_side_active: bool,
    ) -> float:
        if not short_side_active:
            return 0.0
        premium = max(float(short_recall_risk_premium), 0.0)
        risk = max(ctx.recall_risk_score, premium)
        if risk <= 0.0:
            return 0.0
        recall_component = self.recall_weight * risk
        if risk >= HIGH_RECALL_RISK_THRESHOLD:
            recall_component *= 1.75
        return recall_component

    def _thin_availability_penalty(
        self,
        ctx: ShortSideCostContext,
        short_side_active: bool,
    ) -> float:
        if not short_side_active or not ctx.thin_availability:
            return 0.0
        pressure = max(ctx.short_interest_pressure, 0.25)
        return self.thin_availability_weight * pressure

    def _short_operational_multiplier(
        self,
        ctx: ShortSideCostContext,
        short_side_active: bool,
    ) -> float:
        if not short_side_active:
            return 1.0
        multiplier = 1.0
        if not ctx.easy_to_borrow:
            multiplier += 0.45
        if ctx.thin_availability:
            multiplier += 0.30
        if ctx.recall_risk_score >= HIGH_RECALL_RISK_THRESHOLD:
            multiplier += 0.50
        if not ctx.borrow_stable:
            multiplier += 0.25
        return float(np.clip(multiplier, 1.0, 3.0))

    def _persist_adjustment(
        self,
        *,
        scope_key: str,
        turnover_tax: float,
        execution_bps: float,
        borrow_fee_rate: float,
        short_recall_risk_premium: float,
        result: CompositeCostAdjustment,
    ) -> None:
        breakdown = {
            "turnover_penalty": result.breakdown.turnover_penalty,
            "execution_penalty": result.breakdown.execution_penalty,
            "borrow_penalty": result.breakdown.borrow_penalty,
            "recall_penalty": result.breakdown.recall_penalty,
            "thin_availability_penalty": result.breakdown.thin_availability_penalty,
            "short_operational_multiplier": result.breakdown.short_operational_multiplier,
            "total_penalty": result.breakdown.total_penalty,
            "short_side_active": result.short_side_active,
        }
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO frictional_cost_adjustments (
                    timestamp, scope_key, composite_score_raw,
                    composite_score_adjusted, turnover_tax, execution_bps,
                    borrow_fee_rate, short_recall_risk_premium,
                    penalty_breakdown_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    scope_key,
                    result.raw_score,
                    result.adjusted_score,
                    turnover_tax,
                    execution_bps,
                    borrow_fee_rate,
                    short_recall_risk_premium,
                    json.dumps(breakdown, separators=(",", ":")),
                ),
            )


def ensure_frictional_cost_schema(db_path: Path = RESEARCH_VAULT_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(FRICTIONAL_COST_ADJUSTMENTS_DDL)
