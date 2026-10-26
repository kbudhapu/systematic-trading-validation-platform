"""Tests for async database writer queue."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.engine.attribution import (
    AttributionEnvironmentState,
    LiveAttributionRecord,
    TradeAttributionInput,
    log_trade_attribution,
)
from src.persistence.db_queue import (
    LOCAL_WRITE_AHEAD_DDL,
    AsyncDBWriter,
    QueuedWrite,
    WalLeakyBucketDrainer,
    WriteKind,
)


def test_async_db_writer_batches_trade_attribution(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    writer = AsyncDBWriter(db_path=str(db_path))
    writer.start()
    record = LiveAttributionRecord(
        trade_id="t1",
        timestamp=datetime.now(timezone.utc).isoformat(),
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        side="buy",
        qty=1.0,
        pnl=0.5,
        regime_id="CALM_MR",
        session_type="MIDDAY_DOLDRUMS",
        liquidity_state="NORMAL",
        execution_tactic="AGGRESSIVE",
        champion_version_id=1,
        ai_policy_execution_state="PASSIVE_SHADOW",
        promotion_id=None,
        expected_price=100.0,
        filled_price=100.1,
        slippage_pct=0.001,
    )
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.TRADE_ATTRIBUTION,
            payload=record.__dict__,
            db_path=str(db_path),
        )
    )
    with sqlite3.connect(db_path) as conn:
        conn.executescript(LOCAL_WRITE_AHEAD_DDL)
        staged = conn.execute("SELECT COUNT(*) FROM local_write_ahead_stage").fetchone()
    assert staged is not None
    assert int(staged[0]) == 1

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if writer.queue_depth == 0:
            break
        time.sleep(0.05)
    writer.stop()

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT trade_id, pnl FROM live_attribution_ledger WHERE trade_id = ?",
            ("t1",),
        ).fetchone()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM local_write_ahead_stage"
        ).fetchone()
    assert row is not None
    assert row[0] == "t1"
    assert float(row[1]) == 0.5
    assert remaining is not None
    assert int(remaining[0]) == 0


def test_write_ahead_retained_until_remote_commit(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    writer = AsyncDBWriter(db_path=str(db_path))
    record = LiveAttributionRecord(
        trade_id="wal_hold",
        timestamp=datetime.now(timezone.utc).isoformat(),
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        side="buy",
        qty=1.0,
        pnl=0.25,
        regime_id="CALM_MR",
        session_type="MIDDAY_DOLDRUMS",
        liquidity_state="NORMAL",
        execution_tactic="AGGRESSIVE",
        champion_version_id=1,
        ai_policy_execution_state="PASSIVE_SHADOW",
        promotion_id=None,
        expected_price=100.0,
        filled_price=100.0,
        slippage_pct=0.0,
    )
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.TRADE_ATTRIBUTION,
            payload=record.__dict__,
            db_path=str(db_path),
        )
    )
    with patch.object(writer, "_flush_trade_attribution_batch", return_value=False):
        writer._flush_from_write_ahead(force=True)
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM local_write_ahead_stage"
        ).fetchone()
    assert remaining is not None
    assert int(remaining[0]) == 1


def test_log_trade_attribution_uses_writer_when_running(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    writer = AsyncDBWriter(db_path=str(db_path), postgres_settings=None)
    writer.start()
    trade = TradeAttributionInput(
        trade_id="t2",
        timestamp=datetime.now(timezone.utc),
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        side="buy",
        qty=2.0,
        pnl=1.0,
        expected_price=100.0,
        filled_price=100.0,
        slippage_pct=0.0,
    )
    env = AttributionEnvironmentState(regime_id="CALM_MR")
    log_trade_attribution(trade, env, db_path=db_path, use_async_writer=True)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and writer.queue_depth > 0:
        time.sleep(0.05)
    writer.stop()


def test_wal_leaky_bucket_drainer_limits_tokens() -> None:
    limiter = WalLeakyBucketDrainer(
        max_batches_per_interval=2,
        interval_seconds=900.0,
    )
    assert limiter.acquire(force=False) is True
    assert limiter.acquire(force=False) is True
    assert limiter.acquire(force=False) is False
    assert limiter.acquire(force=True) is True


def test_flush_from_write_ahead_throttles_when_bucket_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    writer = AsyncDBWriter(db_path=str(db_path), postgres_settings=None)
    writer._drain_limiter = WalLeakyBucketDrainer(max_batches_per_interval=1, interval_seconds=3600.0)
    record = LiveAttributionRecord(
        trade_id="throttle-1",
        timestamp=datetime.now(timezone.utc).isoformat(),
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        side="buy",
        qty=1.0,
        pnl=0.1,
        regime_id="CALM_MR",
        session_type="MIDDAY_DOLDRUMS",
        liquidity_state="NORMAL",
        execution_tactic="AGGRESSIVE",
        champion_version_id=1,
        ai_policy_execution_state="PASSIVE_SHADOW",
        promotion_id=None,
        expected_price=100.0,
        filled_price=100.0,
        slippage_pct=0.0,
    )
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.TRADE_ATTRIBUTION,
            payload=record.__dict__,
            db_path=str(db_path),
        )
    )
    writer._flush_from_write_ahead(force=False)
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.TRADE_ATTRIBUTION,
            payload={
                **record.__dict__,
                "trade_id": "throttle-2",
            },
            db_path=str(db_path),
        )
    )
    writer._flush_from_write_ahead(force=False)
    assert writer._stats["wal_throttled"] >= 1
    assert writer._pending_write_ahead_count() >= 1
