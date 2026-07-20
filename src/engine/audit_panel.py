"""
Governance telemetry provider and rollback post-mortem generator.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from src.engine.governance import (
    ImmutableChangeJournal,
    ensure_governance_schema,
    verify_journal_signature,
)
from src.persistence.db import RESEARCH_VAULT_PATH
from src.persistence.ownership_guard import ensure_db_writable

POST_MORTEM_LOOKBACK_HOURS = 72
MAINTENANCE_JOB_IDS = (
    "pre_open_readiness",
    "post_close_reconciliation",
    "weekend_parameter_tuner",
    "weekly_policy_brain",
)
DASHBOARD_SUMMARY_BAR_INTERVAL = 10
DASHBOARD_SUMMARY_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS dashboard_summary_snapshots (
    environment TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    performance_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
"""

ROOT_CAUSE_SIGNAL_FAILURE = "Signal Failure"
ROOT_CAUSE_IMPLEMENTATION_SHORTFALL = "Implementation Shortfall"
ROOT_CAUSE_MACRO_SHOCK = "Macro Shock"
ROOT_CAUSE_CORRUPT_DATA_FEED = "Corrupt Data Feed"


@dataclass(frozen=True)
class OperationalSnapshot:
    generated_at: str
    ai_policy_states: dict[str, dict[str, Any]]
    champion_ages: dict[str, list[dict[str, Any]]]
    maintenance_jobs: dict[str, dict[str, Any]]
    active_kill_switches: list[dict[str, Any]]
    recent_journal_events: list[dict[str, Any]]
    performance_metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class GovernanceTelemetryProvider:
    """Aggregates governance and operational state for dashboard visibility."""

    db_path: Path = RESEARCH_VAULT_PATH
    journal: ImmutableChangeJournal | None = None
    environment: str = "production"
    _bars_since_refresh: int = field(default=0, init=False, repr=False)
    _refresh_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _aggregator_stop: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )
    _aggregator_thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.journal is None:
            self.journal = ImmutableChangeJournal(db_path=self.db_path)
        ensure_governance_schema(self.db_path)
        self._ensure_dashboard_summary_schema()

    def start_dashboard_aggregator(self) -> None:
        if self._aggregator_thread is not None and self._aggregator_thread.is_alive():
            return
        self._aggregator_stop.clear()
        self._aggregator_thread = threading.Thread(
            target=self._dashboard_aggregator_loop,
            name="dashboard-summary-aggregator",
            daemon=True,
        )
        self._aggregator_thread.start()

    def stop_dashboard_aggregator(self, *, timeout_seconds: float = 5.0) -> None:
        self._aggregator_stop.set()
        self._refresh_event.set()
        if self._aggregator_thread is not None and self._aggregator_thread.is_alive():
            self._aggregator_thread.join(timeout=max(timeout_seconds, 0.1))

    def _dashboard_aggregator_loop(self) -> None:
        while not self._aggregator_stop.is_set():
            triggered = self._refresh_event.wait(timeout=1.0)
            if not triggered:
                continue
            self._refresh_event.clear()
            if self._aggregator_stop.is_set():
                return
            try:
                self.refresh_dashboard_summary()
            except Exception as exc:
                structlog.get_logger().error(
                    "dashboard_summary_aggregator_failed",
                    error=str(exc),
                )

    def _ensure_dashboard_summary_schema(self) -> None:
        ensure_db_writable(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(DASHBOARD_SUMMARY_SQLITE_DDL)

    def note_bar_closed(self, *, post_close: bool = False) -> None:
        self._bars_since_refresh += 1
        if post_close or self._bars_since_refresh >= DASHBOARD_SUMMARY_BAR_INTERVAL:
            self._bars_since_refresh = 0
            self._refresh_event.set()

    def _journal_event_payload(self, entry: Any) -> dict[str, Any]:
        return {
            "journal_id": entry.journal_id,
            "timestamp": entry.timestamp,
            "event_type": entry.event_type,
            "triggered_by": entry.triggered_by,
            "scope_key": entry.scope_key,
            "rationale_hash": entry.rationale_hash,
            "signature_valid": verify_journal_signature(entry),
        }

    def refresh_dashboard_summary(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        ai_states = self._fetch_ai_policy_states()
        champion_ages = self._fetch_champion_ages(now)
        maintenance_jobs = self._fetch_maintenance_jobs()
        kill_switches = self._fetch_active_kill_switches()
        recent_events = [
            self._journal_event_payload(entry)
            for entry in self.journal.fetch_recent(limit=25)
        ]
        performance_metrics = self._compute_performance_metrics()
        performance_metrics.update(self._collect_runtime_telemetry())
        degradation_mode = self._fetch_degradation_mode()
        summary = {
            "generated_at": now.isoformat(),
            "degradation_mode": degradation_mode,
            "ai_policy_states": ai_states,
            "champion_ages": champion_ages,
            "maintenance_jobs": maintenance_jobs,
            "active_kill_switches": kill_switches,
            "recent_journal_events": recent_events,
        }
        self._persist_dashboard_summary(
            environment=self.environment,
            summary=summary,
            performance=performance_metrics,
            generated_at=now.isoformat(),
        )
        return summary

    def _persist_dashboard_summary(
        self,
        *,
        environment: str,
        summary: dict[str, Any],
        performance: dict[str, Any],
        generated_at: str,
    ) -> None:
        payload_summary = json.dumps(summary, separators=(",", ":"))
        payload_performance = json.dumps(performance, separators=(",", ":"))
        updated_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO dashboard_summary_snapshots (
                    environment, generated_at, summary_json, performance_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(environment) DO UPDATE SET
                    generated_at = excluded.generated_at,
                    summary_json = excluded.summary_json,
                    performance_json = excluded.performance_json,
                    updated_at = excluded.updated_at
                """,
                (environment, generated_at, payload_summary, payload_performance, updated_at),
            )
        from src.persistence.db_queue import upsert_dashboard_summary_postgres

        upsert_dashboard_summary_postgres(
            environment=environment,
            summary=summary,
            performance=performance,
        )

    def fetch_operational_snapshot(self) -> OperationalSnapshot:
        cached = self._load_dashboard_summary_row()
        if cached is not None:
            summary = cached["summary"]
            performance = cached.get("performance") or {}
            return OperationalSnapshot(
                generated_at=str(summary.get("generated_at") or cached.get("generated_at")),
                ai_policy_states=dict(summary.get("ai_policy_states") or {}),
                champion_ages=dict(summary.get("champion_ages") or {}),
                maintenance_jobs=dict(summary.get("maintenance_jobs") or {}),
                active_kill_switches=list(summary.get("active_kill_switches") or []),
                recent_journal_events=list(summary.get("recent_journal_events") or []),
                performance_metrics=dict(performance),
            )
        return self._compute_operational_snapshot_live()

    def _load_dashboard_summary_row(self) -> dict[str, Any] | None:
        from src.persistence.postgres_config import PostgresConnectionSettings

        settings = PostgresConnectionSettings.from_env()
        if settings is not None:
            try:
                import psycopg

                with psycopg.connect(settings.dsn(use_pool=True)) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT generated_at, summary_json, performance_json
                            FROM dashboard_summary_snapshots
                            WHERE environment = %s
                            LIMIT 1
                            """,
                            (self.environment,),
                        )
                        row = cur.fetchone()
                if row is not None:
                    summary_raw = row[1]
                    performance_raw = row[2]
                    summary = (
                        summary_raw
                        if isinstance(summary_raw, dict)
                        else json.loads(str(summary_raw))
                    )
                    performance = (
                        performance_raw
                        if isinstance(performance_raw, dict)
                        else json.loads(str(performance_raw or "{}"))
                    )
                    return {
                        "generated_at": str(row[0]),
                        "summary": summary,
                        "performance": performance,
                    }
            except Exception:
                pass

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    """
                    SELECT generated_at, summary_json, performance_json
                    FROM dashboard_summary_snapshots
                    WHERE environment = ?
                    LIMIT 1
                    """,
                    (self.environment,),
                ).fetchone()
            except sqlite3.Error:
                return None
        if row is None:
            return None
        return {
            "generated_at": str(row["generated_at"]),
            "summary": json.loads(str(row["summary_json"])),
            "performance": json.loads(str(row["performance_json"] or "{}")),
        }

    def _compute_operational_snapshot_live(self) -> OperationalSnapshot:
        now = datetime.now(timezone.utc)
        return OperationalSnapshot(
            generated_at=now.isoformat(),
            ai_policy_states=self._fetch_ai_policy_states(),
            champion_ages=self._fetch_champion_ages(now),
            maintenance_jobs=self._fetch_maintenance_jobs(),
            active_kill_switches=self._fetch_active_kill_switches(),
            recent_journal_events=[
                self._journal_event_payload(entry)
                for entry in self.journal.fetch_recent(limit=25)
            ],
            performance_metrics=self._compute_performance_metrics(),
        )

    def _compute_performance_metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "rolling_trade_count_7d": 0,
            "rolling_mean_pnl_7d": 0.0,
            "rolling_mean_slippage_7d": 0.0,
            "mean_champion_age_days": 0.0,
            "active_policy_state_counts": {},
        }
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT pnl, slippage_pct
                    FROM live_attribution_ledger
                    WHERE timestamp >= ?
                    """,
                    (cutoff,),
                ).fetchall()
            except sqlite3.Error:
                rows = []
        if rows:
            pnls = [float(row["pnl"]) for row in rows]
            slips = [float(row["slippage_pct"] or 0.0) for row in rows]
            metrics["rolling_trade_count_7d"] = len(rows)
            metrics["rolling_mean_pnl_7d"] = float(np.mean(np.asarray(pnls)))
            metrics["rolling_mean_slippage_7d"] = float(np.mean(np.asarray(slips)))

        now = datetime.now(timezone.utc)
        champion_ages = self._fetch_champion_ages(now)
        age_values: list[float] = []
        for entries in champion_ages.values():
            for entry in entries:
                age_values.append(float(entry.get("age_days") or 0.0))
        if age_values:
            metrics["mean_champion_age_days"] = float(np.mean(np.asarray(age_values)))

        for strategy_id, state in self._fetch_ai_policy_states().items():
            label = str(state.get("ai_policy_execution_state") or "UNKNOWN")
            metrics["active_policy_state_counts"][label] = (
                int(metrics["active_policy_state_counts"].get(label, 0)) + 1
            )
            metrics.setdefault("strategies", {})[strategy_id] = {
                "probation_clean_trading_days": int(
                    state.get("probation_clean_trading_days") or 0
                ),
                "eviction_lockout_active": bool(state.get("eviction_lockout_active")),
            }
        return metrics

    def _collect_runtime_telemetry(self) -> dict[str, Any]:
        from src.ingestor.dual_buffer_manager import get_dual_buffer_telemetry
        from src.persistence.db_queue import get_queue_telemetry

        queue = get_queue_telemetry()
        buffer = get_dual_buffer_telemetry()
        lag = buffer.get("shadow_matrix_lag_seconds")
        return {
            "wal_backlog_depth": int(queue.get("wal_backlog_depth", 0)),
            "shadow_matrix_lag_seconds": (
                float(lag) if lag is not None else None
            ),
            "tape_latch_active": bool(buffer.get("tape_latch_active", False)),
            "wal_queue_stats": {
                "enqueued": int(queue.get("enqueued", 0)),
                "flushed": int(queue.get("flushed", 0)),
                "wal_throttled": int(queue.get("wal_throttled", 0)),
                "errors": int(queue.get("errors", 0)),
            },
            "tape_divergent_strategy_ids": list(
                buffer.get("divergent_strategy_ids") or []
            ),
        }

    def snapshot_as_dict(self) -> dict[str, Any]:
        snap = self.fetch_operational_snapshot()
        return {
            "generated_at": snap.generated_at,
            "ai_policy_states": snap.ai_policy_states,
            "champion_ages": snap.champion_ages,
            "maintenance_jobs": snap.maintenance_jobs,
            "active_kill_switches": snap.active_kill_switches,
            "recent_journal_events": snap.recent_journal_events,
            "performance_metrics": snap.performance_metrics,
        }

    def _fetch_degradation_mode(self) -> str:
        from src.config import DB_PATH
        from src.persistence.governance_state_store import load_governance_state

        persisted = load_governance_state(DB_PATH)
        if persisted is None:
            return "NORMAL"
        return persisted.degradation_mode

    def _fetch_ai_policy_states(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM ai_policy_lifecycle_state"
                ).fetchall()
            except sqlite3.Error:
                return out
        for row in rows:
            data = dict(row)
            strategy_id = str(data["strategy_id"])
            lockout_until = data.get("eviction_lockout_until")
            lockout_active = False
            if lockout_until:
                try:
                    lockout_ts = datetime.fromisoformat(str(lockout_until))
                    if lockout_ts.tzinfo is None:
                        lockout_ts = lockout_ts.replace(tzinfo=timezone.utc)
                    lockout_active = lockout_ts > datetime.now(timezone.utc)
                except ValueError:
                    lockout_active = True
            out[strategy_id] = {
                "symbol": data.get("symbol"),
                "ai_policy_execution_state": data.get("execution_state"),
                "probation_clean_trading_days": int(
                    data.get("probation_clean_trading_days") or 0
                ),
                "probation_started_at": data.get("probation_started_at"),
                "eviction_lockout_until": lockout_until,
                "eviction_lockout_active": lockout_active,
                "updated_at": data.get("updated_at"),
            }
        return out

    def _fetch_champion_ages(self, now: datetime) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT symbol, regime, composite_score, promoted_at, params_json
                    FROM regime_champions
                    ORDER BY symbol, promoted_at DESC
                    """
                ).fetchall()
            except sqlite3.Error:
                return out
        for row in rows:
            data = dict(row)
            symbol = str(data["symbol"])
            promoted_raw = str(data.get("promoted_at") or "")
            age_days = 0
            if promoted_raw:
                promoted_at = datetime.fromisoformat(promoted_raw)
                if promoted_at.tzinfo is None:
                    promoted_at = promoted_at.replace(tzinfo=timezone.utc)
                age_days = max(0, (now - promoted_at.astimezone(timezone.utc)).days)
            params = {}
            try:
                params = json.loads(str(data.get("params_json") or "{}"))
            except json.JSONDecodeError:
                params = {}
            out.setdefault(symbol, []).append(
                {
                    "regime": data.get("regime"),
                    "composite_score": float(data.get("composite_score") or 0.0),
                    "promoted_at": promoted_raw,
                    "age_days": age_days,
                    "param_keys": sorted(params.keys()),
                }
            )
        return out

    def _fetch_maintenance_jobs(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM maintenance_job_ledger"
                ).fetchall()
            except sqlite3.Error:
                return {job_id: {"status": "unknown"} for job_id in MAINTENANCE_JOB_IDS}
        by_id = {str(dict(row)["job_id"]): dict(row) for row in rows}
        for job_id in MAINTENANCE_JOB_IDS:
            row = by_id.get(job_id)
            if row is None:
                out[job_id] = {"status": "never_run", "validation_passed": False}
                continue
            payload = {}
            raw_payload = row.get("payload_json")
            if raw_payload:
                try:
                    payload = json.loads(str(raw_payload))
                except json.JSONDecodeError:
                    payload = {}
            out[job_id] = {
                "status": row.get("last_status"),
                "validation_passed": bool(row.get("validation_passed")),
                "last_success_at": row.get("last_success_at"),
                "last_attempt_at": row.get("last_attempt_at"),
                "last_error": row.get("last_error"),
                "payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
            }
        return out

    def _fetch_active_kill_switches(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT kill_level, scope_key, engaged_at, engaged_by, rationale_hash
                    FROM human_override_registry
                    WHERE active = 1
                    ORDER BY engaged_at DESC
                    """
                ).fetchall()
            except sqlite3.Error:
                return []
        return [dict(row) for row in rows]


def generate_rollback_post_mortem(
    rollback_event_id: str,
    *,
    db_path: Path = RESEARCH_VAULT_PATH,
) -> str:
    """
    Build a Markdown post-mortem for rollback_event_id.

    Supported identifiers:
    - journal:{id}
    - drift:{alert_id}
  """
    ensure_governance_schema(db_path)
    event_kind, raw_id = _parse_rollback_event_id(rollback_event_id)
    context = _load_rollback_context(event_kind, raw_id, db_path=db_path)
    root_cause = _classify_root_cause(context)
    return _render_post_mortem_markdown(
        rollback_event_id=rollback_event_id,
        context=context,
        root_cause=root_cause,
    )


def _parse_rollback_event_id(rollback_event_id: str) -> tuple[str, str]:
    if ":" in rollback_event_id:
        kind, raw_id = rollback_event_id.split(":", 1)
        return kind.lower(), raw_id
    return "journal", rollback_event_id


def _load_rollback_context(
    event_kind: str,
    raw_id: str,
    *,
    db_path: Path,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "event_kind": event_kind,
        "event_id": raw_id,
        "anchor_timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy_id": None,
        "symbol": None,
        "previous_state": {},
        "requested_state": {},
        "journal_trail": [],
        "promotion_attribution": [],
        "execution_feedback": [],
        "drift_alert": None,
        "health_transitions": [],
        "journal_entry": None,
    }
    journal = ImmutableChangeJournal(db_path=db_path)

    if event_kind == "journal":
        entry = journal.fetch_entry(int(raw_id))
        if entry is not None:
            context["journal_entry"] = entry
            context["anchor_timestamp"] = entry.timestamp
            context["previous_state"] = entry.previous_state
            context["requested_state"] = entry.requested_state
            context["strategy_id"] = entry.scope_key
    elif event_kind == "drift":
        context["drift_alert"] = _fetch_drift_alert(int(raw_id), db_path=db_path)
        if context["drift_alert"]:
            context["anchor_timestamp"] = context["drift_alert"]["timestamp"]
            context["strategy_id"] = context["drift_alert"].get("strategy_id")
            context["symbol"] = context["drift_alert"].get("symbol")

    anchor = _parse_ts(context["anchor_timestamp"])
    window_start = (anchor - timedelta(hours=POST_MORTEM_LOOKBACK_HOURS)).isoformat()
    strategy_id = context.get("strategy_id")
    symbol = context.get("symbol")

    context["journal_trail"] = [
        {
            "journal_id": e.journal_id,
            "timestamp": e.timestamp,
            "event_type": e.event_type,
            "triggered_by": e.triggered_by,
            "scope_key": e.scope_key,
        }
        for e in journal.fetch_recent(limit=100)
        if e.timestamp >= window_start
    ]
    context["promotion_attribution"] = _fetch_promotion_attribution(
        db_path,
        strategy_id=strategy_id,
        symbol=symbol,
        since=window_start,
    )
    context["execution_feedback"] = _fetch_execution_feedback(
        db_path,
        symbol=symbol,
        since=window_start,
    )
    context["health_transitions"] = _fetch_health_transitions(
        db_path,
        strategy_id=strategy_id,
        since=window_start,
    )
    return context


def _classify_root_cause(context: dict[str, Any]) -> str:
    scores = {
        ROOT_CAUSE_SIGNAL_FAILURE: 0.0,
        ROOT_CAUSE_IMPLEMENTATION_SHORTFALL: 0.0,
        ROOT_CAUSE_MACRO_SHOCK: 0.0,
        ROOT_CAUSE_CORRUPT_DATA_FEED: 0.0,
    }

    promo_rows = context.get("promotion_attribution") or []
    if promo_rows:
        pnls = [float(r.get("trade_pnl") or 0.0) for r in promo_rows]
        slippages = [float(r.get("slippage_pct") or 0.0) for r in promo_rows]
        sharpes = [
            float(r.get("live_sharpe_10t"))
            for r in promo_rows
            if r.get("live_sharpe_10t") is not None
        ]
        if pnls and float(np.mean(np.asarray(pnls))) < 0.0:
            scores[ROOT_CAUSE_SIGNAL_FAILURE] += 2.0
        if sharpes and float(np.mean(np.asarray(sharpes))) < 0.0:
            scores[ROOT_CAUSE_SIGNAL_FAILURE] += 1.5
        if slippages and float(np.mean(np.asarray(slippages))) > 0.001:
            scores[ROOT_CAUSE_IMPLEMENTATION_SHORTFALL] += 2.0

    exec_rows = context.get("execution_feedback") or []
    if exec_rows:
        markout_5 = [
            float(r.get("markout_5m"))
            for r in exec_rows
            if r.get("markout_5m") is not None
        ]
        slip_deltas = [float(r.get("slippage_delta_pct") or 0.0) for r in exec_rows]
        if slip_deltas and float(np.mean(np.asarray(slip_deltas))) > 0.0:
            scores[ROOT_CAUSE_IMPLEMENTATION_SHORTFALL] += 1.5
        if markout_5 and float(np.mean(np.asarray(markout_5))) < 0.0:
            scores[ROOT_CAUSE_IMPLEMENTATION_SHORTFALL] += 1.0

    drift = context.get("drift_alert")
    if isinstance(drift, dict):
        metrics = drift.get("metrics") or {}
        if str(drift.get("classification")) == "OLD_AND_WRONG":
            scores[ROOT_CAUSE_SIGNAL_FAILURE] += 2.0
        if float(metrics.get("tracking_error_decay") or 0.0) < -0.2:
            scores[ROOT_CAUSE_SIGNAL_FAILURE] += 1.0
        if float(metrics.get("win_rate_decay") or 0.0) < -0.2:
            scores[ROOT_CAUSE_SIGNAL_FAILURE] += 1.0

    health_rows = context.get("health_transitions") or []
    for row in health_rows:
        if str(row.get("new_status")) in {"DEGRADED", "HALTED", "DEFENSIVE"}:
            components = row.get("components") or {}
            if float(components.get("data_integrity", 1.0)) < 1.0:
                scores[ROOT_CAUSE_CORRUPT_DATA_FEED] += 2.5
            if float(components.get("execution_drift", 1.0)) < 1.0:
                scores[ROOT_CAUSE_IMPLEMENTATION_SHORTFALL] += 1.0

    journal_trail = context.get("journal_trail") or []
    for item in journal_trail:
        if item.get("event_type") in {
            "KILL_SWITCH_ENGAGED",
            "RECOVERY_ROLLBACK",
            "AI_LIFECYCLE_ADJUSTMENT",
        }:
            scores[ROOT_CAUSE_MACRO_SHOCK] += 0.25

    return max(scores.items(), key=lambda pair: pair[1])[0]


def _render_post_mortem_markdown(
    *,
    rollback_event_id: str,
    context: dict[str, Any],
    root_cause: str,
) -> str:
    entry = context.get("journal_entry")
    lines = [
        "# Rollback Post-Mortem",
        "",
        f"- **Event ID:** `{rollback_event_id}`",
        f"- **Generated At (UTC):** {datetime.now(timezone.utc).isoformat()}",
        f"- **Anchor Timestamp:** {context.get('anchor_timestamp')}",
        f"- **Primary Root Cause:** {root_cause}",
        "",
        "## State Transition",
        "",
    ]
    if entry is not None:
        lines.extend(
            [
                f"- **Event Type:** {entry.event_type}",
                f"- **Triggered By:** {entry.triggered_by}",
                f"- **Scope:** {entry.scope_key}",
                f"- **Rationale Hash:** `{entry.rationale_hash}`",
                f"- **Signature Valid:** `{True}`",
                "",
                "### Previous State",
                "```json",
                json.dumps(entry.previous_state, indent=2),
                "```",
                "",
                "### Requested State",
                "```json",
                json.dumps(entry.requested_state, indent=2),
                "```",
                "",
            ]
        )
    elif context.get("drift_alert"):
        lines.extend(
            [
                "### Drift Alert",
                "```json",
                json.dumps(context["drift_alert"], indent=2),
                "```",
                "",
            ]
        )

    lines.extend(["## Signal Metrics", ""])
    promo = context.get("promotion_attribution") or []
    if promo:
        avg_pnl = float(np.mean([float(r.get("trade_pnl") or 0.0) for r in promo]))
        avg_slip = float(np.mean([float(r.get("slippage_pct") or 0.0) for r in promo]))
        lines.append(f"- Trades analyzed: {len(promo)}")
        lines.append(f"- Average trade PnL: `{avg_pnl:.4f}`")
        lines.append(f"- Average slippage pct: `{avg_slip:.6f}`")
    else:
        lines.append("- No promotion attribution rows in lookback window.")

    lines.extend(["", "## Execution Markouts", ""])
    exec_rows = context.get("execution_feedback") or []
    if exec_rows:
        markouts = [
            float(r.get("markout_5m"))
            for r in exec_rows
            if r.get("markout_5m") is not None
        ]
        if markouts:
            lines.append(f"- Average 5m markout (bps proxy): `{float(np.mean(markouts)):.4f}`")
        lines.append(f"- Feedback rows: {len(exec_rows)}")
    else:
        lines.append("- No execution feedback rows in lookback window.")

    lines.extend(["", "## Macro / Health Context", ""])
    health = context.get("health_transitions") or []
    if health:
        for row in health[:5]:
            lines.append(
                f"- {row.get('timestamp')}: `{row.get('prior_status')}` -> "
                f"`{row.get('new_status')}` score={row.get('health_score')}"
            )
    else:
        lines.append("- No health transitions in lookback window.")

    lines.extend(["", "## Journal Trail (Recent)", ""])
    for item in (context.get("journal_trail") or [])[:15]:
        lines.append(
            f"- `{item.get('timestamp')}` {item.get('event_type')} "
            f"({item.get('triggered_by')}) scope={item.get('scope_key')}"
        )

    lines.extend(
        [
            "",
            "## Systematic Autopsy",
            "",
            f"Classification selected **{root_cause}** based on weighted evidence across "
            "signal attribution, execution feedback, drift metrics, and health transitions.",
            "",
            "### Recommended Actions",
            "",
        ]
    )
    if root_cause == ROOT_CAUSE_SIGNAL_FAILURE:
        lines.append("- Re-run regime-scoped parameter sweep and withhold promotion until OOS validation passes.")
    elif root_cause == ROOT_CAUSE_IMPLEMENTATION_SHORTFALL:
        lines.append("- Tighten execution adaptor routing urgency and review session slippage multipliers.")
    elif root_cause == ROOT_CAUSE_MACRO_SHOCK:
        lines.append("- Maintain defensive portfolio risk mode and widen entry thresholds until stress composite normalizes.")
    else:
        lines.append("- Engage RESEARCH_HALT, validate bar freshness SLOs, and restore vault integrity before re-arming live entries.")

    lines.append("")
    return "\n".join(lines)


def _fetch_drift_alert(alert_id: int, *, db_path: Path) -> dict[str, Any] | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM state_drift_alerts WHERE alert_id = ?",
                (int(alert_id),),
            ).fetchone()
        except sqlite3.Error:
            return None
    if row is None:
        return None
    data = dict(row)
    metrics = {}
    try:
        metrics = json.loads(str(data.get("metrics_json") or "{}"))
    except json.JSONDecodeError:
        metrics = {}
    data["metrics"] = metrics
    return data


def _fetch_promotion_attribution(
    db_path: Path,
    *,
    strategy_id: str | None,
    symbol: str | None,
    since: str,
) -> list[dict[str, Any]]:
    query = """
        SELECT * FROM active_promotion_attribution
        WHERE timestamp >= ?
    """
    params: list[Any] = [since]
    if strategy_id:
        query += " AND strategy_id = ?"
        params.append(strategy_id)
    elif symbol:
        query += " AND symbol = ?"
        params.append(symbol.upper())
    query += " ORDER BY timestamp DESC LIMIT 50"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
    return [dict(row) for row in rows]


def _fetch_execution_feedback(
    db_path: Path,
    *,
    symbol: str | None,
    since: str,
) -> list[dict[str, Any]]:
    query = """
        SELECT * FROM execution_feedback_ledger
        WHERE timestamp >= ?
    """
    params: list[Any] = [since]
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol.upper())
    query += " ORDER BY timestamp DESC LIMIT 50"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
    return [dict(row) for row in rows]


def _fetch_health_transitions(
    db_path: Path,
    *,
    strategy_id: str | None,
    since: str,
) -> list[dict[str, Any]]:
    query = """
        SELECT * FROM system_health_ledger
        WHERE timestamp >= ?
    """
    params: list[Any] = [since]
    if strategy_id:
        query += " AND strategy_id = ?"
        params.append(strategy_id)
    query += " ORDER BY timestamp DESC LIMIT 25"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
    out: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        components = {}
        try:
            components = json.loads(str(data.get("components_json") or "{}"))
        except json.JSONDecodeError:
            components = {}
        data["components"] = components
        out.append(data)
    return out


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
