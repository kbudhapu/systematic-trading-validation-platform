"""Depth router — dynamic liquidity thresholds and IOC residual handling."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.engine.depth_router import (
    DepthRouter,
    estimate_adv_shares,
    reset_depth_router,
    scale_liquidity_thresholds,
)
from src.engine.execution_adaptor import DEPTH_CONFIDENCE_LOW
from src.ingestor.level1_depth_cache import Level1DepthQuote, reset_level1_depth_cache
from src.models import OrderResult, Side


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_depth_router()
    reset_level1_depth_cache()


def test_scale_liquidity_thresholds_tightens_for_low_adv() -> None:
    high_adv_depth, high_adv_spread = scale_liquidity_thresholds(
        20_000_000.0, 0.01, 50.0, 0.015
    )
    low_adv_depth, low_adv_spread = scale_liquidity_thresholds(
        500_000.0, 0.01, 50.0, 0.015
    )
    assert low_adv_depth < high_adv_depth
    assert low_adv_spread < high_adv_spread


def test_scale_liquidity_thresholds_tightens_for_high_volatility() -> None:
    calm_depth, calm_spread = scale_liquidity_thresholds(
        10_000_000.0, 0.01, 50.0, 0.015
    )
    volatile_depth, volatile_spread = scale_liquidity_thresholds(
        10_000_000.0, 0.05, 50.0, 0.015
    )
    assert volatile_depth < calm_depth
    assert volatile_spread < calm_spread


def test_depth_router_flags_liquidity_constrained_on_thin_book() -> None:
    router = DepthRouter()
    quote = Level1DepthQuote(
        symbol="QQQ",
        bid_price=100.0,
        ask_price=100.5,
        bid_size=5.0,
        ask_size=5.0,
        timestamp=datetime.now(timezone.utc),
        source="stream",
    )
    result = router.evaluate_routing(
        symbol="QQQ",
        side=Side.BUY,
        stream_quote=quote,
        rest_snapshot=None,
        adv_shares=500_000.0,
        atr=2.0,
        reference_price=100.0,
        asset_class="stock",
    )
    assert result.liquidity_constrained is True
    assert result.evaluation.force_aggressive_ioc is True
    assert result.evaluation.depth_confidence == DEPTH_CONFIDENCE_LOW
    assert result.thresholds.thin_book_depth_shares < 50.0


def test_register_defensive_ioc_partial_tracks_residual() -> None:
    router = DepthRouter()
    result = OrderResult(
        symbol="QQQ",
        side=Side.BUY,
        qty=4.0,
        filled_price=100.1,
        filled_at=datetime.now(timezone.utc),
        status="partially_filled",
    )
    adj = router.register_defensive_ioc_partial(
        strategy_id="leg_a",
        symbol="QQQ",
        requested_qty=10.0,
        result=result,
        defensive_ioc=True,
    )
    assert adj is not None
    assert adj.residual_qty == pytest.approx(6.0)
    assert adj.entry_scale_factor == pytest.approx(0.4)
    assert router.entry_scale_for_strategy("leg_a") == pytest.approx(0.4)
    assert router.residual_qty_for_strategy("leg_a") == pytest.approx(6.0)


def test_estimate_adv_shares_from_window_volumes() -> None:
    volumes = np.array([1_000.0] * 20)
    adv = estimate_adv_shares(
        volumes,
        timeframe="15Min",
        asset_class="stock",
    )
    assert adv > 0.0


def test_crypto_depth_rest_worker_updates_cache() -> None:
    from src.engine.depth_router import CryptoDepthRestWorker
    from src.ingestor.level1_depth_cache import get_level1_depth_cache

    broker = MagicMock()
    broker._get_crypto_nbbo_snapshot_sync = MagicMock(
        return_value=MagicMock(
            bid_price=50_000.0,
            ask_price=50_010.0,
            bid_size=1.5,
            ask_size=2.0,
        )
    )
    worker = CryptoDepthRestWorker(poll_interval_seconds=0.05)

    async def _run() -> None:
        await worker.sync_symbols(("BTC/USD",), quote_fetcher=broker)
        await asyncio.sleep(0.12)
        await worker.stop()

    asyncio.run(_run())
    quote = get_level1_depth_cache().get("BTC/USD")
    assert quote is not None
    assert quote.bid_price == 50_000.0
    assert quote.source == "rest_crypto"
