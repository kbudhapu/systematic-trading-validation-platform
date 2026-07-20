"""
Pure attribution reconciliation and slippage calibration helpers.

Extracted from scripts/reconcile_and_calibrate.py so unit tests avoid importing
the Numba replay pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from src.engine.attribution import row_slippage_for_direction
from src.engine.slippage_calibration import (
    EXECUTION_DIRECTION_TYPES,
    SESSION_TYPES,
    default_asymmetric_multipliers,
)
from src.router.risk_manager import SESSION_MIDDAY_DOLDRUMS


@dataclass(frozen=True)
class ReplayResult:
    symbol: str
    regime: str
    period_pnl: float
    period_return: float
    trades: int
    effective_slippage_pct: float
    spread_proxy_pct: float
    participation_cap_pct: float
    daily_pnl: dict[str, float]


@dataclass(frozen=True)
class ReconciliationReport:
    lookback_days: int
    live_total_pnl: float
    replay_total_pnl: float
    pnl_delta: float
    daily_variance: float
    variance_tolerance: float
    pnl_tolerance: float
    breach: bool
    per_symbol: dict[str, dict[str, float]]
    per_session_live: dict[str, float]
    per_session_replay_alloc: dict[str, float]


@dataclass(frozen=True)
class SlippageCalibration:
    base_slippage_pct: float
    current_multipliers: dict[str, dict[str, float]]
    calibrated_multipliers: dict[str, dict[str, float]]
    sample_counts: dict[str, dict[str, int]]
    implied_from_slippage: dict[str, dict[str, float]]
    implied_from_markout: dict[str, dict[str, float]]


def aligned_daily_variance(
    live_daily: dict[str, float],
    replay_daily: dict[str, float],
) -> tuple[float, list[tuple[str, float, float, float]]]:
    days = sorted(set(live_daily) | set(replay_daily))
    deltas: list[float] = []
    breakdown: list[tuple[str, float, float, float]] = []
    for day in days:
        live = live_daily.get(day, 0.0)
        replay = replay_daily.get(day, 0.0)
        delta = live - replay
        deltas.append(delta)
        breakdown.append((day, live, replay, delta))
    if len(deltas) < 2:
        return 0.0, breakdown
    return float(np.var(deltas, ddof=1)), breakdown


def aggregate_live_daily_pnl(rows: list[dict[str, Any]]) -> dict[str, float]:
    daily: dict[str, float] = {}
    for row in rows:
        ts = datetime.fromisoformat(str(row["timestamp"]))
        day = ts.date().isoformat()
        daily[day] = daily.get(day, 0.0) + float(row["pnl"])
    return daily


class TrackingErrorAnalyzer:
    def __init__(
        self,
        *,
        variance_tolerance: float,
        pnl_tolerance: float,
    ) -> None:
        self.variance_tolerance = variance_tolerance
        self.pnl_tolerance = pnl_tolerance

    def analyze(
        self,
        *,
        lookback_days: int,
        live_rows: list[dict[str, Any]],
        replay_results: list[ReplayResult],
    ) -> ReconciliationReport:
        live_total = sum(float(r["pnl"]) for r in live_rows)
        replay_total = sum(r.period_pnl for r in replay_results)
        pnl_delta = live_total - replay_total
        live_daily = aggregate_live_daily_pnl(live_rows)
        replay_daily: dict[str, float] = {}
        for result in replay_results:
            for day, pnl in result.daily_pnl.items():
                replay_daily[day] = replay_daily.get(day, 0.0) + pnl
        variance, _ = aligned_daily_variance(live_daily, replay_daily)
        breach = (
            variance > self.variance_tolerance
            or abs(pnl_delta) > self.pnl_tolerance
        )
        per_symbol: dict[str, dict[str, float]] = {}
        for row in live_rows:
            sym = str(row["symbol"]).upper()
            bucket = per_symbol.setdefault(
                sym,
                {"live_pnl": 0.0, "replay_pnl": 0.0, "trade_count": 0.0},
            )
            bucket["live_pnl"] += float(row["pnl"])
            bucket["trade_count"] += 1.0
        for replay in replay_results:
            bucket = per_symbol.setdefault(
                replay.symbol,
                {"live_pnl": 0.0, "replay_pnl": 0.0, "trade_count": 0.0},
            )
            bucket["replay_pnl"] += replay.period_pnl
        per_session_live: dict[str, float] = {}
        for row in live_rows:
            session = str(row["session_type"])
            per_session_live[session] = per_session_live.get(session, 0.0) + float(
                row["pnl"]
            )
        live_session_total = sum(abs(v) for v in per_session_live.values()) or 1.0
        replay_alloc = replay_total
        per_session_replay_alloc = {
            session: replay_alloc * (abs(pnl) / live_session_total)
            for session, pnl in per_session_live.items()
        }
        return ReconciliationReport(
            lookback_days=lookback_days,
            live_total_pnl=live_total,
            replay_total_pnl=replay_total,
            pnl_delta=pnl_delta,
            daily_variance=variance,
            variance_tolerance=self.variance_tolerance,
            pnl_tolerance=self.pnl_tolerance,
            breach=breach,
            per_symbol=per_symbol,
            per_session_live=per_session_live,
            per_session_replay_alloc=per_session_replay_alloc,
        )


class SlippageModelCalibrator:
    def __init__(self, base_slippage_pct: float) -> None:
        self.base_slippage_pct = base_slippage_pct

    def calibrate(self, rows: list[dict[str, Any]]) -> SlippageCalibration:
        defaults = default_asymmetric_multipliers()
        implied_slippage: dict[str, dict[str, list[float]]] = {
            session: {direction: [] for direction in EXECUTION_DIRECTION_TYPES}
            for session in SESSION_TYPES
        }
        implied_markout: dict[str, dict[str, list[float]]] = {
            session: {direction: [] for direction in EXECUTION_DIRECTION_TYPES}
            for session in SESSION_TYPES
        }

        for row in rows:
            session = str(row.get("session_type") or SESSION_MIDDAY_DOLDRUMS)
            if session not in implied_slippage:
                continue
            direction = str(row.get("execution_direction_type") or "")
            if direction not in EXECUTION_DIRECTION_TYPES:
                for candidate in EXECUTION_DIRECTION_TYPES:
                    slip_value = row_slippage_for_direction(row, candidate)
                    if slip_value is not None and slip_value > 0.0:
                        direction = candidate
                        break
                else:
                    continue

            slip = row_slippage_for_direction(row, direction)
            if slip is None:
                slip = float(row.get("slippage_pct") or 0.0)
            if slip > 0.0 and self.base_slippage_pct > 0.0:
                implied_slippage[session][direction].append(
                    slip / self.base_slippage_pct
                )

            markout = row.get("markout_5bar")
            if markout is not None and self.base_slippage_pct > 0.0:
                cost = max(0.0, -float(markout))
                implied_markout[session][direction].append(
                    cost / self.base_slippage_pct
                )

        calibrated: dict[str, dict[str, float]] = {}
        from_slippage: dict[str, dict[str, float]] = {}
        from_markout: dict[str, dict[str, float]] = {}
        counts: dict[str, dict[str, int]] = {}

        for session in SESSION_TYPES:
            calibrated[session] = {}
            from_slippage[session] = {}
            from_markout[session] = {}
            counts[session] = {}
            for direction in EXECUTION_DIRECTION_TYPES:
                slip_vals = implied_slippage[session][direction]
                mark_vals = implied_markout[session][direction]
                counts[session][direction] = len(slip_vals)
                current = defaults[session][direction]
                slip_est = float(np.median(slip_vals)) if slip_vals else current
                mark_est = float(np.median(mark_vals)) if mark_vals else current
                from_slippage[session][direction] = slip_est
                from_markout[session][direction] = mark_est
                if slip_vals and mark_vals:
                    calibrated[session][direction] = float(
                        np.median([slip_est, mark_est])
                    )
                elif slip_vals:
                    calibrated[session][direction] = slip_est
                elif mark_vals:
                    calibrated[session][direction] = mark_est
                else:
                    calibrated[session][direction] = current

        return SlippageCalibration(
            base_slippage_pct=self.base_slippage_pct,
            current_multipliers=defaults,
            calibrated_multipliers=calibrated,
            sample_counts=counts,
            implied_from_slippage=from_slippage,
            implied_from_markout=from_markout,
        )
