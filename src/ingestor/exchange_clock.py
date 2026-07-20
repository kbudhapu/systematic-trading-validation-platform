"""
Authoritative exchange clock sourced from Alpaca market-data ingestion events.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from src.ingestor.alpaca import TIMEFRAME_MINUTES
from src.models import Bar

ClockSource = Literal["stream_bar", "stream_heartbeat", "rest_ingestion"]


@dataclass(frozen=True)
class ExchangeClockSnapshot:
    symbol: str
    timeframe: str
    exchange_reference_utc: datetime
    last_bar_period_end_utc: datetime | None
    source: ClockSource
    last_event_exchange_utc: datetime


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bar_period_end(bar: Bar, bar_minutes: int) -> datetime:
    return _coerce_utc(bar.timestamp) + timedelta(minutes=bar_minutes)


class ExchangeClockRegistry:
    """Thread-safe registry of exchange-derived reference times per symbol/timeframe."""

    __slots__ = ("_lock", "_snapshots")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: dict[tuple[str, str], ExchangeClockSnapshot] = {}

    def reference_for(
        self,
        symbol: str,
        timeframe: str,
        *,
        asset_class: str = "stock",
    ) -> datetime:
        key = (symbol.upper(), timeframe)
        with self._lock:
            snap = self._snapshots.get(key)
        if snap is not None:
            return snap.exchange_reference_utc
        raise KeyError(
            f"exchange clock not initialized for {symbol}/{timeframe}; "
            "ingest bars or start market-data stream first"
        )

    def snapshot_for(self, symbol: str, timeframe: str) -> ExchangeClockSnapshot | None:
        with self._lock:
            return self._snapshots.get((symbol.upper(), timeframe))

    def note_stream_bar(
        self,
        symbol: str,
        timeframe: str,
        *,
        bar_timestamp: datetime,
        asset_class: str = "stock",
    ) -> None:
        event_ts = _coerce_utc(bar_timestamp)
        # MC-3 (2026-07-16, verdict A): the WebSocket delivers ONE-MINUTE bars regardless of the
        # leg timeframe, so "exchange now" for a stream bar is its own close = open + 1 minute.
        # Adding the LEG period here (15Min/1Hour) pushed exchange_reference ~one full leg period
        # into the FUTURE, inflating every bar_freshness reading by ~one period on a HEALTHY feed
        # (the false bar_freshness_critical that held the soak degraded). Pinned by freshness_diag:
        # latest_bar_ts == window_newest_ts in every sample; reference ≈ wall + leg period.
        period_end = event_ts + timedelta(minutes=1)
        self._upsert(
            symbol=symbol,
            timeframe=timeframe,
            candidate_reference=period_end,
            period_end=period_end,
            source="stream_bar",
            event_exchange_utc=event_ts,
        )

    def note_stream_heartbeat(
        self,
        symbol: str,
        timeframe: str,
        *,
        exchange_timestamp: datetime,
        asset_class: str = "stock",
    ) -> None:
        event_ts = _coerce_utc(exchange_timestamp)
        self._upsert(
            symbol=symbol,
            timeframe=timeframe,
            candidate_reference=event_ts,
            period_end=None,
            source="stream_heartbeat",
            event_exchange_utc=event_ts,
        )

    def note_rest_ingestion(
        self,
        symbol: str,
        timeframe: str,
        bars: list[Bar],
        *,
        asset_class: str = "stock",
    ) -> None:
        if not bars:
            return
        bar_minutes = TIMEFRAME_MINUTES.get(timeframe, 15)
        latest = max(bars, key=lambda bar: _coerce_utc(bar.timestamp))
        event_ts = _coerce_utc(latest.timestamp)
        period_end = _bar_period_end(latest, bar_minutes)
        self._upsert(
            symbol=symbol,
            timeframe=timeframe,
            candidate_reference=period_end,
            period_end=period_end,
            source="rest_ingestion",
            event_exchange_utc=event_ts,
        )

    def _upsert(
        self,
        *,
        symbol: str,
        timeframe: str,
        candidate_reference: datetime,
        period_end: datetime | None,
        source: ClockSource,
        event_exchange_utc: datetime,
    ) -> None:
        key = (symbol.upper(), timeframe)
        candidate = _coerce_utc(candidate_reference)
        with self._lock:
            prior = self._snapshots.get(key)
            reference = candidate
            if prior is not None and prior.exchange_reference_utc > reference:
                reference = prior.exchange_reference_utc
            resolved_period_end = period_end
            if resolved_period_end is None and prior is not None:
                resolved_period_end = prior.last_bar_period_end_utc
            self._snapshots[key] = ExchangeClockSnapshot(
                symbol=symbol.upper(),
                timeframe=timeframe,
                exchange_reference_utc=reference,
                last_bar_period_end_utc=resolved_period_end,
                source=source,
                last_event_exchange_utc=_coerce_utc(event_exchange_utc),
            )


_registry: ExchangeClockRegistry | None = None
_registry_lock = threading.Lock()


def get_exchange_clock_registry() -> ExchangeClockRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = ExchangeClockRegistry()
        return _registry
