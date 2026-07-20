"""Tests for ingestor helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from src.ingestor.alpaca import AlpacaDataIngestor
from src.models import Bar


def _bar(ts_minute: int) -> Bar:
    return Bar(
        timestamp=datetime(2024, 1, 1, 10, ts_minute, tzinfo=timezone.utc),
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=1.0,
        symbol="SPY",
    )


def test_filter_new_bars():
    bars = [_bar(i) for i in range(5)]
    after = datetime(2024, 1, 1, 10, 2, tzinfo=timezone.utc)
    new = AlpacaDataIngestor.filter_new_bars(bars, after)
    assert len(new) == 2
    assert new[0].timestamp.minute == 3
