"""
Adaptive execution feedback, markout tracking, routing urgency, and participation curves.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from numba import njit

from src.models import Order, OrderResult, Position, Side
from src.persistence.db import RESEARCH_VAULT_PATH
from src.persistence.ownership_guard import ensure_db_writable
from src.router.risk_manager import (
    THIN_LIQUIDITY_PARTICIPATION_MULT,
    ExecutionDriftDiagnostics,
    _realized_slippage_pct,
    resolve_trading_session,
)

MARKOUT_HORIZONS_MINUTES = (1, 5, 30)
SLIPPAGE_FEEDBACK_WINDOW = 20
ALPHA_DECAY_URGENCY_BPS = 8.0
BOOK_PRESSURE_PASSIVE_THRESHOLD = 0.65
BOOK_PRESSURE_AGGRESSIVE_THRESHOLD = 0.35
THIN_LEVEL1_TOTAL_DEPTH = 50.0
VOLATILE_SPREAD_PCT = 0.015
DEPTH_CONFIDENCE_HIGH = "high"
DEPTH_CONFIDENCE_LOW = "low"
DEPTH_CONFIDENCE_UNAVAILABLE = "unavailable"
MIN_EXPECTED_EDGE_BPS = 5.0
PARTICIPATION_VOL_LOOKBACK = 40
PARTICIPATION_VOL_FLOOR_MULT = 0.35
PARTICIPATION_VOL_CEILING_MULT = 1.0
PARTICIPATION_SIZE_IMPACT_CEILING = 0.50
PARTICIPATION_ADV_NOTIONAL_FRACTION = 0.02

ORDER_SIZE_BUCKET_MICRO = "MICRO"
ORDER_SIZE_BUCKET_SMALL = "SMALL"
ORDER_SIZE_BUCKET_MEDIUM = "MEDIUM"
ORDER_SIZE_BUCKET_LARGE = "LARGE"

ROUTING_POSTURE_PASSIVE_NBBO = "PASSIVE_NBBO"
ROUTING_POSTURE_BALANCED_IOC = "BALANCED_IOC"
ROUTING_POSTURE_AGGRESSIVE_TAKER = "AGGRESSIVE_TAKER"

EXECUTION_FEEDBACK_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS execution_feedback_ledger (
    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    expected_price REAL NOT NULL,
    filled_price REAL NOT NULL,
    expected_slippage_pct REAL NOT NULL,
    realized_slippage_pct REAL NOT NULL,
    slippage_delta_pct REAL NOT NULL,
    session_type TEXT NOT NULL,
    size_bucket TEXT NOT NULL,
    routing_posture TEXT NOT NULL,
    participation_cap_pct REAL NOT NULL,
    markout_1m REAL,
    markout_5m REAL,
    markout_30m REAL,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_feedback_ts
    ON execution_feedback_ledger(timestamp);
CREATE INDEX IF NOT EXISTS idx_exec_feedback_symbol
    ON execution_feedback_ledger(symbol);
"""


class RoutingPosture(str, Enum):
    PASSIVE_NBBO = ROUTING_POSTURE_PASSIVE_NBBO
    BALANCED_IOC = ROUTING_POSTURE_BALANCED_IOC
    AGGRESSIVE_TAKER = ROUTING_POSTURE_AGGRESSIVE_TAKER


@dataclass(frozen=True)
class MarkoutHorizonResult:
    horizon_minutes: int
    markout_bps: float
    reference_price: float
    drift_price: float


@dataclass(frozen=True)
class StratifiedMarkoutBreakdown:
    session_type: str
    size_bucket: str
    horizons: tuple[MarkoutHorizonResult, ...]
    implementation_shortfall_bps: float


@dataclass(frozen=True)
class SlippageFeedbackRecord:
    trade_id: str
    timestamp: datetime
    strategy_id: str
    symbol: str
    expected_slippage_pct: float
    realized_slippage_pct: float
    slippage_delta_pct: float
    session_type: str
    size_bucket: str


@dataclass(frozen=True)
class RoutingUrgencyDecision:
    posture: RoutingPosture
    aggressiveness: float
    alpha_decay_bps: float
    expected_edge_bps: float
    book_pressure: float
    reason: str

    def to_execution_diagnostics(
        self,
        base: ExecutionDriftDiagnostics,
    ) -> ExecutionDriftDiagnostics:
        if self.posture == RoutingPosture.PASSIVE_NBBO:
            return ExecutionDriftDiagnostics(
                average_realized_slippage_pct=base.average_realized_slippage_pct,
                modeled_slippage_pct=base.modeled_slippage_pct,
                drift_multiple=max(base.drift_multiple, 2.0),
                elevated=True,
                critical=False,
            )
        if self.posture == RoutingPosture.AGGRESSIVE_TAKER:
            drift = min(base.drift_multiple, 1.5)
            return ExecutionDriftDiagnostics(
                average_realized_slippage_pct=base.average_realized_slippage_pct,
                modeled_slippage_pct=base.modeled_slippage_pct,
                drift_multiple=drift,
                elevated=False,
                critical=False,
            )
        return base


def emergency_flatten_urgency_context() -> dict[str, Any]:
    """Routing context for unified flatten protocol — always IOC/market taker."""
    return {
        "expected_edge_bps": 100.0,
        "book_pressure": 0.0,
        "alpha_decay_bps": ALPHA_DECAY_URGENCY_BPS,
        "modeled_slippage_bps": 0.0,
        "emergency_flatten": True,
    }


def determine_emergency_flatten_urgency() -> RoutingUrgencyDecision:
    """Force aggressive taker routing for emergency exposure termination."""
    return RoutingUrgencyDecision(
        posture=RoutingPosture.AGGRESSIVE_TAKER,
        aggressiveness=1.0,
        alpha_decay_bps=ALPHA_DECAY_URGENCY_BPS,
        expected_edge_bps=100.0,
        book_pressure=0.0,
        reason="emergency_flatten_protocol",
    )


@dataclass(frozen=True)
class ParticipationCapDecision:
    participation_cap_pct: float
    vol_impact_scalar: float
    size_impact_scalar: float
    trailing_volume: float
    reference_volume: float
    thin_liquidity_active: bool


@dataclass
class ExecutionFeedbackLoop:
    """Tracks slippage drift, markouts, routing posture, and participation scaling."""

    db_path: Path = RESEARCH_VAULT_PATH
    slippage_window: deque[float] = field(
        default_factory=lambda: deque(maxlen=SLIPPAGE_FEEDBACK_WINDOW)
    )
    markout_window: deque[float] = field(
        default_factory=lambda: deque(maxlen=SLIPPAGE_FEEDBACK_WINDOW)
    )
    volume_tracks: dict[str, deque[float]] = field(default_factory=dict)

    def process_fill(self, fill_data: Mapping[str, Any]) -> SlippageFeedbackRecord:
        trade_id = str(fill_data.get("trade_id") or "")
        timestamp = _coerce_timestamp(fill_data.get("timestamp"))
        strategy_id = str(fill_data.get("strategy_id") or "")
        symbol = str(fill_data.get("symbol") or "").upper()
        side = _coerce_side(fill_data.get("side"))
        qty = float(fill_data.get("qty") or 0.0)
        expected_price = float(fill_data.get("expected_price") or 0.0)
        filled_price = float(fill_data.get("filled_price") or 0.0)
        expected_slippage_pct = float(fill_data.get("expected_slippage_pct") or 0.0)

        realized_slippage_pct = _realized_slippage_pct(
            expected_price,
            filled_price,
            side,
        )
        slippage_delta_pct = realized_slippage_pct - expected_slippage_pct
        self.slippage_window.append(realized_slippage_pct)

        session_type = resolve_trading_session(timestamp)
        size_bucket = classify_order_size_bucket(qty)

        record = SlippageFeedbackRecord(
            trade_id=trade_id,
            timestamp=timestamp,
            strategy_id=strategy_id,
            symbol=symbol,
            expected_slippage_pct=expected_slippage_pct,
            realized_slippage_pct=realized_slippage_pct,
            slippage_delta_pct=slippage_delta_pct,
            session_type=session_type,
            size_bucket=size_bucket,
        )
        self._persist_slippage_feedback(record, fill_data)
        return record

    def calculate_post_fill_markouts(
        self,
        fill_data: Mapping[str, Any],
        subsequent_bars: list[Mapping[str, Any]],
    ) -> StratifiedMarkoutBreakdown:
        timestamp = _coerce_timestamp(fill_data.get("timestamp"))
        side = _coerce_side(fill_data.get("side"))
        fill_price = float(fill_data.get("filled_price") or 0.0)
        qty = float(fill_data.get("qty") or 0.0)
        expected_price = float(fill_data.get("expected_price") or fill_price)
        session_type = resolve_trading_session(timestamp)
        size_bucket = classify_order_size_bucket(qty)

        bar_ts, bar_closes = _bars_to_arrays(subsequent_bars)
        fill_ts_ns = int(timestamp.timestamp() * 1_000_000_000)
        horizon_arr = np.asarray(MARKOUT_HORIZONS_MINUTES, dtype=np.int64)
        drift_prices = _markout_prices_at_horizons(
            fill_ts_ns,
            bar_ts,
            bar_closes,
            horizon_arr,
        )

        side_sign = 1.0 if side == Side.BUY else -1.0
        horizons: list[MarkoutHorizonResult] = []
        for horizon_min, drift_price in zip(MARKOUT_HORIZONS_MINUTES, drift_prices):
            if drift_price <= 0.0 or fill_price <= 0.0:
                markout_bps = 0.0
            else:
                raw_move = (drift_price - fill_price) / fill_price
                markout_bps = raw_move * side_sign * 10_000.0
            horizons.append(
                MarkoutHorizonResult(
                    horizon_minutes=int(horizon_min),
                    markout_bps=float(markout_bps),
                    reference_price=fill_price,
                    drift_price=float(drift_price),
                )
            )
            if horizon_min == 5:
                self.markout_window.append(markout_bps)

        implementation_shortfall_bps = 0.0
        if fill_price > 0.0:
            slip_bps = abs((fill_price - expected_price) / fill_price) * 10_000.0
            markout_5m = horizons[1].markout_bps if len(horizons) > 1 else 0.0
            implementation_shortfall_bps = slip_bps - markout_5m

        breakdown = StratifiedMarkoutBreakdown(
            session_type=session_type,
            size_bucket=size_bucket,
            horizons=tuple(horizons),
            implementation_shortfall_bps=float(implementation_shortfall_bps),
        )
        self._persist_markout_breakdown(fill_data, breakdown)
        return breakdown

    def determine_routing_urgency(
        self,
        urgency_context: Mapping[str, Any],
    ) -> RoutingUrgencyDecision:
        if bool(urgency_context.get("force_aggressive_ioc")):
            expected_edge_bps = float(urgency_context.get("expected_edge_bps") or 0.0)
            modeled_slippage_bps = float(urgency_context.get("modeled_slippage_bps") or 5.0)
            book_pressure = float(urgency_context.get("book_pressure") or 0.0)
            return RoutingUrgencyDecision(
                posture=RoutingPosture.BALANCED_IOC,
                aggressiveness=1.0,
                alpha_decay_bps=0.0,
                expected_edge_bps=expected_edge_bps,
                book_pressure=max(0.0, min(1.0, book_pressure)),
                reason="low_confidence_depth_defensive_ioc",
            )

        depth_confidence = str(urgency_context.get("depth_confidence") or DEPTH_CONFIDENCE_HIGH)
        expected_edge_bps = float(urgency_context.get("expected_edge_bps") or 0.0)
        book_pressure = float(urgency_context.get("book_pressure") or 0.5)
        book_pressure = max(0.0, min(1.0, book_pressure))

        alpha_decay_bps = self._alpha_decay_bps(urgency_context)
        override_decay = urgency_context.get("alpha_decay_bps")
        if override_decay is not None:
            alpha_decay_bps = float(override_decay)

        modeled_slippage_bps = float(urgency_context.get("modeled_slippage_bps") or 5.0)
        urgency_score = expected_edge_bps - alpha_decay_bps - modeled_slippage_bps

        if depth_confidence in {DEPTH_CONFIDENCE_LOW, DEPTH_CONFIDENCE_UNAVAILABLE}:
            return RoutingUrgencyDecision(
                posture=RoutingPosture.BALANCED_IOC,
                aggressiveness=1.0,
                alpha_decay_bps=alpha_decay_bps,
                expected_edge_bps=expected_edge_bps,
                book_pressure=book_pressure,
                reason="thin_or_missing_depth_defensive_ioc",
            )

        if (
            alpha_decay_bps >= ALPHA_DECAY_URGENCY_BPS
            and expected_edge_bps >= MIN_EXPECTED_EDGE_BPS
            and book_pressure <= BOOK_PRESSURE_AGGRESSIVE_THRESHOLD
        ):
            return RoutingUrgencyDecision(
                posture=RoutingPosture.AGGRESSIVE_TAKER,
                aggressiveness=1.0,
                alpha_decay_bps=alpha_decay_bps,
                expected_edge_bps=expected_edge_bps,
                book_pressure=book_pressure,
                reason="alpha_decay_with_edge",
            )

        if book_pressure >= BOOK_PRESSURE_PASSIVE_THRESHOLD or urgency_score < 0.0:
            aggressiveness = 0.5 if urgency_score >= 0.0 else 0.35
            return RoutingUrgencyDecision(
                posture=RoutingPosture.PASSIVE_NBBO,
                aggressiveness=aggressiveness,
                alpha_decay_bps=alpha_decay_bps,
                expected_edge_bps=expected_edge_bps,
                book_pressure=book_pressure,
                reason="book_pressure_or_negative_urgency",
            )

        return RoutingUrgencyDecision(
            posture=RoutingPosture.BALANCED_IOC,
            aggressiveness=0.75,
            alpha_decay_bps=alpha_decay_bps,
            expected_edge_bps=expected_edge_bps,
            book_pressure=book_pressure,
            reason="balanced_default",
        )

    def update_volume_track(self, symbol: str, bar_volume: float) -> None:
        key = symbol.upper()
        track = self.volume_tracks.setdefault(
            key,
            deque(maxlen=PARTICIPATION_VOL_LOOKBACK),
        )
        track.append(max(float(bar_volume), 0.0))

    def resolve_participation_cap(
        self,
        participation_context: Mapping[str, Any],
    ) -> ParticipationCapDecision:
        symbol = str(participation_context.get("symbol") or "").upper()
        base_cap_pct = float(participation_context.get("base_cap_pct") or 0.95)
        order_notional = float(participation_context.get("order_notional") or 0.0)
        thin_liquidity_active = bool(participation_context.get("thin_liquidity_active"))

        trailing = participation_context.get("trailing_volumes")
        if isinstance(trailing, (list, tuple, np.ndarray)) and len(trailing) > 0:
            volumes = np.asarray(trailing, dtype=np.float64)
        else:
            track = self.volume_tracks.get(symbol)
            volumes = (
                np.asarray(list(track), dtype=np.float64)
                if track and len(track) > 0
                else np.asarray([0.0], dtype=np.float64)
            )

        trailing_volume = float(volumes[-1]) if volumes.size else 0.0
        reference_volume = float(np.median(volumes)) if volumes.size else 0.0
        if reference_volume <= 0.0:
            reference_volume = max(trailing_volume, 1.0)

        vol_ratio = trailing_volume / reference_volume
        vol_impact_scalar = float(
            np.clip(
                vol_ratio,
                PARTICIPATION_VOL_FLOOR_MULT,
                PARTICIPATION_VOL_CEILING_MULT,
            )
        )

        adv_notional = reference_volume * float(
            participation_context.get("reference_price") or 1.0
        )
        size_pressure = 0.0
        if adv_notional > 0.0 and order_notional > 0.0:
            size_pressure = min(
                PARTICIPATION_SIZE_IMPACT_CEILING,
                order_notional / max(adv_notional * PARTICIPATION_ADV_NOTIONAL_FRACTION, 1e-9),
            )
        size_impact_scalar = 1.0 - size_pressure

        participation_cap_pct = base_cap_pct * vol_impact_scalar * size_impact_scalar
        if thin_liquidity_active:
            participation_cap_pct *= THIN_LIQUIDITY_PARTICIPATION_MULT

        return ParticipationCapDecision(
            participation_cap_pct=max(0.0, min(base_cap_pct, participation_cap_pct)),
            vol_impact_scalar=vol_impact_scalar,
            size_impact_scalar=size_impact_scalar,
            trailing_volume=trailing_volume,
            reference_volume=reference_volume,
            thin_liquidity_active=thin_liquidity_active,
        )

    def average_slippage_delta_bps(self) -> float:
        if not self.slippage_window:
            return 0.0
        return float(np.mean(list(self.slippage_window))) * 10_000.0

    def _alpha_decay_bps(self, urgency_context: Mapping[str, Any]) -> float:
        if self.markout_window:
            return max(0.0, -float(np.mean(list(self.markout_window))))
        return max(0.0, float(urgency_context.get("alpha_decay_bps") or 0.0))

    def _persist_slippage_feedback(
        self,
        record: SlippageFeedbackRecord,
        fill_data: Mapping[str, Any],
    ) -> None:
        ensure_execution_feedback_schema(self.db_path)
        metadata = dict(fill_data.get("metadata") or {})
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO execution_feedback_ledger (
                    trade_id, timestamp, strategy_id, symbol, side, qty,
                    expected_price, filled_price, expected_slippage_pct,
                    realized_slippage_pct, slippage_delta_pct, session_type,
                    size_bucket, routing_posture, participation_cap_pct,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.trade_id,
                    record.timestamp.isoformat(),
                    record.strategy_id,
                    record.symbol,
                    str(fill_data.get("side") or ""),
                    float(fill_data.get("qty") or 0.0),
                    float(fill_data.get("expected_price") or 0.0),
                    float(fill_data.get("filled_price") or 0.0),
                    record.expected_slippage_pct,
                    record.realized_slippage_pct,
                    record.slippage_delta_pct,
                    record.session_type,
                    record.size_bucket,
                    str(fill_data.get("routing_posture") or ROUTING_POSTURE_BALANCED_IOC),
                    float(fill_data.get("participation_cap_pct") or 0.0),
                    json.dumps(metadata),
                ),
            )

    def _persist_markout_breakdown(
        self,
        fill_data: Mapping[str, Any],
        breakdown: StratifiedMarkoutBreakdown,
    ) -> None:
        ensure_execution_feedback_schema(self.db_path)
        markouts = {h.horizon_minutes: h.markout_bps for h in breakdown.horizons}
        trade_id = str(fill_data.get("trade_id") or "")
        if not trade_id:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE execution_feedback_ledger
                SET markout_1m = ?, markout_5m = ?, markout_30m = ?
                WHERE trade_id = ?
                """,
                (
                    markouts.get(1),
                    markouts.get(5),
                    markouts.get(30),
                    trade_id,
                ),
            )


def ensure_execution_feedback_schema(db_path: Path = RESEARCH_VAULT_PATH) -> None:
    ensure_db_writable(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(EXECUTION_FEEDBACK_LEDGER_DDL)


def classify_order_size_bucket(qty: float) -> str:
    shares = abs(float(qty))
    if shares < 50.0:
        return ORDER_SIZE_BUCKET_MICRO
    if shares < 200.0:
        return ORDER_SIZE_BUCKET_SMALL
    if shares < 1000.0:
        return ORDER_SIZE_BUCKET_MEDIUM
    return ORDER_SIZE_BUCKET_LARGE


@dataclass(frozen=True)
class DepthRoutingEvaluation:
    book_pressure: float
    depth_confidence: str
    force_aggressive_ioc: bool
    bid_size: float
    ask_size: float
    spread_pct: float
    depth_source: str


def evaluate_level1_depth_routing(
    *,
    symbol: str,
    side: Side,
    stream_quote: Any | None = None,
    rest_snapshot: Any | None = None,
    thin_depth_threshold: float = THIN_LEVEL1_TOTAL_DEPTH,
    volatile_spread_pct: float = VOLATILE_SPREAD_PCT,
) -> DepthRoutingEvaluation:
    """
    Resolve book pressure from websocket-primary depth with REST fallback.

    Missing, stale, or thin depth forces a defensive immediate-or-cancel profile.
    """
    bid_price = 0.0
    ask_price = 0.0
    bid_size = 0.0
    ask_size = 0.0
    depth_source = "unavailable"
    depth_confidence = DEPTH_CONFIDENCE_UNAVAILABLE

    if stream_quote is not None and not stream_quote.is_stale():
        bid_price = float(stream_quote.bid_price)
        ask_price = float(stream_quote.ask_price)
        bid_size = float(stream_quote.bid_size)
        ask_size = float(stream_quote.ask_size)
        depth_source = str(stream_quote.source)
        depth_confidence = DEPTH_CONFIDENCE_HIGH
    elif rest_snapshot is not None:
        bid_price = float(getattr(rest_snapshot, "bid_price", 0.0) or 0.0)
        ask_price = float(getattr(rest_snapshot, "ask_price", 0.0) or 0.0)
        bid_size = float(getattr(rest_snapshot, "bid_size", 0.0) or 0.0)
        ask_size = float(getattr(rest_snapshot, "ask_size", 0.0) or 0.0)
        depth_source = str(getattr(rest_snapshot, "source", "rest") or "rest")
        if bid_price > 0.0 or ask_price > 0.0:
            depth_confidence = (
                DEPTH_CONFIDENCE_HIGH
                if bid_size > 0.0 or ask_size > 0.0
                else DEPTH_CONFIDENCE_LOW
            )

    mid_price = 0.0
    if bid_price > 0.0 and ask_price > 0.0:
        mid_price = (bid_price + ask_price) / 2.0
    else:
        mid_price = max(bid_price, ask_price, 0.0)

    spread_pct = 0.0
    if mid_price > 0.0:
        spread_pct = max(ask_price - bid_price, 0.0) / mid_price

    total_depth = max(bid_size, 0.0) + max(ask_size, 0.0)
    force_aggressive_ioc = False
    if depth_confidence == DEPTH_CONFIDENCE_UNAVAILABLE:
        force_aggressive_ioc = True
    elif total_depth <= 0.0:
        depth_confidence = DEPTH_CONFIDENCE_LOW
        force_aggressive_ioc = True
    elif total_depth < thin_depth_threshold or spread_pct >= volatile_spread_pct:
        depth_confidence = DEPTH_CONFIDENCE_LOW
        force_aggressive_ioc = True

    book_pressure = compute_book_pressure(
        bid_size=bid_size,
        ask_size=ask_size,
        spread_pct=spread_pct,
        side=side,
    )
    if force_aggressive_ioc:
        book_pressure = min(book_pressure, BOOK_PRESSURE_AGGRESSIVE_THRESHOLD)

    return DepthRoutingEvaluation(
        book_pressure=book_pressure,
        depth_confidence=depth_confidence,
        force_aggressive_ioc=force_aggressive_ioc,
        bid_size=bid_size,
        ask_size=ask_size,
        spread_pct=spread_pct,
        depth_source=depth_source,
    )


def compute_book_pressure(
    *,
    bid_size: float,
    ask_size: float,
    spread_pct: float,
    side: Side,
) -> float:
    """Localized order book pressure in [0, 1]; higher favors passive posting."""
    depth = max(bid_size, 0.0) + max(ask_size, 0.0)
    if depth <= 0.0:
        imbalance = 0.5
    elif side == Side.BUY:
        imbalance = ask_size / depth
    else:
        imbalance = bid_size / depth
    spread_component = min(1.0, max(0.0, spread_pct / 0.002))
    return max(0.0, min(1.0, 0.6 * imbalance + 0.4 * spread_component))


def _coerce_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_side(value: Any) -> Side:
    if isinstance(value, Side):
        return value
    token = str(value or "").lower()
    if token in {"buy", "long"}:
        return Side.BUY
    return Side.SELL


def _bars_to_arrays(
    subsequent_bars: list[Mapping[str, Any]],
) -> tuple[np.ndarray, np.ndarray]:
    if not subsequent_bars:
        return np.asarray([], dtype=np.int64), np.asarray([], dtype=np.float64)

    timestamps: list[int] = []
    closes: list[float] = []
    for bar in subsequent_bars:
        ts = _coerce_timestamp(bar.get("timestamp"))
        timestamps.append(int(ts.timestamp() * 1_000_000_000))
        closes.append(float(bar.get("close") or 0.0))
    return np.asarray(timestamps, dtype=np.int64), np.asarray(closes, dtype=np.float64)


@njit(cache=True)
def _markout_prices_at_horizons(
    fill_ts_ns: int,
    bar_ts_ns: np.ndarray,
    bar_closes: np.ndarray,
    horizon_minutes: np.ndarray,
) -> np.ndarray:
    n_horizons = horizon_minutes.shape[0]
    out = np.zeros(n_horizons, dtype=np.float64)
    if bar_ts_ns.size == 0 or bar_closes.size == 0:
        return out

    for i in range(n_horizons):
        target_ns = fill_ts_ns + int(horizon_minutes[i]) * 60 * 1_000_000_000
        best_idx = -1
        best_delta = 9_223_372_036_854_775_807
        for j in range(bar_ts_ns.shape[0]):
            delta = abs(bar_ts_ns[j] - target_ns)
            if delta < best_delta:
                best_delta = delta
                best_idx = j
        if best_idx >= 0:
            out[i] = bar_closes[best_idx]
    return out


@dataclass(frozen=True)
class BrokerPrimaryReconciliation:
    """Broker-authoritative leg state after a fill or partial fill."""

    broker_qty: float
    position_side: str
    bars_in_trade: int
    is_partial: bool
    requested_qty: float
    filled_qty: float
    residual_qty: float


def _signed_broker_qty(position: Position | None) -> float:
    if position is None:
        return 0.0
    qty = abs(float(position.qty))
    if str(position.side).lower() == "short":
        return -qty
    return qty


def _position_side_label(position: Position | None) -> str:
    if position is None:
        return "flat"
    return "long" if str(position.side).lower() == "long" else "short"


def reconcile_broker_primary_fill(
    *,
    requested_qty: float,
    fill_result: OrderResult,
    prior_position: Position | None,
    broker_position: Position | None,
    bars_in_trade: int,
) -> BrokerPrimaryReconciliation:
    """
    Reconcile leg memory to broker fill quantity (broker_qty is source of truth).

    Partial fills keep bars_in_trade coherent with the exchange-reported position.
    """
    filled_qty = max(float(fill_result.qty), 0.0)
    requested = max(float(requested_qty), 0.0)
    status = str(fill_result.status or "").lower()
    is_partial = status == "partially_filled" or (
        requested > 0.0 and filled_qty + 1e-9 < requested
    )
    residual_qty = max(requested - filled_qty, 0.0) if is_partial else 0.0
    broker_qty = _signed_broker_qty(broker_position)
    position_side = _position_side_label(broker_position)

    if broker_position is None:
        reconciled_bars = 0
    elif prior_position is None:
        reconciled_bars = 1
    else:
        reconciled_bars = max(int(bars_in_trade), 1)

    return BrokerPrimaryReconciliation(
        broker_qty=broker_qty,
        position_side=position_side,
        bars_in_trade=reconciled_bars,
        is_partial=is_partial,
        requested_qty=requested,
        filled_qty=filled_qty,
        residual_qty=residual_qty,
    )


def detect_leg_broker_memory_drift(
    *,
    bars_in_trade: int,
    broker_position: Position | None,
) -> bool:
    local_in_position = int(bars_in_trade) > 0
    broker_in_position = (
        broker_position is not None and abs(float(broker_position.qty)) > 1e-9
    )
    return local_in_position != broker_in_position


def apply_broker_authority_to_leg_memory(
    *,
    bars_in_trade: int,
    broker_position: Position | None,
    prior_position: Position | None,
) -> int:
    if broker_position is None:
        return 0
    if prior_position is None:
        return max(int(bars_in_trade), 1)
    return max(int(bars_in_trade), 1)
