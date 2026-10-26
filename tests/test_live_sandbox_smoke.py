from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from scripts.adaptive_parameter_tuner import (
    DEFAULT_DAILY_TIMEFRAME,
    StrategyRuntime,
    _is_rate_limit_error,
    _resample_bars,
    load_live_market_data_with_retry,
    prepare_market_data_for_runtime,
)
from src.config import load_config


@dataclass
class _LogCapture:
    events: list[tuple[str, dict[str, Any]]]

    def warning(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, dict(kwargs)))

    def info(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, dict(kwargs)))


def _runtime() -> StrategyRuntime:
    config = load_config()
    strategy = next(
        strat for strat in config.strategies if strat.strategy_id == "mean_reversion_qqq"
    )
    return StrategyRuntime(
        strategy_id=strategy.strategy_id,
        module=strategy.module,
        symbol=strategy.symbol.upper(),
        timeframe=strategy.timeframe,
        poll_interval_seconds=strategy.poll_interval_seconds,
        environment="paper",
        params=dict(strategy.params),
        constraints={"regime_filter": True, "cold_start_policy": "HIGH_AVAILABILITY"},
        strategy_path=Path("config/strategies/mean_reversion_qqq.yaml"),
    )


def _require_live_smoke(live_smoke_enabled: bool) -> None:
    if not live_smoke_enabled:
        pytest.skip("set RUN_LIVE_SMOKE=1 or pass --run-live-smoke to enable")


def test_rate_limit_gate_retries_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    capture = _LogCapture(events=[])
    attempts = {"count": 0}

    async def _fake_loader(*args: Any, **kwargs: Any):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("429 Too Many Requests")
        return "intraday", "daily"

    monkeypatch.setattr(
        "scripts.adaptive_parameter_tuner._load_market_data",
        _fake_loader,
    )
    monkeypatch.setattr("scripts.adaptive_parameter_tuner.log", capture)

    result = asyncio.run(
        load_live_market_data_with_retry(
            runtime,
            lookback_days=5,
            source_timeframe="1Min",
            refresh_cache=True,
            max_retries=2,
            initial_backoff_seconds=0.01,
        )
    )

    assert result == ("intraday", "daily")
    assert attempts["count"] == 2
    assert any(event == "rate_limit_gate: RETRY_ENGAGED" for event, _ in capture.events)
    assert _is_rate_limit_error(RuntimeError("429 Too Many Requests")) is True


def test_live_sandbox_smoke_processing_path(
    live_smoke_enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_live_smoke(live_smoke_enabled)
    monkeypatch.setenv("SOAK_TEST_MODE", "1")
    runtime = _runtime()
    capture = _LogCapture(events=[])
    monkeypatch.setattr("scripts.adaptive_parameter_tuner.log", capture)

    intraday_df, daily_df = asyncio.run(
        load_live_market_data_with_retry(
            runtime,
            lookback_days=20,
            source_timeframe="1Min",
            refresh_cache=True,
        )
    )

    assert not intraday_df.is_empty()
    assert not daily_df.is_empty()
    assert "timestamp" in intraday_df.columns
    assert "close" in intraday_df.columns

    start_cutoff = datetime.now(timezone.utc) - timedelta(days=22)
    intraday_filtered = intraday_df.filter(pl.col("timestamp") >= start_cutoff)
    assert not intraday_filtered.is_empty()

    resampled = _resample_bars(intraday_filtered, runtime.timeframe)
    assert not resampled.is_empty()
    assert len(resampled.columns) == 7

    prepared = prepare_market_data_for_runtime(
        runtime,
        intraday_filtered,
        daily_df.filter(
            pl.col("timestamp") >= datetime.now(timezone.utc) - timedelta(days=450)
        ),
        train_frac=0.70,
    )

    assert prepared.symbol == "QQQ"
    assert prepared.timeframe == runtime.timeframe
    assert prepared.opens.shape == prepared.closes.shape
    assert prepared.atr.shape == prepared.closes.shape
    assert prepared.z_stack.shape[1] == prepared.closes.shape[0]
    assert prepared.train_end > 0
    assert os.getenv("SOAK_TEST_MODE") == "1"

    capture.info(
        "live_sandbox_smoke: PROMOTION_GATE_CLEARED",
        symbol=runtime.symbol,
        source_timeframe="1Min",
        target_timeframe=runtime.timeframe,
        intraday_rows=len(intraday_filtered),
        resampled_rows=len(resampled),
        daily_rows=len(daily_df),
    )
    assert any(
        event == "live_sandbox_smoke: PROMOTION_GATE_CLEARED"
        for event, _ in capture.events
    )
    assert DEFAULT_DAILY_TIMEFRAME == "1Day"
