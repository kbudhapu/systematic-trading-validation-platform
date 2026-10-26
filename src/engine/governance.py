"""
Immutable change journal and human override kill-switch registry.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import structlog

from src.config import DATA_DIR, ROOT
from src.persistence.db import RESEARCH_VAULT_PATH
from src.persistence.ownership_guard import ensure_db_writable
from src.router.risk_manager import AI_POLICY_PASSIVE_SHADOW

log = structlog.get_logger()


class KillSwitchReleaseError(RuntimeError):
    """Raised when a kill-switch release fails to persist to the DB.

    A governance API must NEVER report unverified success: if the release UPDATE
    did not stick (the halt is still active on a fresh read), callers would
    believe trading is unblocked when it is not. This converts that silent
    write-loss into a loud failure at the release seam (the R1 defect)."""


CIRCUIT_BREAKER_FILE = DATA_DIR / "circuit_breaker_state.json"
GOVERNANCE_SIGNING_ENV = "GOVERNANCE_SIGNING_KEY"

EVENT_PARAMETER_UPDATE = "PARAMETER_UPDATE"
EVENT_CHAMPION_PROMOTION = "CHAMPION_PROMOTION"
EVENT_AI_LIFECYCLE_ADJUSTMENT = "AI_LIFECYCLE_ADJUSTMENT"
EVENT_MANUAL_CONFIG_OVERRIDE = "MANUAL_CONFIG_OVERRIDE"
EVENT_RECOVERY_ROLLBACK = "RECOVERY_ROLLBACK"
EVENT_KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
EVENT_KILL_SWITCH_RELEASED = "KILL_SWITCH_RELEASED"
EVENT_STATE_RECON_RECOVERY = "STATE_RECON_RECOVERY"
EVENT_PRE_MIGRATION_VALIDATION = "PRE_MIGRATION_VALIDATION"
EVENT_PRE_MIGRATION_VALIDATION_REJECTED = "PRE_MIGRATION_VALIDATION_REJECTED"
EVENT_RESEARCH_ARTIFACT_REFRESH = "RESEARCH_ARTIFACT_REFRESH"

IMMUTABLE_CHANGE_JOURNAL_DDL = """
CREATE TABLE IF NOT EXISTS immutable_change_journal (
    journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    scope_key TEXT NOT NULL DEFAULT 'GLOBAL',
    previous_state_json TEXT NOT NULL,
    requested_state_json TEXT NOT NULL,
    rationale_hash TEXT NOT NULL,
    payload_signature TEXT NOT NULL,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_journal_ts
    ON immutable_change_journal(timestamp);
CREATE INDEX IF NOT EXISTS idx_change_journal_event
    ON immutable_change_journal(event_type, timestamp);
CREATE TRIGGER IF NOT EXISTS immutable_change_journal_no_update
BEFORE UPDATE ON immutable_change_journal
BEGIN
    SELECT RAISE(ABORT, 'immutable_change_journal is append-only');
END;
CREATE TRIGGER IF NOT EXISTS immutable_change_journal_no_delete
BEFORE DELETE ON immutable_change_journal
BEGIN
    SELECT RAISE(ABORT, 'immutable_change_journal is append-only');
END;
"""

HUMAN_OVERRIDE_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS human_override_registry (
    kill_level TEXT NOT NULL,
    scope_key TEXT NOT NULL DEFAULT 'GLOBAL',
    active INTEGER NOT NULL DEFAULT 0,
    engaged_at TEXT,
    engaged_by TEXT,
    rationale_hash TEXT,
    metadata_json TEXT,
    PRIMARY KEY (kill_level, scope_key)
);
CREATE INDEX IF NOT EXISTS idx_human_override_active
    ON human_override_registry(active, kill_level);
"""

CONTROL_PLANE_LATCHES_DDL = """
CREATE TABLE IF NOT EXISTS control_plane_latches (
    latch_key TEXT PRIMARY KEY,
    active INTEGER NOT NULL,
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

PRE_FLIGHT_RECON_LATCH_KEY = "PRE_FLIGHT_RECON_LOCK"


class TriggeredBy(str, Enum):
    SYSTEM_AUTOMATIC = "SYSTEM_AUTOMATIC"
    HUMAN_OPERATOR = "HUMAN_OPERATOR"


# E3 (CP5-b): the journal's triggered_by was HARDCODED to HUMAN_OPERATOR even for automatic
# engagements (RISK_ESCALATION, CAPACITY_GOVERNOR, degradation), so an auto-flatten was recorded
# as a human kill. Derive the actor from the trigger source instead. These substrings mark a
# SYSTEM source; anything else (an operator email/name from a dashboard/API command) is HUMAN.
_SYSTEM_OPERATOR_MARKERS = (
    "risk_escalation", "governor", "system", "automatic", "degrad", "preempt",
    "boot_recon", "reconcil", "watchdog", "kill_switch", "capacity",
)


def _actor_from_operator(operator: str) -> TriggeredBy:
    op = (operator or "").strip().lower()
    if any(marker in op for marker in _SYSTEM_OPERATOR_MARKERS):
        return TriggeredBy.SYSTEM_AUTOMATIC
    return TriggeredBy.HUMAN_OPERATOR


class KillLevel(str, Enum):
    STRATEGY_HALT = "STRATEGY_HALT"
    PORTFOLIO_HALT = "PORTFOLIO_HALT"
    RESEARCH_HALT = "RESEARCH_HALT"
    AI_HALT = "AI_HALT"


@dataclass(frozen=True)
class ChangeJournalEntry:
    journal_id: int
    timestamp: str
    event_type: str
    triggered_by: str
    scope_key: str
    previous_state: dict[str, Any]
    requested_state: dict[str, Any]
    rationale_hash: str
    payload_signature: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class KillSwitchState:
    kill_level: KillLevel
    scope_key: str
    active: bool
    engaged_at: str | None
    engaged_by: str | None
    rationale_hash: str | None


@dataclass(frozen=True)
class OverrideSnapshot:
    strategy_halts: frozenset[str]
    portfolio_halt: bool
    research_halt: bool
    ai_halt: bool
    states: tuple[KillSwitchState, ...]


def ensure_governance_schema(db_path: Path = RESEARCH_VAULT_PATH) -> None:
    ensure_db_writable(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(IMMUTABLE_CHANGE_JOURNAL_DDL)
        conn.executescript(HUMAN_OVERRIDE_REGISTRY_DDL)
        conn.executescript(CONTROL_PLANE_LATCHES_DDL)


def hash_rationale(rationale: str) -> str:
    return hashlib.sha256(rationale.encode("utf-8")).hexdigest()


def _signing_key() -> bytes:
    material = os.getenv(GOVERNANCE_SIGNING_ENV, "local-dev-governance-signing-key")
    return material.encode("utf-8")


def sign_journal_payload(
    *,
    timestamp: str,
    event_type: str,
    triggered_by: str,
    scope_key: str,
    previous_state: Mapping[str, Any],
    requested_state: Mapping[str, Any],
    rationale_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "timestamp": timestamp,
            "event_type": event_type,
            "triggered_by": triggered_by,
            "scope_key": scope_key,
            "previous_state": dict(previous_state),
            "requested_state": dict(requested_state),
            "rationale_hash": rationale_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hmac.new(
        _signing_key(),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_journal_signature(entry: ChangeJournalEntry) -> bool:
    expected = sign_journal_payload(
        timestamp=entry.timestamp,
        event_type=entry.event_type,
        triggered_by=entry.triggered_by,
        scope_key=entry.scope_key,
        previous_state=entry.previous_state,
        requested_state=entry.requested_state,
        rationale_hash=entry.rationale_hash,
    )
    return hmac.compare_digest(expected, entry.payload_signature)


def is_production_environment(environment: str) -> bool:
    normalized = str(environment or "").strip().lower()
    return normalized in {"live", "production"}


def validate_production_governance_journal(
    journal: ImmutableChangeJournal,
    *,
    environment: str,
    scan_limit: int = 500,
) -> None:
    """
    Verify signing key presence and journal integrity before live execution.

    Raises RuntimeError on production misconfiguration or signature tampering.
    """
    if not is_production_environment(environment):
        return
    signing_key = os.getenv(GOVERNANCE_SIGNING_ENV, "").strip()
    if not signing_key:
        raise RuntimeError(
            f"{GOVERNANCE_SIGNING_ENV} must be configured for production governance"
        )
    entries = journal.fetch_recent(limit=max(int(scan_limit), 1))
    for entry in entries:
        if not verify_journal_signature(entry):
            raise RuntimeError(
                "immutable change journal signature verification failed "
                f"for journal_id={entry.journal_id}"
            )


@dataclass
class ImmutableChangeJournal:
    """Append-only audit trail for configuration and lifecycle mutations."""

    db_path: Path = RESEARCH_VAULT_PATH

    def append(
        self,
        *,
        event_type: str,
        triggered_by: TriggeredBy | str,
        previous_state: Mapping[str, Any],
        requested_state: Mapping[str, Any],
        rationale: str,
        scope_key: str = "GLOBAL",
        metadata: Mapping[str, Any] | None = None,
    ) -> ChangeJournalEntry:
        ensure_governance_schema(self.db_path)
        timestamp = datetime.now(timezone.utc).isoformat()
        rationale_hash = hash_rationale(rationale)
        triggered = (
            triggered_by.value
            if isinstance(triggered_by, TriggeredBy)
            else str(triggered_by)
        )
        signature = sign_journal_payload(
            timestamp=timestamp,
            event_type=event_type,
            triggered_by=triggered,
            scope_key=scope_key,
            previous_state=previous_state,
            requested_state=requested_state,
            rationale_hash=rationale_hash,
        )
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO immutable_change_journal (
                    timestamp, event_type, triggered_by, scope_key,
                    previous_state_json, requested_state_json,
                    rationale_hash, payload_signature, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    event_type,
                    triggered,
                    scope_key,
                    json.dumps(dict(previous_state), separators=(",", ":")),
                    json.dumps(dict(requested_state), separators=(",", ":")),
                    rationale_hash,
                    signature,
                    json.dumps(dict(metadata or {}), separators=(",", ":")),
                ),
            )
            journal_id = int(cur.lastrowid)
        entry = ChangeJournalEntry(
            journal_id=journal_id,
            timestamp=timestamp,
            event_type=event_type,
            triggered_by=triggered,
            scope_key=scope_key,
            previous_state=dict(previous_state),
            requested_state=dict(requested_state),
            rationale_hash=rationale_hash,
            payload_signature=signature,
            metadata=dict(metadata or {}),
        )
        if not verify_journal_signature(entry):
            raise RuntimeError("journal signature verification failed after insert")
        return entry

    def record_parameter_update(
        self,
        *,
        strategy_id: str,
        previous_params: Mapping[str, Any],
        requested_params: Mapping[str, Any],
        rationale: str,
        triggered_by: TriggeredBy = TriggeredBy.SYSTEM_AUTOMATIC,
        metadata: Mapping[str, Any] | None = None,
    ) -> ChangeJournalEntry:
        return self.append(
            event_type=EVENT_PARAMETER_UPDATE,
            triggered_by=triggered_by,
            scope_key=strategy_id,
            previous_state={"params": dict(previous_params)},
            requested_state={"params": dict(requested_params)},
            rationale=rationale,
            metadata=metadata,
        )

    def record_champion_promotion(
        self,
        *,
        symbol: str,
        regime: str,
        previous_champion: Mapping[str, Any] | None,
        requested_champion: Mapping[str, Any],
        rationale: str,
        triggered_by: TriggeredBy = TriggeredBy.SYSTEM_AUTOMATIC,
    ) -> ChangeJournalEntry:
        return self.append(
            event_type=EVENT_CHAMPION_PROMOTION,
            triggered_by=triggered_by,
            scope_key=f"{symbol.upper()}:{regime}",
            previous_state=dict(previous_champion or {}),
            requested_state=dict(requested_champion),
            rationale=rationale,
        )

    def record_ai_lifecycle_adjustment(
        self,
        *,
        strategy_id: str,
        previous_state: Mapping[str, Any],
        requested_state: Mapping[str, Any],
        rationale: str,
        triggered_by: TriggeredBy = TriggeredBy.SYSTEM_AUTOMATIC,
    ) -> ChangeJournalEntry:
        return self.append(
            event_type=EVENT_AI_LIFECYCLE_ADJUSTMENT,
            triggered_by=triggered_by,
            scope_key=strategy_id,
            previous_state=dict(previous_state),
            requested_state=dict(requested_state),
            rationale=rationale,
        )

    def record_manual_override(
        self,
        *,
        scope_key: str,
        previous_state: Mapping[str, Any],
        requested_state: Mapping[str, Any],
        rationale: str,
        operator: str,
    ) -> ChangeJournalEntry:
        return self.append(
            event_type=EVENT_MANUAL_CONFIG_OVERRIDE,
            triggered_by=TriggeredBy.HUMAN_OPERATOR,
            scope_key=scope_key,
            previous_state=dict(previous_state),
            requested_state=dict(requested_state),
            rationale=rationale,
            metadata={"operator": operator},
        )

    def record_recovery_rollback(
        self,
        *,
        scope_key: str,
        previous_state: Mapping[str, Any],
        requested_state: Mapping[str, Any],
        rationale: str,
        triggered_by: TriggeredBy = TriggeredBy.SYSTEM_AUTOMATIC,
        metadata: Mapping[str, Any] | None = None,
    ) -> ChangeJournalEntry:
        return self.append(
            event_type=EVENT_RECOVERY_ROLLBACK,
            triggered_by=triggered_by,
            scope_key=scope_key,
            previous_state=dict(previous_state),
            requested_state=dict(requested_state),
            rationale=rationale,
            metadata=metadata,
        )

    def fetch_entry(self, journal_id: int) -> ChangeJournalEntry | None:
        ensure_governance_schema(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM immutable_change_journal WHERE journal_id = ?",
                (int(journal_id),),
            ).fetchone()
        if row is None:
            return None
        return _row_to_journal_entry(dict(row))

    def fetch_recent(
        self,
        *,
        limit: int = 50,
        event_type: str | None = None,
        scope_key: str | None = None,
    ) -> list[ChangeJournalEntry]:
        ensure_governance_schema(self.db_path)
        query = "SELECT * FROM immutable_change_journal"
        clauses: list[str] = []
        params: list[Any] = []
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if scope_key:
            clauses.append("scope_key = ?")
            params.append(scope_key)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY journal_id DESC LIMIT ?"
        params.append(int(limit))
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [_row_to_journal_entry(dict(row)) for row in rows]


@dataclass
class HumanOverrideRegistry:
    """Global circuit-breaker registry backed by vault table and state file."""

    db_path: Path = RESEARCH_VAULT_PATH
    state_file: Path = CIRCUIT_BREAKER_FILE
    journal: ImmutableChangeJournal | None = None

    def __post_init__(self) -> None:
        if self.journal is None:
            self.journal = ImmutableChangeJournal(db_path=self.db_path)
        ensure_governance_schema(self.db_path)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def engage(
        self,
        kill_level: KillLevel,
        *,
        scope_key: str = "GLOBAL",
        operator: str,
        rationale: str,
        metadata: Mapping[str, Any] | None = None,
        actor: TriggeredBy | None = None,
    ) -> KillSwitchState:
        ensure_governance_schema(self.db_path)
        prior = self.get_state(kill_level, scope_key=scope_key)
        rationale_hash = hash_rationale(rationale)
        engaged_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO human_override_registry (
                    kill_level, scope_key, active, engaged_at,
                    engaged_by, rationale_hash, metadata_json
                ) VALUES (?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(kill_level, scope_key) DO UPDATE SET
                    active = 1,
                    engaged_at = excluded.engaged_at,
                    engaged_by = excluded.engaged_by,
                    rationale_hash = excluded.rationale_hash,
                    metadata_json = excluded.metadata_json
                """,
                (
                    kill_level.value,
                    scope_key,
                    engaged_at,
                    operator,
                    rationale_hash,
                    json.dumps(dict(metadata or {}), separators=(",", ":")),
                ),
            )
        self._sync_state_file()
        self.journal.append(
            event_type=EVENT_KILL_SWITCH_ENGAGED,
            triggered_by=actor if actor is not None else _actor_from_operator(operator),
            scope_key=f"{kill_level.value}:{scope_key}",
            previous_state={"active": prior.active},
            requested_state={"active": True, "operator": operator},
            rationale=rationale,
            metadata=metadata,
        )
        return KillSwitchState(
            kill_level=kill_level,
            scope_key=scope_key,
            active=True,
            engaged_at=engaged_at,
            engaged_by=operator,
            rationale_hash=rationale_hash,
        )

    def release(
        self,
        kill_level: KillLevel,
        *,
        scope_key: str = "GLOBAL",
        operator: str,
        rationale: str,
        actor: TriggeredBy | None = None,
    ) -> KillSwitchState:
        ensure_governance_schema(self.db_path)
        prior = self.get_state(kill_level, scope_key=scope_key)
        # R1: a governance API must NEVER report unverified success. Check rowcount,
        # commit explicitly, and mirror to the state file (DB-direct) BEFORE journaling
        # -- so a subsequent snapshot()/hydrate cannot re-activate the released halt.
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                UPDATE human_override_registry
                SET active = 0
                WHERE kill_level = ? AND scope_key = ?
                """,
                (kill_level.value, scope_key),
            )
            conn.commit()
            updated = int(cur.rowcount or 0)
        self._sync_state_file()
        if updated == 0:
            log.warning(
                "kill_switch_release_no_row",
                kill_level=kill_level.value, scope_key=scope_key,
                note="no matching override row to release (nothing was active)",
            )
        elif not prior.active:
            # Row existed but was already released -> idempotent no-op. Do NOT
            # append a duplicate release journal row for an already-open state.
            log.info(
                "kill_switch_release_noop_already_released",
                kill_level=kill_level.value, scope_key=scope_key,
            )
        else:
            self.journal.append(
                event_type=EVENT_KILL_SWITCH_RELEASED,
                triggered_by=actor if actor is not None else _actor_from_operator(operator),
                scope_key=f"{kill_level.value}:{scope_key}",
                previous_state={"active": prior.active},
                requested_state={"active": False, "operator": operator},
                rationale=rationale,
            )
        # Verify persistence on a FRESH connection -- fail loudly if the halt is
        # still active (never silently report success on a write that didn't stick).
        if self.get_state(kill_level, scope_key=scope_key).active:
            raise KillSwitchReleaseError(
                f"kill-switch release did not persist: {kill_level.value}:{scope_key} "
                f"still active after release (rowcount={updated})"
            )
        return KillSwitchState(
            kill_level=kill_level,
            scope_key=scope_key,
            active=False,
            engaged_at=prior.engaged_at,
            engaged_by=operator,
            rationale_hash=hash_rationale(rationale),
        )

    def get_state(
        self,
        kill_level: KillLevel,
        *,
        scope_key: str = "GLOBAL",
    ) -> KillSwitchState:
        ensure_governance_schema(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM human_override_registry
                WHERE kill_level = ? AND scope_key = ?
                """,
                (kill_level.value, scope_key),
            ).fetchone()
        if row is None:
            return KillSwitchState(
                kill_level=kill_level,
                scope_key=scope_key,
                active=False,
                engaged_at=None,
                engaged_by=None,
                rationale_hash=None,
            )
        data = dict(row)
        return KillSwitchState(
            kill_level=kill_level,
            scope_key=scope_key,
            active=bool(data.get("active")),
            engaged_at=data.get("engaged_at"),
            engaged_by=data.get("engaged_by"),
            rationale_hash=data.get("rationale_hash"),
        )

    def snapshot(self) -> OverrideSnapshot:
        self._hydrate_from_state_file()
        return self._read_db_snapshot()

    def _read_db_snapshot(self) -> OverrideSnapshot:
        """Build the snapshot from the DB ONLY -- no state-file hydration. The
        DB->file mirror (`_sync_state_file`) uses this so it never runs the hydrate
        step, which would re-activate a just-released halt from the stale state file
        (the R1 `release()` persistence-bug root cause: DB UPDATE -> _sync_state_file
        -> snapshot -> _hydrate_from_state_file -> re-seed active=1)."""
        states: list[KillSwitchState] = []
        strategy_halts: set[str] = set()
        portfolio_halt = False
        research_halt = False
        ai_halt = False
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM human_override_registry
                WHERE active = 1
                """
            ).fetchall()
        for row in rows:
            data = dict(row)
            level = KillLevel(str(data["kill_level"]))
            scope = str(data.get("scope_key") or "GLOBAL")
            state = KillSwitchState(
                kill_level=level,
                scope_key=scope,
                active=True,
                engaged_at=data.get("engaged_at"),
                engaged_by=data.get("engaged_by"),
                rationale_hash=data.get("rationale_hash"),
            )
            states.append(state)
            if level == KillLevel.STRATEGY_HALT and scope != "GLOBAL":
                strategy_halts.add(scope)
            elif level == KillLevel.PORTFOLIO_HALT:
                portfolio_halt = True
            elif level == KillLevel.RESEARCH_HALT:
                research_halt = True
            elif level == KillLevel.AI_HALT:
                ai_halt = True
        return OverrideSnapshot(
            strategy_halts=frozenset(strategy_halts),
            portfolio_halt=portfolio_halt,
            research_halt=research_halt,
            ai_halt=ai_halt,
            states=tuple(states),
        )

    def is_strategy_halted(self, strategy_id: str) -> bool:
        snap = self.snapshot()
        if snap.portfolio_halt:
            return True
        return strategy_id in snap.strategy_halts or (
            "GLOBAL" in snap.strategy_halts
        )

    def is_portfolio_halted(self) -> bool:
        return self.snapshot().portfolio_halt

    def is_research_halted(self) -> bool:
        return self.snapshot().research_halt

    def is_ai_halted(self) -> bool:
        return self.snapshot().ai_halt

    def apply_ai_halt_to_params(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.is_ai_halted():
            return params
        adjusted = dict(params)
        adjusted["ai_policy_execution_state"] = AI_POLICY_PASSIVE_SHADOW
        adjusted.pop("_shadow_action_live", None)
        adjusted["ai_halt_active"] = True
        return adjusted

    def _sync_state_file(self) -> None:
        # DB->file MIRROR: read the DB directly (NOT snapshot(), which hydrates from
        # the file). Using snapshot() here created the release() revert loop.
        snap = self._read_db_snapshot()
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "portfolio_halt": snap.portfolio_halt,
            "research_halt": snap.research_halt,
            "ai_halt": snap.ai_halt,
            "strategy_halts": sorted(snap.strategy_halts),
            "states": [
                {
                    "kill_level": state.kill_level.value,
                    "scope_key": state.scope_key,
                    "active": state.active,
                    "engaged_at": state.engaged_at,
                    "engaged_by": state.engaged_by,
                    "rationale_hash": state.rationale_hash,
                }
                for state in snap.states
            ],
        }
        self.state_file.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

    def _hydrate_from_state_file(self) -> None:
        if not self.state_file.exists():
            return
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in payload.get("states", []):
            if not item.get("active"):
                continue
            try:
                level = KillLevel(str(item["kill_level"]))
            except ValueError:
                continue
            scope = str(item.get("scope_key") or "GLOBAL")
            existing = self.get_state(level, scope_key=scope)
            if existing.active:
                continue
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO human_override_registry (
                        kill_level, scope_key, active, engaged_at,
                        engaged_by, rationale_hash, metadata_json
                    ) VALUES (?, ?, 1, ?, ?, ?, ?)
                    ON CONFLICT(kill_level, scope_key) DO UPDATE SET
                        active = 1,
                        engaged_at = excluded.engaged_at,
                        engaged_by = excluded.engaged_by,
                        rationale_hash = excluded.rationale_hash
                    """,
                    (
                        level.value,
                        scope,
                        item.get("engaged_at"),
                        item.get("engaged_by"),
                        item.get("rationale_hash"),
                        "{}",
                    ),
                )


def _row_to_journal_entry(row: dict[str, Any]) -> ChangeJournalEntry:
    return ChangeJournalEntry(
        journal_id=int(row["journal_id"]),
        timestamp=str(row["timestamp"]),
        event_type=str(row["event_type"]),
        triggered_by=str(row["triggered_by"]),
        scope_key=str(row.get("scope_key") or "GLOBAL"),
        previous_state=json.loads(str(row["previous_state_json"])),
        requested_state=json.loads(str(row["requested_state_json"])),
        rationale_hash=str(row["rationale_hash"]),
        payload_signature=str(row["payload_signature"]),
        metadata=json.loads(str(row.get("metadata_json") or "{}")),
    )


def is_pre_flight_recon_locked(
    db_path: Path = RESEARCH_VAULT_PATH,
) -> tuple[bool, str]:
    ensure_governance_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT active, reason
            FROM control_plane_latches
            WHERE latch_key = ?
            """,
            (PRE_FLIGHT_RECON_LATCH_KEY,),
        ).fetchone()
    if row is None or int(row[0]) != 1:
        return False, ""
    return True, str(row[1])


def engage_pre_flight_recon_lock(
    reason: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    db_path: Path = RESEARCH_VAULT_PATH,
) -> None:
    ensure_governance_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO control_plane_latches (
                latch_key, active, reason, metadata_json, updated_at
            ) VALUES (?, 1, ?, ?, ?)
            ON CONFLICT(latch_key) DO UPDATE SET
                active = 1,
                reason = excluded.reason,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                PRE_FLIGHT_RECON_LATCH_KEY,
                reason,
                json.dumps(dict(metadata or {}), separators=(",", ":")),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def release_pre_flight_recon_lock(
    *,
    operator: str,
    rationale: str,
    db_path: Path = RESEARCH_VAULT_PATH,
) -> bool:
    ensure_governance_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT active FROM control_plane_latches
            WHERE latch_key = ?
            """,
            (PRE_FLIGHT_RECON_LATCH_KEY,),
        ).fetchone()
        if row is None or int(row[0]) != 1:
            return False
        cur = conn.execute(
            """
            UPDATE control_plane_latches
            SET active = 0,
                reason = ?,
                metadata_json = ?,
                updated_at = ?
            WHERE latch_key = ?
            """,
            (
                f"released_by:{operator}",
                json.dumps({"release_rationale": rationale}, separators=(",", ":")),
                datetime.now(timezone.utc).isoformat(),
                PRE_FLIGHT_RECON_LATCH_KEY,
            ),
        )
        # R1 sibling hardening: never report a release the UPDATE didn't apply.
        if int(cur.rowcount or 0) == 0:
            log.warning("pre_flight_recon_release_no_row",
                        latch_key=PRE_FLIGHT_RECON_LATCH_KEY)
            return False
    return True
