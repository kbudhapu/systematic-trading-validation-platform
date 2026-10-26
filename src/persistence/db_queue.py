"""
Asynchronous database writer queue for non-blocking execution-cycle persistence.

Truth model hierarchy
---------------------
Local SQLite databases are the execution source of truth:
  - ``trading.db`` — operational bot state, runs, and control telemetry
  - ``research_vault.db`` — attribution, governance, and research vault tables

Supabase Postgres is a read-replica for dashboard visualization. Writes always
land in SQLite first via the write-ahead stage; Postgres drains are best-effort
replication and must never be treated as authoritative for live execution.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import structlog

from src.engine.attribution import (
    LiveAttributionRecord,
    ensure_live_attribution_schema,
)
from src.engine.portfolio_coordinator import ensure_portfolio_constraint_schema
from src.persistence.db import RESEARCH_VAULT_PATH
from src.persistence.ownership_guard import ensure_db_writable
from src.persistence.postgres_config import PostgresConnectionSettings

log = structlog.get_logger()

WRITER_FLUSH_INTERVAL_SECONDS = 0.25
WRITER_MAX_BATCH_SIZE = 64
WRITER_QUEUE_MAXLEN = 50_000
WAL_MAX_BATCHES_PER_BAR_INTERVAL = 2
WAL_BURST_MAX_BATCHES_PER_INTERVAL = 8
WAL_BAR_INTERVAL_SECONDS = 900.0
WAL_BACKLOG_HIGH_WATER = 128
WAL_BACKLOG_PAGE_THRESHOLD = 256

LOCAL_WRITE_AHEAD_DDL = """
CREATE TABLE IF NOT EXISTS local_write_ahead_stage (
    stage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    write_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    target_db_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wal_stage_pending ON local_write_ahead_stage(stage_id);
"""

DUAL_POLICY_SHADOW_DDL = """
CREATE TABLE IF NOT EXISTS dual_policy_shadow_log (
    log_id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    champion_id TEXT NOT NULL,
    challenger_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    session_type TEXT NOT NULL,
    regime_id TEXT NOT NULL,
    market_state_json JSONB NOT NULL DEFAULT '{}',
    champion_action TEXT NOT NULL,
    challenger_action TEXT NOT NULL,
    champion_capital DOUBLE PRECISION NOT NULL,
    challenger_capital DOUBLE PRECISION NOT NULL,
    champion_pnl DOUBLE PRECISION NOT NULL,
    challenger_pnl DOUBLE PRECISION NOT NULL,
    rules_baseline_pnl DOUBLE PRECISION NOT NULL,
    matched_capital_notional DOUBLE PRECISION NOT NULL,
    execution_path_json JSONB NOT NULL DEFAULT '{}'
);
"""

POSTGRES_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS live_attribution_ledger (
    attribution_id BIGSERIAL PRIMARY KEY,
    trade_id TEXT NOT NULL UNIQUE,
    timestamp TIMESTAMPTZ NOT NULL,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty DOUBLE PRECISION NOT NULL,
    pnl DOUBLE PRECISION NOT NULL,
    regime_id TEXT NOT NULL,
    session_type TEXT NOT NULL,
    liquidity_state TEXT NOT NULL,
    execution_tactic TEXT NOT NULL,
    champion_version_id INTEGER,
    ai_policy_execution_state TEXT NOT NULL,
    promotion_id TEXT,
    expected_price DOUBLE PRECISION,
    filled_price DOUBLE PRECISION,
    slippage_pct DOUBLE PRECISION,
    execution_direction_type TEXT,
    slip_direction_long_entry DOUBLE PRECISION,
    slip_direction_long_exit DOUBLE PRECISION,
    slip_direction_short_entry DOUBLE PRECISION,
    slip_direction_short_exit DOUBLE PRECISION,
    markout_5bar DOUBLE PRECISION,
    participation_cap_pct DOUBLE PRECISION,
    metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS portfolio_constraint_ledger (
    log_id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    cycle_id TEXT NOT NULL,
    constraint_type TEXT NOT NULL,
    strategy_id TEXT,
    symbol TEXT,
    action_taken TEXT NOT NULL,
    sizing_multiplier DOUBLE PRECISION,
    metadata_json TEXT NOT NULL,
    multi_day_net_inventory DOUBLE PRECISION,
    directional_entry_lockout TEXT,
    current_equity DOUBLE PRECISION
);
""" + DUAL_POLICY_SHADOW_DDL + """
CREATE TABLE IF NOT EXISTS dashboard_summary_snapshots (
    environment TEXT PRIMARY KEY,
    generated_at TIMESTAMPTZ NOT NULL,
    summary_json JSONB NOT NULL,
    performance_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class WriteKind(str, Enum):
    TRADE_ATTRIBUTION = "trade_attribution"
    PORTFOLIO_CONSTRAINT = "portfolio_constraint"
    COUNTERFACTUAL_STATE = "counterfactual_state"
    PORTFOLIO_RISK_STATE = "portfolio_risk_state"
    CYCLE_METRICS = "cycle_metrics"
    TRIAL_LEDGER = "trial_ledger"
    DIAGNOSTIC_REPORT = "diagnostic_report"
    QUARANTINED_HYPOTHESIS = "quarantined_hypothesis"
    HEARTBEAT = "heartbeat"
    CONFIG_HASH_CHAIN = "config_hash_chain"
    RECONCILIATION = "reconciliation"


# --- WriteKind -> flush-handler registry (E3/R2) ---------------------------- #
# A store self-registers its append-only flush handler at import time, so adding
# a NEW telemetry kind requires ZERO edits to the AsyncDBWriter dispatch below
# (the F3 shotgun-surgery fix). A handler is
# ``(items: list[QueuedWrite], sqlite_path: Path) -> bool`` (True == committed).
# The complex legacy kinds (trade_attribution / portfolio_constraint / etc.,
# which also drive the Postgres drain) stay in the explicit dispatch fallback.
from collections.abc import Callable  # noqa: E402

_WRITE_HANDLERS: dict["WriteKind", Callable[[list, "Path"], bool]] = {}


def register_write_handler(kind: "WriteKind", handler: Callable[[list, "Path"], bool]) -> None:
    _WRITE_HANDLERS[kind] = handler


def get_write_handler(kind: "WriteKind"):
    return _WRITE_HANDLERS.get(kind)


@dataclass(frozen=True)
class QueuedWrite:
    kind: WriteKind
    payload: dict[str, Any]
    db_path: str


@dataclass(frozen=True)
class StagedWrite:
    stage_id: int
    item: QueuedWrite


class WalLeakyBucketDrainer:
    """Rate-limits WAL postgres drains; scales burst allowance on backlog pressure."""

    __slots__ = (
        "_base_max_batches",
        "_burst_max_batches",
        "_interval_seconds",
        "_last_refill",
        "_lock",
        "_max_batches",
        "_tokens",
    )

    def __init__(
        self,
        *,
        max_batches_per_interval: int = WAL_MAX_BATCHES_PER_BAR_INTERVAL,
        burst_max_batches_per_interval: int = WAL_BURST_MAX_BATCHES_PER_INTERVAL,
        interval_seconds: float = WAL_BAR_INTERVAL_SECONDS,
    ) -> None:
        self._base_max_batches = max(int(max_batches_per_interval), 1)
        self._burst_max_batches = max(
            int(burst_max_batches_per_interval),
            self._base_max_batches,
        )
        self._max_batches = self._base_max_batches
        self._interval_seconds = max(float(interval_seconds), 1.0)
        self._lock = threading.Lock()
        self._tokens = float(self._max_batches)
        self._last_refill = time.monotonic()

    def update_backlog_depth(self, depth: int) -> None:
        with self._lock:
            target = (
                self._burst_max_batches
                if depth >= WAL_BACKLOG_HIGH_WATER
                else self._base_max_batches
            )
            if target == self._max_batches:
                return
            prior_max = self._max_batches
            self._max_batches = target
            if target > prior_max:
                self._tokens = float(target)
            else:
                self._tokens = min(self._tokens, float(self._max_batches))

    def acquire(self, *, force: bool) -> bool:
        if force:
            return True
        with self._lock:
            self._refill_locked()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0.0:
            return
        refill = (elapsed / self._interval_seconds) * float(self._max_batches)
        if refill <= 0.0:
            return
        self._tokens = min(float(self._max_batches), self._tokens + refill)
        self._last_refill = now


class AsyncDBWriter:
    """Durable write-ahead staging with a dedicated batch persistence worker."""

    def __init__(
        self,
        *,
        db_path: str | None = None,
        postgres_settings: PostgresConnectionSettings | None = None,
        flush_interval_seconds: float = WRITER_FLUSH_INTERVAL_SECONDS,
        max_batch_size: int = WRITER_MAX_BATCH_SIZE,
    ) -> None:
        self._db_path = db_path or str(RESEARCH_VAULT_PATH)
        self._postgres_settings = postgres_settings or PostgresConnectionSettings.from_env()
        self._flush_interval_seconds = max(flush_interval_seconds, 0.05)
        self._max_batch_size = max(int(max_batch_size), 1)
        self._wake_queue: deque[int] = deque(maxlen=WRITER_QUEUE_MAXLEN)
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()   # serialize worker vs stop() drains
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._postgres_ready = False
        self._wal_schema_ready = False
        self._drain_limiter = WalLeakyBucketDrainer()
        self._stats = {
            "enqueued": 0,
            "flushed": 0,
            "postgres_batches": 0,
            "sqlite_batches": 0,
            "wal_deleted": 0,
            "wal_throttled": 0,
            "errors": 0,
        }
        self._ensure_write_ahead_schema()

    @property
    def is_running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    @property
    def queue_depth(self) -> int:
        return self._pending_write_ahead_count()

    def telemetry_snapshot(self) -> dict[str, Any]:
        """Export live WAL queue counters for dashboard telemetry."""
        with self._lock:
            stats = dict(self._stats)
        backlog = self._pending_write_ahead_count()
        return {
            "wal_backlog_depth": backlog,
            "queue_depth": backlog,
            "enqueued": int(stats.get("enqueued", 0)),
            "flushed": int(stats.get("flushed", 0)),
            "postgres_batches": int(stats.get("postgres_batches", 0)),
            "sqlite_batches": int(stats.get("sqlite_batches", 0)),
            "wal_deleted": int(stats.get("wal_deleted", 0)),
            "wal_throttled": int(stats.get("wal_throttled", 0)),
            "errors": int(stats.get("errors", 0)),
            "worker_running": self.is_running,
            "postgres_configured": self._postgres_settings is not None,
        }

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="async-db-writer",
            daemon=True,
        )
        self._worker.start()
        log.info(
            "async_db_writer_started",
            postgres_configured=self._postgres_settings is not None,
            pool_port=(
                self._postgres_settings.pool_port
                if self._postgres_settings is not None
                else None
            ),
        )

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        deadline = time.monotonic() + max(timeout_seconds, 0.1)
        if self.is_running:
            self._stop_event.set()
            assert self._worker is not None
            # remaining time budget for the join (the worker wakes immediately on
            # the stop event, so this normally returns well under the timeout).
            self._worker.join(timeout=max(deadline - time.monotonic(), 0.1))
        # bounded best-effort drain: never hangs, even on a poison batch.
        self._drain_until_empty(deadline=deadline)
        log.info("async_db_writer_stopped", stats=dict(self._stats))

    def _drain_until_empty(self, *, deadline: float) -> None:
        """Drain the write-ahead stage until empty, bounded by ``deadline`` and a
        no-progress guard. A batch that persistently fails to commit (e.g. a poison
        payload) would otherwise spin this loop forever -- that unbounded loop was
        the db_queue shutdown-timeout bug; here it terminates and logs instead."""
        while self._pending_write_ahead_count() > 0:
            if time.monotonic() >= deadline:
                log.warning("async_db_writer_stop_timeout",
                            pending=self._pending_write_ahead_count())
                return
            before = self._pending_write_ahead_count()
            self._flush_from_write_ahead(force=True)
            if self._pending_write_ahead_count() >= before:
                # no forward progress -> a batch cannot commit; stop rather than hang
                log.warning("async_db_writer_stop_no_progress",
                            pending=self._pending_write_ahead_count())
                return

    def enqueue(self, item: QueuedWrite) -> None:
        stage_id = self._append_write_ahead(item)
        with self._lock:
            self._wake_queue.append(stage_id)
            self._stats["enqueued"] += 1

    def _connect_wal(self):
        """Open the write-ahead-stage db in WAL journal mode with a long
        busy_timeout so concurrent enqueues (many producer threads) and the
        worker's drain do not raise 'database is locked' and drop writes under a
        high-volume storm."""
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_write_ahead_schema(self) -> None:
        if self._wal_schema_ready:
            return
        wal_path = Path(self._db_path)
        ensure_db_writable(wal_path)
        wal_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect_wal() as conn:
            conn.executescript(LOCAL_WRITE_AHEAD_DDL)
        self._wal_schema_ready = True

    def _append_write_ahead(self, item: QueuedWrite) -> int:
        self._ensure_write_ahead_schema()
        created_at = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(item.payload, separators=(",", ":"), default=str)
        with self._connect_wal() as conn:
            cur = conn.execute(
                """
                INSERT INTO local_write_ahead_stage (
                    write_kind, payload_json, target_db_path, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    item.kind.value,
                    payload_json,
                    item.db_path,
                    created_at,
                ),
            )
            return int(cur.lastrowid)

    def _pending_write_ahead_count(self) -> int:
        self._ensure_write_ahead_schema()
        with self._connect_wal() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM local_write_ahead_stage"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def _fetch_staged_batch(self, limit: int) -> list[StagedWrite]:
        self._ensure_write_ahead_schema()
        with self._connect_wal() as conn:
            rows = conn.execute(
                """
                SELECT stage_id, write_kind, payload_json, target_db_path
                FROM local_write_ahead_stage
                ORDER BY stage_id ASC
                LIMIT ?
                """,
                (max(int(limit), 1),),
            ).fetchall()
        staged: list[StagedWrite] = []
        for stage_id, write_kind, payload_json, target_db_path in rows:
            payload = json.loads(str(payload_json))
            staged.append(
                StagedWrite(
                    stage_id=int(stage_id),
                    item=QueuedWrite(
                        kind=WriteKind(str(write_kind)),
                        payload=payload,
                        db_path=str(target_db_path),
                    ),
                )
            )
        return staged

    def _delete_staged_rows(self, stage_ids: list[int]) -> None:
        if not stage_ids:
            return
        placeholders = ",".join("?" for _ in stage_ids)
        with self._connect_wal() as conn:
            conn.execute(
                f"DELETE FROM local_write_ahead_stage WHERE stage_id IN ({placeholders})",
                stage_ids,
            )
        self._stats["wal_deleted"] += len(stage_ids)

    def _drain_wake_signals(self) -> None:
        with self._lock:
            self._wake_queue.clear()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            self._flush_from_write_ahead(force=False)
            # wait ON the stop event (not a blind sleep) so shutdown is observed
            # immediately -- this is what prevents the worker from force-flushing
            # concurrently with stop()'s drain (the WAL drain race).
            self._stop_event.wait(self._flush_interval_seconds)
        self._flush_from_write_ahead(force=True)

    def _flush_from_write_ahead(self, *, force: bool) -> None:
        # serialize drains: the worker's final flush and stop()'s drain must never
        # read/delete the same staged rows concurrently.
        with self._flush_lock:
            self._flush_from_write_ahead_locked(force=force)

    def _flush_from_write_ahead_locked(self, *, force: bool) -> None:
        backlog = self._pending_write_ahead_count()
        self._drain_limiter.update_backlog_depth(backlog)
        while True:
            if not self._drain_limiter.acquire(force=force):
                self._stats["wal_throttled"] += 1
                return
            batch = self._fetch_staged_batch(self._max_batch_size)
            if not batch:
                self._drain_wake_signals()
                return
            try:
                committed_ids = self._persist_staged_batch(batch)
                if not committed_ids:
                    log.warning(
                        "async_db_writer_batch_uncommitted",
                        pending=len(batch),
                    )
                    return
                self._delete_staged_rows(committed_ids)
                self._stats["flushed"] += len(committed_ids)
                self._drain_wake_signals()
            except Exception as exc:
                self._stats["errors"] += 1
                log.error(
                    "async_db_writer_batch_failed",
                    error=str(exc),
                    size=len(batch),
                )
                return
            if not force:
                return

    def _persist_staged_batch(self, batch: list[StagedWrite]) -> list[int]:
        grouped: dict[tuple[WriteKind, str], list[StagedWrite]] = {}
        for staged in batch:
            key = (staged.item.kind, staged.item.db_path)
            grouped.setdefault(key, []).append(staged)

        committed: list[int] = []
        for (kind, db_path_str), items in grouped.items():
            # Per-group fault isolation. A single group that raises (e.g. a
            # cross-platform WAL spool row carried in a restored vault whose
            # target_db_path is a foreign absolute path -- a Windows path
            # replayed on Linux -- trips the root/create guard) must NEVER abort
            # the whole drain. Before this, one poisoned group at the head of the
            # oldest-first batch jammed every healthy group behind it forever
            # (the droplet-migration incident). Isolate the failure: log it,
            # leave that group's rows staged for a later retry, and keep draining
            # the rest so healthy writes still commit.
            try:
                committed.extend(self._persist_group(kind, Path(db_path_str), items))
            except Exception as exc:
                self._stats["errors"] += 1
                log.error(
                    "async_db_writer_group_failed",
                    error=str(exc),
                    kind=kind.value,
                    target=db_path_str,
                    rows=len(items),
                )
        return committed

    def _persist_group(
        self,
        kind: WriteKind,
        sqlite_path: Path,
        items: list[StagedWrite],
    ) -> list[int]:
        """Persist one (kind, target) group; return the committed stage_ids.

        Raises on write failure -- the caller isolates the failure per group so a
        poisoned group cannot jam the drain for healthy groups behind it.
        """
        committed: list[int] = []
        # E3/R2: registered handlers first (self-registering clone stores).
        handler = get_write_handler(kind)
        if handler is not None:
            queued = [staged.item for staged in items]
            if handler(queued, sqlite_path):
                self._stats["sqlite_batches"] += 1
                committed.extend(staged.stage_id for staged in items)
            return committed
        # Legacy/complex kinds (also drive the Postgres drain) stay explicit.
        if kind == WriteKind.TRADE_ATTRIBUTION:
            records = [LiveAttributionRecord(**staged.item.payload) for staged in items]
            if self._flush_trade_attribution_batch(records, sqlite_path):
                committed.extend(staged.stage_id for staged in items)
        elif kind == WriteKind.PORTFOLIO_CONSTRAINT:
            queued = [staged.item for staged in items]
            if self._flush_portfolio_constraint_batch(queued, sqlite_path):
                committed.extend(staged.stage_id for staged in items)
        elif kind == WriteKind.COUNTERFACTUAL_STATE:
            queued = [staged.item for staged in items]
            if self._flush_counterfactual_batch(queued, sqlite_path):
                committed.extend(staged.stage_id for staged in items)
        elif kind == WriteKind.PORTFOLIO_RISK_STATE:
            queued = [staged.item for staged in items]
            if self._flush_portfolio_risk_state_batch(queued, sqlite_path):
                committed.extend(staged.stage_id for staged in items)
        elif kind == WriteKind.CYCLE_METRICS:
            queued = [staged.item for staged in items]
            if self._flush_cycle_metrics_batch(queued, sqlite_path):
                committed.extend(staged.stage_id for staged in items)
        return committed

    def _postgres_connection(self):
        import psycopg

        if self._postgres_settings is None:
            raise RuntimeError("postgres not configured")
        return psycopg.connect(self._postgres_settings.dsn(use_pool=True))

    def _ensure_postgres_schema(self) -> None:
        if self._postgres_ready or self._postgres_settings is None:
            return
        with self._postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(POSTGRES_SCHEMA_DDL)
            conn.commit()
        self._postgres_ready = True

    def _flush_trade_attribution_batch(
        self,
        records: list[LiveAttributionRecord],
        sqlite_path,
    ) -> bool:
        if not records:
            return True
        ensure_live_attribution_schema(sqlite_path)
        with sqlite3.connect(sqlite_path) as conn:
            conn.executemany(
                """
                INSERT INTO live_attribution_ledger (
                    trade_id, timestamp, strategy_id, symbol, side, qty, pnl,
                    regime_id, session_type, liquidity_state, execution_tactic,
                    champion_version_id, ai_policy_execution_state, promotion_id,
                    expected_price, filled_price, slippage_pct, execution_direction_type,
                    slip_direction_long_entry, slip_direction_long_exit,
                    slip_direction_short_entry, slip_direction_short_exit,
                    markout_5bar, participation_cap_pct, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    pnl = excluded.pnl,
                    slippage_pct = excluded.slippage_pct,
                    execution_direction_type = excluded.execution_direction_type,
                    slip_direction_long_entry = excluded.slip_direction_long_entry,
                    slip_direction_long_exit = excluded.slip_direction_long_exit,
                    slip_direction_short_entry = excluded.slip_direction_short_entry,
                    slip_direction_short_exit = excluded.slip_direction_short_exit,
                    markout_5bar = COALESCE(excluded.markout_5bar, live_attribution_ledger.markout_5bar),
                    metadata_json = excluded.metadata_json
                """,
                [record.to_insert_tuple() for record in records],
            )
        self._stats["sqlite_batches"] += 1

        if self._postgres_settings is None:
            return True
        try:
            self._ensure_postgres_schema()
            with self._postgres_connection() as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO live_attribution_ledger (
                            trade_id, timestamp, strategy_id, symbol, side, qty, pnl,
                            regime_id, session_type, liquidity_state, execution_tactic,
                            champion_version_id, ai_policy_execution_state, promotion_id,
                            expected_price, filled_price, slippage_pct, execution_direction_type,
                            slip_direction_long_entry, slip_direction_long_exit,
                            slip_direction_short_entry, slip_direction_short_exit,
                            markout_5bar, participation_cap_pct, metadata_json
                        ) VALUES (
                            %(trade_id)s, %(timestamp)s, %(strategy_id)s, %(symbol)s, %(side)s,
                            %(qty)s, %(pnl)s, %(regime_id)s, %(session_type)s, %(liquidity_state)s,
                            %(execution_tactic)s, %(champion_version_id)s, %(ai_policy_execution_state)s,
                            %(promotion_id)s, %(expected_price)s, %(filled_price)s, %(slippage_pct)s,
                            %(execution_direction_type)s, %(slip_direction_long_entry)s,
                            %(slip_direction_long_exit)s, %(slip_direction_short_entry)s,
                            %(slip_direction_short_exit)s, %(markout_5bar)s, %(participation_cap_pct)s,
                            %(metadata_json)s
                        )
                        ON CONFLICT (trade_id) DO UPDATE SET
                            pnl = EXCLUDED.pnl,
                            slippage_pct = EXCLUDED.slippage_pct,
                            metadata_json = EXCLUDED.metadata_json
                        """,
                        [
                            {
                                "trade_id": record.trade_id,
                                "timestamp": record.timestamp,
                                "strategy_id": record.strategy_id,
                                "symbol": record.symbol,
                                "side": record.side,
                                "qty": record.qty,
                                "pnl": record.pnl,
                                "regime_id": record.regime_id,
                                "session_type": record.session_type,
                                "liquidity_state": record.liquidity_state,
                                "execution_tactic": record.execution_tactic,
                                "champion_version_id": record.champion_version_id,
                                "ai_policy_execution_state": record.ai_policy_execution_state,
                                "promotion_id": record.promotion_id,
                                "expected_price": record.expected_price,
                                "filled_price": record.filled_price,
                                "slippage_pct": record.slippage_pct,
                                "execution_direction_type": record.execution_direction_type,
                                "slip_direction_long_entry": record.slip_direction_long_entry,
                                "slip_direction_long_exit": record.slip_direction_long_exit,
                                "slip_direction_short_entry": record.slip_direction_short_entry,
                                "slip_direction_short_exit": record.slip_direction_short_exit,
                                "markout_5bar": record.markout_5bar,
                                "participation_cap_pct": record.participation_cap_pct,
                                "metadata_json": record.metadata_json,
                            }
                            for record in records
                        ],
                    )
                conn.commit()
            self._stats["postgres_batches"] += 1
            return True
        except Exception as exc:
            log.warning("postgres_trade_attribution_batch_failed", error=str(exc))
            return False

    def _flush_portfolio_constraint_batch(self, items: list[QueuedWrite], sqlite_path) -> bool:
        if not items:
            return True
        ensure_portfolio_constraint_schema(sqlite_path)
        rows = [item.payload for item in items]
        with sqlite3.connect(sqlite_path) as conn:
            conn.executemany(
                """
                INSERT INTO portfolio_constraint_ledger (
                    timestamp, cycle_id, constraint_type, strategy_id, symbol,
                    action_taken, sizing_multiplier, metadata_json,
                    multi_day_net_inventory, directional_entry_lockout, current_equity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["timestamp"],
                        row["cycle_id"],
                        row["constraint_type"],
                        row.get("strategy_id"),
                        row.get("symbol"),
                        row["action_taken"],
                        row.get("sizing_multiplier"),
                        row.get("metadata_json"),
                        row.get("multi_day_net_inventory"),
                        row.get("directional_entry_lockout"),
                        row.get("current_equity"),
                    )
                    for row in rows
                ],
            )
        self._stats["sqlite_batches"] += 1

        if self._postgres_settings is None:
            return True
        try:
            self._ensure_postgres_schema()
            with self._postgres_connection() as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO portfolio_constraint_ledger (
                            timestamp, cycle_id, constraint_type, strategy_id, symbol,
                            action_taken, sizing_multiplier, metadata_json,
                            multi_day_net_inventory, directional_entry_lockout, current_equity
                        ) VALUES (
                            %(timestamp)s, %(cycle_id)s, %(constraint_type)s, %(strategy_id)s,
                            %(symbol)s, %(action_taken)s, %(sizing_multiplier)s, %(metadata_json)s,
                            %(multi_day_net_inventory)s, %(directional_entry_lockout)s, %(current_equity)s
                        )
                        """,
                        rows,
                    )
                conn.commit()
            self._stats["postgres_batches"] += 1
            return True
        except Exception as exc:
            log.warning("postgres_portfolio_constraint_batch_failed", error=str(exc))
            return False

    def _flush_counterfactual_batch(self, items: list[QueuedWrite], sqlite_path) -> bool:
        if not items:
            return True
        from src.engine.challenger_registry import ensure_challenger_schema

        ensure_challenger_schema(sqlite_path)
        rows = [item.payload for item in items]
        with sqlite3.connect(sqlite_path) as conn:
            conn.executemany(
                """
                INSERT INTO dual_policy_shadow_log (
                    timestamp, champion_id, challenger_id, symbol, session_type,
                    regime_id, market_state_json, champion_action, challenger_action,
                    champion_capital, challenger_capital, champion_pnl, challenger_pnl,
                    rules_baseline_pnl, matched_capital_notional, execution_path_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["timestamp"],
                        row["champion_id"],
                        row["challenger_id"],
                        row["symbol"],
                        row["session_type"],
                        row["regime_id"],
                        row["market_state_json"],
                        row["champion_action"],
                        row["challenger_action"],
                        row["champion_capital"],
                        row["challenger_capital"],
                        row["champion_pnl"],
                        row["challenger_pnl"],
                        row["rules_baseline_pnl"],
                        row["matched_capital_notional"],
                        row["execution_path_json"],
                    )
                    for row in rows
                ],
            )
        self._stats["sqlite_batches"] += 1

        if self._postgres_settings is None:
            return True
        try:
            self._ensure_postgres_schema()
            with self._postgres_connection() as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO dual_policy_shadow_log (
                            timestamp, champion_id, challenger_id, symbol, session_type,
                            regime_id, market_state_json, champion_action, challenger_action,
                            champion_capital, challenger_capital, champion_pnl, challenger_pnl,
                            rules_baseline_pnl, matched_capital_notional, execution_path_json
                        ) VALUES (
                            %(timestamp)s, %(champion_id)s, %(challenger_id)s, %(symbol)s,
                            %(session_type)s, %(regime_id)s, %(market_state_json)s::jsonb,
                            %(champion_action)s, %(challenger_action)s, %(champion_capital)s,
                            %(challenger_capital)s, %(champion_pnl)s, %(challenger_pnl)s,
                            %(rules_baseline_pnl)s, %(matched_capital_notional)s,
                            %(execution_path_json)s::jsonb
                        )
                        """,
                        rows,
                    )
                conn.commit()
            self._stats["postgres_batches"] += 1
            return True
        except Exception as exc:
            log.warning("postgres_counterfactual_batch_failed", error=str(exc))
            return False

    def _flush_portfolio_risk_state_batch(
        self,
        items: list[QueuedWrite],
        sqlite_path: Path,
    ) -> bool:
        if not items:
            return True
        from src.persistence.portfolio_risk_state_store import persist_portfolio_risk_state

        latest = items[-1].payload
        persist_portfolio_risk_state(latest, db_path=sqlite_path)
        self._stats["sqlite_batches"] += 1
        return True

    def _flush_cycle_metrics_batch(
        self,
        items: list[QueuedWrite],
        sqlite_path: Path,
    ) -> bool:
        if not items:
            return True
        from src.persistence.cycle_metrics_store import persist_cycle_metrics_row

        for item in items:
            payload = item.payload
            persist_cycle_metrics_row(
                phase_a_ms=float(payload.get("phase_a_ms", 0.0)),
                phase_b_ms=float(payload.get("phase_b_ms", 0.0)),
                phase_c_ms=float(payload.get("phase_c_ms", 0.0)),
                total_cycle_ms=float(payload.get("total_cycle_ms", 0.0)),
                sieve_backlog_qty=int(payload.get("sieve_backlog_qty", 0)),
                db_path=sqlite_path,
            )
        self._stats["sqlite_batches"] += 1
        return True

    def _flush_trial_ledger_batch(
        self,
        items: list[QueuedWrite],
        sqlite_path: Path,
    ) -> bool:
        if not items:
            return True
        from src.persistence.trial_ledger_store import persist_trial_rows

        persist_trial_rows([item.payload for item in items], sqlite_path)
        self._stats["sqlite_batches"] += 1
        return True

    def _flush_diagnostic_report_batch(
        self,
        items: list[QueuedWrite],
        sqlite_path: Path,
    ) -> bool:
        if not items:
            return True
        from src.persistence.diagnostic_report_store import persist_diagnostic_reports

        persist_diagnostic_reports([item.payload for item in items], sqlite_path)
        self._stats["sqlite_batches"] += 1
        return True

    def _flush_quarantined_hypothesis_batch(
        self,
        items: list[QueuedWrite],
        sqlite_path: Path,
    ) -> bool:
        if not items:
            return True
        from src.persistence.diagnostic_report_store import persist_quarantined_hypotheses

        persist_quarantined_hypotheses([item.payload for item in items], sqlite_path)
        self._stats["sqlite_batches"] += 1
        return True

    def _flush_heartbeat_batch(
        self,
        items: list[QueuedWrite],
        sqlite_path: Path,
    ) -> bool:
        if not items:
            return True
        from src.persistence.heartbeat_store import persist_heartbeats

        persist_heartbeats([item.payload for item in items], sqlite_path)
        self._stats["sqlite_batches"] += 1
        return True

    def _flush_hash_chain_batch(
        self,
        items: list[QueuedWrite],
        sqlite_path: Path,
    ) -> bool:
        if not items:
            return True
        from src.persistence.hash_chain_store import persist_chain_entries

        persist_chain_entries([item.payload for item in items], sqlite_path)
        self._stats["sqlite_batches"] += 1
        return True

    def _flush_reconciliation_batch(
        self,
        items: list[QueuedWrite],
        sqlite_path: Path,
    ) -> bool:
        if not items:
            return True
        from src.persistence.reconciliation_store import persist_reconciliation_rows

        persist_reconciliation_rows([item.payload for item in items], sqlite_path)
        self._stats["sqlite_batches"] += 1
        return True


_writer: AsyncDBWriter | None = None
_writer_lock = threading.Lock()


def get_async_db_writer() -> AsyncDBWriter:
    global _writer
    with _writer_lock:
        if _writer is None:
            _writer = AsyncDBWriter()
        return _writer


def start_async_db_writer() -> AsyncDBWriter:
    writer = get_async_db_writer()
    writer.start()
    return writer


def stop_async_db_writer() -> None:
    global _writer
    with _writer_lock:
        if _writer is not None:
            _writer.stop()
            _writer = None


def get_write_ahead_backlog_depth() -> int:
    """Return pending local write-ahead rows awaiting Postgres drain."""
    return get_async_db_writer().queue_depth


def get_queue_telemetry() -> dict[str, Any]:
    """Return live AsyncDBWriter counters for governance snapshots."""
    return get_async_db_writer().telemetry_snapshot()


def enqueue_trade_attribution(record: LiveAttributionRecord, *, db_path: str | None = None) -> None:
    writer = get_async_db_writer()
    if not writer.is_running:
        writer.start()
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.TRADE_ATTRIBUTION,
            payload=record.__dict__,
            db_path=db_path or str(RESEARCH_VAULT_PATH),
        )
    )


def enqueue_trial_ledger(payload: dict[str, Any], *, db_path: str | None = None) -> None:
    """Append a PSD trial-accounting entry through the existing writer (no new
    writer thread). Append-only -- the drain only ever INSERTs."""
    writer = get_async_db_writer()
    if not writer.is_running:
        writer.start()
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.TRIAL_LEDGER,
            payload=payload,
            db_path=db_path or str(RESEARCH_VAULT_PATH),
        )
    )


def enqueue_diagnostic_report(payload: dict[str, Any], *, db_path: str | None = None) -> None:
    """Append a VTD DiagnosticReport through the existing writer (no new writer
    thread). Append-only -- the drain only ever INSERTs (doctrine section 4)."""
    writer = get_async_db_writer()
    if not writer.is_running:
        writer.start()
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.DIAGNOSTIC_REPORT,
            payload=payload,
            db_path=db_path or str(RESEARCH_VAULT_PATH),
        )
    )


def enqueue_quarantined_hypothesis(payload: dict[str, Any], *, db_path: str | None = None) -> None:
    """Append a quarantined hypothesis through the existing writer. Append-only."""
    writer = get_async_db_writer()
    if not writer.is_running:
        writer.start()
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.QUARANTINED_HYPOTHESIS,
            payload=payload,
            db_path=db_path or str(RESEARCH_VAULT_PATH),
        )
    )


def enqueue_heartbeat(payload: dict[str, Any], *, db_path: str | None = None) -> None:
    """Append a liveness heartbeat through the existing writer (no new writer
    thread). Append-only; the watchdog reads the latest beat's age."""
    writer = get_async_db_writer()
    if not writer.is_running:
        writer.start()
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.HEARTBEAT,
            payload=payload,
            db_path=db_path or str(RESEARCH_VAULT_PATH),
        )
    )


def enqueue_hash_chain_entry(payload: dict[str, Any], *, db_path: str | None = None) -> None:
    """Append a config-hash-chain leaf through the existing writer (LLD section 3).
    Append-only; the refit runner writes chain entries itself."""
    writer = get_async_db_writer()
    if not writer.is_running:
        writer.start()
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.CONFIG_HASH_CHAIN,
            payload=payload,
            db_path=db_path or str(RESEARCH_VAULT_PATH),
        )
    )


def enqueue_reconciliation(payload: dict[str, Any], *, db_path: str | None = None) -> None:
    """Append a Stage-5 reconciliation row (fill cost / timing delta) through the
    existing writer. payload['record_type'] in {'fill_cost','timing_delta'}."""
    writer = get_async_db_writer()
    if not writer.is_running:
        writer.start()
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.RECONCILIATION,
            payload=payload,
            db_path=db_path or str(RESEARCH_VAULT_PATH),
        )
    )


def enqueue_portfolio_constraint(row: dict[str, Any], *, db_path: str | None = None) -> None:
    writer = get_async_db_writer()
    if not writer.is_running:
        writer.start()
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.PORTFOLIO_CONSTRAINT,
            payload=row,
            db_path=db_path or str(RESEARCH_VAULT_PATH),
        )
    )


def enqueue_counterfactual_state(row: dict[str, Any], *, db_path: str | None = None) -> None:
    writer = get_async_db_writer()
    if not writer.is_running:
        writer.start()
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.COUNTERFACTUAL_STATE,
            payload=row,
            db_path=db_path or str(RESEARCH_VAULT_PATH),
        )
    )


def enqueue_portfolio_risk_state(payload: dict[str, Any], *, db_path: str | None = None) -> None:
    from src.config import DB_PATH

    writer = get_async_db_writer()
    if not writer.is_running:
        writer.start()
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.PORTFOLIO_RISK_STATE,
            payload=payload,
            db_path=db_path or str(DB_PATH),
        )
    )


def enqueue_cycle_metrics(payload: dict[str, Any], *, db_path: str | None = None) -> None:
    from src.config import DB_PATH

    writer = get_async_db_writer()
    if not writer.is_running:
        writer.start()
    writer.enqueue(
        QueuedWrite(
            kind=WriteKind.CYCLE_METRICS,
            payload=payload,
            db_path=db_path or str(DB_PATH),
        )
    )


def upsert_dashboard_summary_postgres(
    *,
    environment: str,
    summary: dict[str, Any],
    performance: dict[str, Any],
) -> None:
    settings = PostgresConnectionSettings.from_env()
    if settings is None:
        return
    import psycopg

    with psycopg.connect(settings.dsn(use_pool=True)) as conn:
        with conn.cursor() as cur:
            cur.execute(POSTGRES_SCHEMA_DDL)
            cur.execute(
                """
                INSERT INTO dashboard_summary_snapshots (
                    environment, generated_at, summary_json, performance_json, updated_at
                ) VALUES (%s, NOW(), %s::jsonb, %s::jsonb, NOW())
                ON CONFLICT (environment) DO UPDATE SET
                    generated_at = EXCLUDED.generated_at,
                    summary_json = EXCLUDED.summary_json,
                    performance_json = EXCLUDED.performance_json,
                    updated_at = NOW()
                """,
                (
                    environment,
                    json.dumps(summary, separators=(",", ":")),
                    json.dumps(performance, separators=(",", ":")),
                ),
            )
        conn.commit()


# E3/R2: register the append-only clone-store flush handlers (self-registration).
# Placed at module end so WriteKind + register_write_handler are already defined
# when store_handlers imports back into this module. Import failure must not break
# the writer (the legacy explicit dispatch still covers the complex kinds).
try:  # pragma: no cover - registration wiring
    from src.persistence import store_handlers as _store_handlers  # noqa: F401,E402
except Exception as _exc:  # pragma: no cover
    log.error("write_handler_registration_failed", error=str(_exc))
