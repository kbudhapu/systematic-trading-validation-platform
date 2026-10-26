"""Tests for exchange clock registry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.ingestor.exchange_clock import ExchangeClockRegistry
from src.models import Bar


def _bar(ts: datetime) -> Bar:
    return Bar(
        timestamp=ts,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1_000.0,
        symbol="QQQ",
    )


def test_exchange_clock_uses_bar_period_end_from_ingestion() -> None:
    registry = ExchangeClockRegistry()
    bar_ts = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)
    registry.note_rest_ingestion("QQQ", "15Min", [_bar(bar_ts)], asset_class="stock")
    reference = registry.reference_for("QQQ", "15Min", asset_class="stock")
    assert reference == bar_ts + timedelta(minutes=15)


def test_stream_bar_advances_reference() -> None:
    """MC-3: a WS stream bar is a ONE-MINUTE bar whatever the leg timeframe — the reference it
    contributes is its own close (open + 1min), NOT open + leg period (that was the ~2x-period
    freshness inflation: reference landed one leg period in the future)."""
    registry = ExchangeClockRegistry()
    first = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)
    second = datetime(2026, 6, 24, 15, 15, tzinfo=timezone.utc)
    registry.note_stream_bar("QQQ", "15Min", bar_timestamp=first, asset_class="stock")
    registry.note_stream_bar("QQQ", "15Min", bar_timestamp=second, asset_class="stock")
    reference = registry.reference_for("QQQ", "15Min", asset_class="stock")
    assert reference == second + timedelta(minutes=1)


def test_stream_bar_reference_never_a_leg_period_ahead() -> None:
    """The reference from a minute bar arriving 'now' must sit within ~1 minute of now — never a
    full leg period ahead — for BOTH leg timeframes (QQQ 15Min / BTC 1Hour)."""
    for timeframe in ("15Min", "1Hour"):
        registry = ExchangeClockRegistry()
        now_minute = datetime(2026, 7, 16, 17, 1, tzinfo=timezone.utc)
        registry.note_stream_bar("X", timeframe, bar_timestamp=now_minute, asset_class="stock")
        reference = registry.reference_for("X", timeframe, asset_class="stock")
        assert reference == now_minute + timedelta(minutes=1)
