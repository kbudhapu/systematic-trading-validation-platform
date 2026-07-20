"""Microstructure guard — halts, spread cutoff, and stale quote detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.engine.microstructure_guard import (
    MicrostructureGuard,
    MicrostructureGuardConfig,
    reset_microstructure_guard,
)
from src.ingestor.level1_depth_cache import (
    Level1DepthCache,
    Level1DepthQuote,
    reset_level1_depth_cache,
)


@pytest.fixture(autouse=True)
def _reset_guards() -> None:
    reset_microstructure_guard()
    reset_level1_depth_cache()


def _fresh_quote(
    symbol: str = "QQQ",
    *,
    bid: float = 100.0,
    ask: float = 100.05,
    age_seconds: float = 0.0,
) -> Level1DepthQuote:
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return Level1DepthQuote(
        symbol=symbol,
        bid_price=bid,
        ask_price=ask,
        bid_size=500.0,
        ask_size=500.0,
        timestamp=ts,
        source="stream",
    )


def test_spread_cutoff_blocks_entries() -> None:
    cache = Level1DepthCache()
    guard = MicrostructureGuard(
        MicrostructureGuardConfig(max_allowed_spread_pct=0.015),
        depth_cache=cache,
    )
    cache.update(
        "QQQ",
        bid_price=100.0,
        ask_price=102.5,
        bid_size=100.0,
        ask_size=100.0,
        timestamp=datetime.now(timezone.utc),
    )
    guard.note_quote(
        "QQQ",
        bid_price=100.0,
        ask_price=102.5,
        timestamp=datetime.now(timezone.utc),
    )
    verdict = guard.evaluate("QQQ")
    assert verdict.blocks_entries is True
    assert verdict.realized_spread_pct > 0.015
    assert verdict.reason == "spread_exceeded"


def test_regulatory_halt_blocks_entries() -> None:
    guard = MicrostructureGuard()
    guard.note_trading_status(
        "QQQ",
        status_code="H",
        status_message="Trading Halted",
        reason_code="T1",
        reason_message="News Pending",
        timestamp=datetime.now(timezone.utc),
    )
    guard.note_quote(
        "QQQ",
        bid_price=100.0,
        ask_price=100.01,
        timestamp=datetime.now(timezone.utc),
    )
    verdict = guard.evaluate("QQQ")
    assert verdict.blocks_entries is True
    assert verdict.halt_status is True
    assert verdict.reason == "regulatory_halt"


def test_stale_quote_with_fresh_bar_blocks_entries() -> None:
    guard = MicrostructureGuard(
        MicrostructureGuardConfig(
            max_quote_stale_seconds=5.0,
            fresh_bar_window_seconds=120.0,
        )
    )
    now = datetime.now(timezone.utc)
    guard.note_stream_bar("QQQ", bar_timestamp=now)
    guard.note_quote(
        "QQQ",
        bid_price=100.0,
        ask_price=100.01,
        timestamp=now - timedelta(seconds=8.0),
    )
    verdict = guard.evaluate("QQQ", now=now)
    assert verdict.blocks_entries is True
    assert verdict.stale_quote_detected is True
    assert verdict.reason == "stale_quote"


def test_luld_status_blocks_entries() -> None:
    guard = MicrostructureGuard()
    guard.note_trading_status(
        "QQQ",
        status_code="LULD",
        status_message="Limit Up Limit Down",
        reason_code="LU",
        reason_message="Band engaged",
        timestamp=datetime.now(timezone.utc),
    )
    guard.note_quote(
        "QQQ",
        bid_price=100.0,
        ask_price=100.01,
        timestamp=datetime.now(timezone.utc),
    )
    verdict = guard.evaluate("QQQ")
    assert verdict.blocks_entries is True
    assert verdict.luld_active is True
    assert verdict.reason == "luld_band"


def test_resume_status_clears_halt() -> None:
    guard = MicrostructureGuard()
    guard.note_trading_status(
        "QQQ",
        status_code="H",
        status_message="Trading Halted",
        reason_code="T1",
        reason_message="News Pending",
        timestamp=datetime.now(timezone.utc),
    )
    guard.note_trading_status(
        "QQQ",
        status_code="ACTIVE",
        status_message="Trading Resumed",
        reason_code="",
        reason_message="",
        timestamp=datetime.now(timezone.utc),
    )
    guard.note_quote(
        "QQQ",
        bid_price=100.0,
        ask_price=100.01,
        timestamp=datetime.now(timezone.utc),
    )
    verdict = guard.evaluate("QQQ")
    assert verdict.blocks_entries is False
    assert verdict.halt_status is False


def test_tight_spread_and_fresh_quote_allows_entries() -> None:
    cache = Level1DepthCache()
    guard = MicrostructureGuard(depth_cache=cache)
    now = datetime.now(timezone.utc)
    quote = _fresh_quote()
    cache.update(
        "QQQ",
        bid_price=quote.bid_price,
        ask_price=quote.ask_price,
        bid_size=quote.bid_size,
        ask_size=quote.ask_size,
        timestamp=quote.timestamp,
    )
    guard.note_stream_bar("QQQ", bar_timestamp=now)
    guard.note_quote(
        "QQQ",
        bid_price=quote.bid_price,
        ask_price=quote.ask_price,
        timestamp=quote.timestamp,
    )
    verdict = guard.evaluate("QQQ", now=now)
    assert verdict.blocks_entries is False
    assert verdict.safe_for_entries is True
