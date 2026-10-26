"""Data ingestion protocol — async network boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import polars as pl

from src.models import Bar


class DataIngestor(Protocol):
    """Contract for market data sources (Alpaca V1, Polygon V2)."""

    async def fetch_latest_bars(
        self,
        symbol: str,
        timeframe: str,
        lookback_bars: int,
    ) -> list[Bar]: ...

    async def fetch_historical(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        use_cache: bool = True,
    ) -> pl.DataFrame: ...
