"""
Symbol-aware liquidity depth routing with dynamic ADV/volatility-scaled thresholds.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import numpy as np
import structlog
from numba import njit

from src.engine.execution_adaptor import (
    DEPTH_CONFIDENCE_UNAVAILABLE,
    DepthRoutingEvaluation,
    evaluate_level1_depth_routing,
)
from src.ingestor.alpaca import RTH_MINUTES_PER_DAY, TIMEFRAME_MINUTES
from src.ingestor.level1_depth_cache import get_level1_depth_cache
from src.models import OrderResult, Side

log = structlog.get_logger()

BASE_THIN_BOOK_DEPTH_SHARES = 50.0
BASE_VOLATILE_SPREAD_PCT = 0.015
ADV_REFERENCE_SHARES = 10_000_000.0
CRYPTO_DEPTH_REST_POLL_SECONDS = 2.0
MIN_DEPTH_CUTOFF_SHARES = 5.0
MIN_SPREAD_TOLERANCE_PCT = 0.002


@njit
def scale_liquidity_thresholds(
    adv_shares: float,
    atr_pct: float,
    base_depth: float,
    base_spread: float,
) -> tuple[float, float]:
    adv_reference = 10_000_000.0
    adv_ratio = min(max(adv_shares / adv_reference, 0.05), 1.0)
    vol_ratio = min(max(atr_pct / 0.02, 0.5), 3.0)
    depth_cutoff = base_depth * adv_ratio / vol_ratio
    spread_tol = base_spread * max(adv_ratio, 0.25) / vol_ratio
    if depth_cutoff < MIN_DEPTH_CUTOFF_SHARES:
        depth_cutoff = MIN_DEPTH_CUTOFF_SHARES
    if spread_tol < MIN_SPREAD_TOLERANCE_PCT:
        spread_tol = MIN_SPREAD_TOLERANCE_PCT
    return depth_cutoff, spread_tol


@dataclass(frozen=True)
class LiquidityThresholds:
    adv_shares: float
    atr_pct: float
    thin_book_depth_shares: float
    spread_tolerance_pct: float
    volatility_scalar: float
    adv_scalar: float


@dataclass(frozen=True)
class DepthRoutingResult:
    evaluation: DepthRoutingEvaluation
    thresholds: LiquidityThresholds
    total_book_depth: float
    liquidity_constrained: bool


@dataclass(frozen=True)
class IocResidualAdjustment:
    strategy_id: str
    symbol: str
    requested_qty: float
    filled_qty: float
    residual_qty: float
    entry_scale_factor: float


@dataclass
class _IocResidualState:
    residual_qty: float
    entry_scale_factor: float
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class CryptoQuoteFetcher(Protocol):
    def _get_crypto_nbbo_snapshot_sync(self, symbol: str) -> Any | None: ...


def estimate_adv_shares(
    volumes: np.ndarray,
    *,
    timeframe: str,
    asset_class: str,
    lookback: int = 20,
) -> float:
    if volumes.size == 0:
        return ADV_REFERENCE_SHARES * 0.1
    tail = volumes[-min(int(lookback), volumes.size) :]
    mean_bar_volume = float(np.mean(tail))
    bar_minutes = TIMEFRAME_MINUTES.get(timeframe, 15)
    if asset_class == "crypto":
        bars_per_day = (24.0 * 60.0) / float(bar_minutes)
    else:
        bars_per_day = float(RTH_MINUTES_PER_DAY) / float(bar_minutes)
    return max(mean_bar_volume * bars_per_day, 1.0)


class DepthRouter:
    """Dynamic per-symbol liquidity thresholds and defensive IOC residual tracking."""

    def __init__(
        self,
        *,
        base_thin_depth: float = BASE_THIN_BOOK_DEPTH_SHARES,
        base_spread_pct: float = BASE_VOLATILE_SPREAD_PCT,
    ) -> None:
        self._base_thin_depth = float(base_thin_depth)
        self._base_spread_pct = float(base_spread_pct)
        self._lock = threading.RLock()
        self._ioc_residual_by_strategy: dict[str, _IocResidualState] = {}

    def compute_thresholds(
        self,
        *,
        adv_shares: float,
        atr: float,
        reference_price: float,
        asset_class: str = "stock",
    ) -> LiquidityThresholds:
        price = max(float(reference_price), 1e-9)
        atr_pct = max(float(atr), 0.0) / price
        if asset_class == "crypto":
            adv_shares = max(adv_shares, ADV_REFERENCE_SHARES * 0.01)
        depth_cutoff, spread_tol = scale_liquidity_thresholds(
            float(adv_shares),
            atr_pct,
            self._base_thin_depth,
            self._base_spread_pct,
        )
        adv_ratio = min(max(adv_shares / ADV_REFERENCE_SHARES, 0.05), 1.0)
        vol_ratio = min(max(atr_pct / 0.02, 0.5), 3.0)
        return LiquidityThresholds(
            adv_shares=float(adv_shares),
            atr_pct=atr_pct,
            thin_book_depth_shares=depth_cutoff,
            spread_tolerance_pct=spread_tol,
            volatility_scalar=vol_ratio,
            adv_scalar=adv_ratio,
        )

    def evaluate_routing(
        self,
        *,
        symbol: str,
        side: Side,
        stream_quote: Any | None,
        rest_snapshot: Any | None,
        adv_shares: float,
        atr: float,
        reference_price: float,
        asset_class: str = "stock",
    ) -> DepthRoutingResult:
        thresholds = self.compute_thresholds(
            adv_shares=adv_shares,
            atr=atr,
            reference_price=reference_price,
            asset_class=asset_class,
        )
        evaluation = evaluate_level1_depth_routing(
            symbol=symbol,
            side=side,
            stream_quote=stream_quote,
            rest_snapshot=rest_snapshot,
            thin_depth_threshold=thresholds.thin_book_depth_shares,
            volatile_spread_pct=thresholds.spread_tolerance_pct,
        )
        total_depth = max(evaluation.bid_size, 0.0) + max(evaluation.ask_size, 0.0)
        liquidity_constrained = (
            evaluation.force_aggressive_ioc
            or evaluation.depth_confidence == DEPTH_CONFIDENCE_UNAVAILABLE
        )
        if liquidity_constrained:
            log.warning(
                "depth_router: LIQUIDITY_CONSTRAINED",
                symbol=symbol.upper(),
                side=side.value,
                volatility_adjusted_depth_threshold=round(
                    thresholds.thin_book_depth_shares, 4
                ),
                volatility_adjusted_spread_threshold=round(
                    thresholds.spread_tolerance_pct, 6
                ),
                current_book_depth=round(total_depth, 4),
                realized_spread_pct=round(evaluation.spread_pct, 6),
                depth_confidence=evaluation.depth_confidence,
                depth_source=evaluation.depth_source,
                adv_shares=round(thresholds.adv_shares, 2),
                atr_pct=round(thresholds.atr_pct, 6),
            )
        return DepthRoutingResult(
            evaluation=evaluation,
            thresholds=thresholds,
            total_book_depth=total_depth,
            liquidity_constrained=liquidity_constrained,
        )

    def register_defensive_ioc_partial(
        self,
        *,
        strategy_id: str,
        symbol: str,
        requested_qty: float,
        result: OrderResult,
        defensive_ioc: bool,
    ) -> IocResidualAdjustment | None:
        if not defensive_ioc:
            return None
        requested = max(float(requested_qty), 0.0)
        filled = max(float(result.qty), 0.0)
        status = str(result.status or "").lower()
        is_partial = status == "partially_filled" or (
            requested > 0.0 and filled + 1e-9 < requested
        )
        if not is_partial:
            return None
        residual = max(requested - filled, 0.0)
        if residual <= 1e-9:
            return None
        entry_scale = min(max(filled / requested, 0.0), 1.0)
        with self._lock:
            self._ioc_residual_by_strategy[strategy_id] = _IocResidualState(
                residual_qty=residual,
                entry_scale_factor=entry_scale,
            )
        log.warning(
            "depth_router_ioc_partial_residual",
            strategy_id=strategy_id,
            symbol=symbol.upper(),
            requested_qty=requested,
            filled_qty=filled,
            residual_qty=residual,
            entry_scale_factor=entry_scale,
        )
        return IocResidualAdjustment(
            strategy_id=strategy_id,
            symbol=symbol.upper(),
            requested_qty=requested,
            filled_qty=filled,
            residual_qty=residual,
            entry_scale_factor=entry_scale,
        )

    def entry_scale_for_strategy(self, strategy_id: str) -> float:
        with self._lock:
            state = self._ioc_residual_by_strategy.get(strategy_id)
            if state is None:
                return 1.0
            return float(state.entry_scale_factor)

    def residual_qty_for_strategy(self, strategy_id: str) -> float:
        with self._lock:
            state = self._ioc_residual_by_strategy.get(strategy_id)
            if state is None:
                return 0.0
            return float(state.residual_qty)

    def clear_ioc_residual(self, strategy_id: str) -> None:
        with self._lock:
            self._ioc_residual_by_strategy.pop(strategy_id, None)

    def telemetry_snapshot(self) -> dict[str, Any]:
        with self._lock:
            residuals = {
                strategy_id: {
                    "residual_qty": state.residual_qty,
                    "entry_scale_factor": state.entry_scale_factor,
                }
                for strategy_id, state in self._ioc_residual_by_strategy.items()
            }
        return {
            "depth_router_ioc_residuals": residuals,
            "base_thin_book_depth_shares": self._base_thin_depth,
            "base_volatile_spread_pct": self._base_spread_pct,
        }


class CryptoDepthRestWorker:
    """Background REST quote poller for crypto symbols without websocket L1."""

    def __init__(self, poll_interval_seconds: float = CRYPTO_DEPTH_REST_POLL_SECONDS) -> None:
        self._poll_interval = max(float(poll_interval_seconds), 0.5)
        self._symbols: tuple[str, ...] = ()
        self._quote_fetcher: CryptoQuoteFetcher | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def sync_symbols(
        self,
        symbols: tuple[str, ...],
        *,
        quote_fetcher: CryptoQuoteFetcher,
    ) -> None:
        normalized = tuple(dict.fromkeys(s.upper() for s in symbols))
        if normalized == self._symbols and self._task is not None and not self._task.done():
            self._quote_fetcher = quote_fetcher
            return
        await self.stop()
        self._symbols = normalized
        self._quote_fetcher = quote_fetcher
        if not self._symbols:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._poll_loop())
        log.info(
            "crypto_depth_rest_worker_started",
            symbols=list(self._symbols),
            poll_interval_seconds=self._poll_interval,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._symbols = ()

    async def _poll_loop(self) -> None:
        cache = get_level1_depth_cache()
        while not self._stop.is_set():
            fetcher = self._quote_fetcher
            if fetcher is not None:
                for symbol in self._symbols:
                    try:
                        snapshot = await asyncio.to_thread(
                            fetcher._get_crypto_nbbo_snapshot_sync,
                            symbol,
                        )
                    except Exception as exc:
                        log.warning(
                            "crypto_depth_rest_poll_failed",
                            symbol=symbol,
                            error=str(exc),
                        )
                        continue
                    if snapshot is None:
                        continue
                    bid = float(getattr(snapshot, "bid_price", 0.0) or 0.0)
                    ask = float(getattr(snapshot, "ask_price", 0.0) or 0.0)
                    if bid <= 0.0 and ask <= 0.0:
                        continue
                    cache.update(
                        symbol,
                        bid_price=bid,
                        ask_price=ask,
                        bid_size=float(getattr(snapshot, "bid_size", 0.0) or 0.0),
                        ask_size=float(getattr(snapshot, "ask_size", 0.0) or 0.0),
                        timestamp=datetime.now(timezone.utc),
                        source="rest_crypto",
                    )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                continue


_router: DepthRouter | None = None
_router_lock = threading.Lock()


def get_depth_router() -> DepthRouter:
    global _router
    with _router_lock:
        if _router is None:
            _router = DepthRouter()
        return _router


def reset_depth_router() -> None:
    global _router
    with _router_lock:
        _router = None
