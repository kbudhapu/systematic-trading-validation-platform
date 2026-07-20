"""
Thread-safe level-1 quote depth cache fed by the live market-data websocket stream.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

log = structlog.get_logger()

DEPTH_STALE_SECONDS = 5.0


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Level1DepthQuote:
    symbol: str
    bid_price: float
    ask_price: float
    bid_size: float
    ask_size: float
    timestamp: datetime
    source: str = "stream"

    @property
    def mid_price(self) -> float:
        if self.bid_price > 0.0 and self.ask_price > 0.0:
            return (self.bid_price + self.ask_price) / 2.0
        return max(self.bid_price, self.ask_price, 0.0)

    @property
    def spread_pct(self) -> float:
        mid = self.mid_price
        if mid <= 0.0:
            return 0.0
        return max(self.ask_price - self.bid_price, 0.0) / mid

    @property
    def total_depth(self) -> float:
        return max(self.bid_size, 0.0) + max(self.ask_size, 0.0)

    def age_seconds(self, *, now: datetime | None = None) -> float:
        reference = _coerce_utc(now or datetime.now(timezone.utc))
        return max((reference - _coerce_utc(self.timestamp)).total_seconds(), 0.0)

    def is_stale(self, *, max_age_seconds: float = DEPTH_STALE_SECONDS) -> bool:
        return self.age_seconds() > max(max_age_seconds, 0.0)


class Level1DepthCache:
    """Latest websocket quote per symbol for execution book-pressure routing."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._quotes: dict[str, Level1DepthQuote] = {}

    def update(
        self,
        symbol: str,
        *,
        bid_price: float,
        ask_price: float,
        bid_size: float,
        ask_size: float,
        timestamp: datetime,
        source: str = "stream",
    ) -> Level1DepthQuote:
        quote = Level1DepthQuote(
            symbol=symbol.upper(),
            bid_price=max(float(bid_price), 0.0),
            ask_price=max(float(ask_price), 0.0),
            bid_size=max(float(bid_size), 0.0),
            ask_size=max(float(ask_size), 0.0),
            timestamp=_coerce_utc(timestamp),
            source=source,
        )
        with self._lock:
            self._quotes[quote.symbol] = quote
        return quote

    def get(self, symbol: str) -> Level1DepthQuote | None:
        with self._lock:
            return self._quotes.get(symbol.upper())

    def snapshot(self, symbol: str) -> Level1DepthQuote | None:
        with self._lock:
            quote = self._quotes.get(symbol.upper())
            return quote


_cache: Level1DepthCache | None = None
_cache_lock = threading.Lock()


def get_level1_depth_cache() -> Level1DepthCache:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = Level1DepthCache()
        return _cache


def reset_level1_depth_cache() -> None:
    """Clear cached quotes (used in tests)."""
    global _cache
    with _cache_lock:
        if _cache is not None:
            with _cache._lock:
                _cache._quotes.clear()
