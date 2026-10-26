"""
Alpaca market data ingestion — stocks and crypto.

Live path:  async REST → list[Bar] → deque RollingWindow
Batch path: async REST → Polars DataFrame → Parquet cache (backtest only)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import polars as pl
import structlog
from alpaca.data.enums import DataFeed
from src.ingestor.market_data_feed import soak_market_data_feed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from src.config import PARQUET_DIR
from src.core.rolling_window import RollingWindow
from src.ingestor.assets import crypto_data_symbol, infer_asset_class
from src.models import Bar

log = structlog.get_logger()

TIMEFRAME_MAP: dict[str, TimeFrame] = {
    "1Min": TimeFrame(1, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
    "4Hour": TimeFrame(4, TimeFrameUnit.Hour),
    "1Day": TimeFrame(1, TimeFrameUnit.Day),
}

TIMEFRAME_MINUTES: dict[str, int] = {
    "1Min": 1,
    "15Min": 15,
    "1Hour": 60,
    "4Hour": 240,
    "1Day": 1440,
}

RTH_MINUTES_PER_DAY = int(6.5 * 60)


def _history_start(
    timeframe: str, lookback_bars: int, asset_class: str
) -> datetime:
    """Calendar-aware start time so equities have enough RTH bars."""
    end = datetime.now(timezone.utc)
    bar_minutes = TIMEFRAME_MINUTES.get(timeframe, 15)

    if asset_class == "crypto":
        return end - timedelta(minutes=bar_minutes * (lookback_bars + 10))

    trading_days = max(
        5,
        int(lookback_bars * bar_minutes / RTH_MINUTES_PER_DAY * 1.6) + 5,
    )
    return end - timedelta(days=trading_days)


class AlpacaDataIngestor:
    """
    Async Alpaca OHLCV ingestor for equities and crypto.

    Network I/O runs in a thread pool (asyncio.to_thread) so the event loop
    is never blocked by the synchronous alpaca-py REST client.
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        self._stock = StockHistoricalDataClient(api_key, secret_key)
        self._crypto = CryptoHistoricalDataClient(api_key, secret_key)

    def _fetch_stock_bars_sync(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        tf = TIMEFRAME_MAP.get(timeframe)
        if tf is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
            # N1: the ONE soak feed (SIP), resolved from the single source of truth so this REST
            # shadow/bootstrap source of the CLOSED signal bars cannot silently diverge from the
            # websocket and quote paths. Never the alpaca-py default (IEX).
            feed=soak_market_data_feed(),
        )
        response = self._stock.get_stock_bars(request)
        raw = response.data.get(symbol, [])
        return [_bar_from_alpaca(bar, symbol) for bar in raw]

    def _fetch_crypto_bars_sync(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        tf = TIMEFRAME_MAP.get(timeframe)
        if tf is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        data_symbol = crypto_data_symbol(symbol)
        request = CryptoBarsRequest(
            symbol_or_symbols=data_symbol,
            timeframe=tf,
            start=start,
            end=end,
        )
        response = self._crypto.get_crypto_bars(request)
        raw = response.data.get(data_symbol, [])
        return [_bar_from_alpaca(bar, data_symbol) for bar in raw]

    def _fetch_bars_sync(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        asset_class: str,
    ) -> list[Bar]:
        if asset_class == "crypto":
            return self._fetch_crypto_bars_sync(symbol, timeframe, start, end)
        return self._fetch_stock_bars_sync(symbol, timeframe, start, end)

    async def fetch_latest_bars(
        self,
        symbol: str,
        timeframe: str,
        lookback_bars: int,
        asset_class: str | None = None,
    ) -> list[Bar]:
        """
        Fetch recent bars for the live loop.

        Returns bars in chronological order, suitable for RollingWindow.append().
        """
        asset = asset_class or infer_asset_class(symbol)
        end = datetime.now(timezone.utc)
        start = _history_start(timeframe, lookback_bars, asset)

        bars = await asyncio.to_thread(
            self._fetch_bars_sync, symbol, timeframe, start, end, asset
        )
        log.info("bars_ingested", symbol=symbol, count=len(bars), asset_class=asset)
        return bars

    async def fetch_historical(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        use_cache: bool = True,
        asset_class: str | None = None,
    ) -> pl.DataFrame:
        """
        Load historical bars as a Polars DataFrame for backtest batch analysis.

        Never called from live event handlers — Polars stays off the hot path.
        """
        asset = asset_class or infer_asset_class(symbol)
        cache_path = PARQUET_DIR / f"{symbol.replace('/', '_')}_{timeframe}.parquet"
        PARQUET_DIR.mkdir(parents=True, exist_ok=True)

        if use_cache and cache_path.exists():
            cached = pl.read_parquet(cache_path)
            if not cached.is_empty():
                log.info("bars_loaded_from_cache", symbol=symbol, rows=len(cached))
                return cached

        bars = await asyncio.to_thread(
            self._fetch_bars_sync, symbol, timeframe, start, end, asset
        )
        if not bars:
            log.warning("no_bars_returned", symbol=symbol)
            return pl.DataFrame()

        df = pl.DataFrame(
            {
                "timestamp": [b.timestamp for b in bars],
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
                "symbol": [b.symbol for b in bars],
            }
        ).sort("timestamp")

        df.write_parquet(cache_path)
        log.info("bars_fetched", symbol=symbol, rows=len(df))
        return df

    @staticmethod
    def bars_to_window(bars: list[Bar], window: RollingWindow) -> int:
        count = 0
        for bar in bars:
            if window.append(bar):
                count += 1
        return count

    @staticmethod
    def filter_new_bars(
        bars: list[Bar],
        after: datetime | None,
    ) -> list[Bar]:
        if after is None:
            return bars
        return [b for b in bars if b.timestamp > after]


def _bar_from_alpaca(bar, symbol: str) -> Bar:
    return Bar(
        timestamp=bar.timestamp,
        open=float(bar.open),
        high=float(bar.high),
        low=float(bar.low),
        close=float(bar.close),
        volume=float(bar.volume),
        symbol=symbol,
    )
