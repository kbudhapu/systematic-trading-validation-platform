"""
Persistent command bus — control API enqueues, trading-bot executes.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import structlog

from src.control.supabase_client import get_supabase, run_with_supabase_retry
from src.persistence.db import DB_PATH
from src.persistence.ownership_guard import ensure_db_writable

log = structlog.get_logger()

CONTROL_COMMANDS_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS control_commands (
    command_id TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT,
    idempotency_key TEXT UNIQUE,
    error_message TEXT,
    created_at TEXT NOT NULL,
    processed_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_control_commands_pending
    ON control_commands(created_at)
    WHERE status = 'pending';
"""

# Terminal states: a command here is DONE and must never re-fire (K1 idempotency).
# String literals (the ControlCommandStatus enum is defined below this DDL block).
_TERMINAL_STATUSES = ("completed", "failed")
# K1 crash-recovery: bound re-fire of an orphaned command so a poison-pill (one that
# hard-crashes the process every attempt) cannot loop forever.
MAX_RECLAIM_ATTEMPTS = 3


class ControlCommandType(str, Enum):
    FLATTEN_AND_HALT = "FLATTEN_AND_HALT"
    GO_LIVE = "GO_LIVE"
    ENGAGE_KILL_SWITCH = "ENGAGE_KILL_SWITCH"
    RELEASE_KILL_SWITCH = "RELEASE_KILL_SWITCH"
    RELOAD_CONFIG = "RELOAD_CONFIG"
    # PB: a trivial round-trip liveness probe of the command bus. Claim -> no-op execute
    # -> terminal 'completed'. Carries a TTL (expires_at) so an orphaned probe self-retires
    # 'expired' instead of aging into the poison ladder (PC-1). NEVER a protective action.
    PING = "PING"


EMERGENCY_COMMAND_TYPES: frozenset[ControlCommandType] = frozenset(
    {
        ControlCommandType.FLATTEN_AND_HALT,
        ControlCommandType.ENGAGE_KILL_SWITCH,
    }
)


class ControlCommandStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class ControlCommand:
    command_id: str
    command_type: ControlCommandType
    payload: dict[str, Any]
    status: ControlCommandStatus
    requested_by: str | None
    idempotency_key: str | None
    error_message: str | None
    created_at: str
    processed_at: str | None
    expires_at: str | None = None


def ensure_control_commands_schema(db_path: Path | None = None) -> None:
    path = db_path if db_path is not None else DB_PATH
    ensure_db_writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(CONTROL_COMMANDS_SQLITE_DDL)
        # K1 additive migration: `attempts` on a pre-existing control_commands table.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(control_commands)")}
        if "attempts" not in cols:
            conn.execute("ALTER TABLE control_commands ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "expires_at" not in cols:
            # PB additive migration: NULL = never expires (every pre-existing row). A TTL is
            # only ever set on PING probes; safety commands are forbidden from carrying one.
            conn.execute("ALTER TABLE control_commands ADD COLUMN expires_at TEXT")


def enqueue_control_command(
    command_type: ControlCommandType,
    payload: Mapping[str, Any] | None = None,
    *,
    requested_by: str | None = None,
    idempotency_key: str | None = None,
    command_id: str | None = None,
) -> ControlCommand:
    """Enqueue on Supabase when available, otherwise stage in local SQLite."""
    body = dict(payload or {})
    resolved_id = command_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    client = get_supabase()
    if client is not None:
        row = {
            "command_id": resolved_id,
            "command_type": command_type.value,
            "payload_json": body,
            "status": ControlCommandStatus.PENDING.value,
            "requested_by": requested_by,
            "idempotency_key": idempotency_key,
            "created_at": now,
        }
        try:
            resp = client.table("control_commands").insert(row).execute()
            data = (resp.data or [row])[0]
            return _row_to_command(data)
        except Exception as exc:
            if idempotency_key and "duplicate" in str(exc).lower():
                existing = fetch_command_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return existing
            log.warning("control_command_supabase_enqueue_failed", error=str(exc))

    return enqueue_control_command_local(
        command_type,
        body,
        requested_by=requested_by,
        idempotency_key=idempotency_key,
        command_id=resolved_id,
        created_at=now,
    )


def enqueue_control_command_local(
    command_type: ControlCommandType,
    payload: Mapping[str, Any] | None = None,
    *,
    requested_by: str | None = None,
    idempotency_key: str | None = None,
    command_id: str | None = None,
    created_at: str | None = None,
    expires_at: str | None = None,
    db_path: Path | None = None,
) -> ControlCommand:
    """Write directly to the VPS SQLite command queue (cloud bypass path).

    ``expires_at`` (ISO-8601, optional) gives the command a TTL: an orphaned command past
    its expiry self-retires 'expired' (quiet) via ``retire_expired_commands`` instead of
    aging into the poison ladder. It is FORBIDDEN on emergency/protective commands -- a
    safety action must never silently expire -- and enqueue RAISES if one is supplied."""
    if expires_at is not None and command_type in EMERGENCY_COMMAND_TYPES:
        raise ValueError(
            f"{command_type.value} must not carry a TTL (expires_at): a protective/safety "
            f"command must never silently expire")
    resolved_db = db_path if db_path is not None else DB_PATH
    resolved_id = command_id or str(uuid.uuid4())
    now = created_at or datetime.now(timezone.utc).isoformat()
    body = dict(payload or {})
    ensure_control_commands_schema(resolved_db)
    with sqlite3.connect(resolved_db) as conn:
        conn.execute(
            """
            INSERT INTO control_commands (
                command_id, command_type, payload_json, status,
                requested_by, idempotency_key, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolved_id,
                command_type.value,
                json.dumps(body, separators=(",", ":")),
                ControlCommandStatus.PENDING.value,
                requested_by,
                idempotency_key,
                now,
                expires_at,
            ),
        )
    log.info(
        "control_command_staged_local",
        command_id=resolved_id,
        command_type=command_type.value,
    )
    return ControlCommand(
        command_id=resolved_id,
        command_type=command_type,
        payload=body,
        status=ControlCommandStatus.PENDING,
        requested_by=requested_by,
        idempotency_key=idempotency_key,
        error_message=None,
        created_at=now,
        processed_at=None,
        expires_at=expires_at,
    )


def fetch_pending_commands(*, limit: int = 32) -> list[ControlCommand]:
    """Merge pending commands from Supabase and local SQLite staging."""
    cap = max(int(limit), 1)
    merged: list[ControlCommand] = []
    seen: set[str] = set()

    resp = run_with_supabase_retry(
        lambda c: c.table("control_commands")
        .select("*")
        .eq("status", ControlCommandStatus.PENDING.value)
        .order("created_at")
        .limit(cap)
        .execute(),
        label="fetch_pending_commands",
    )
    if resp is not None:
        try:
            for row in resp.data or []:
                command = _row_to_command(row)
                if command.command_id in seen:
                    continue
                merged.append(command)
                seen.add(command.command_id)
        except Exception as exc:
            log.warning("control_command_supabase_fetch_failed", error=str(exc))

    ensure_control_commands_schema()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM control_commands
            WHERE status = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (ControlCommandStatus.PENDING.value, cap),
        ).fetchall()
    for row in rows:
        command = _sqlite_row_to_command(dict(row))
        if command.command_id in seen:
            continue
        merged.append(command)
        seen.add(command.command_id)

    merged.sort(key=lambda item: item.created_at)
    return merged[:cap]


def _transition_local_pending_to_processing(command_id: str, now: str) -> int:
    """Best-effort drive of a LOCAL command copy pending->processing. Guarded on status=pending
    so it never clobbers a terminal local state. Returns rows changed."""
    try:
        ensure_control_commands_schema()
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "UPDATE control_commands SET status = ?, processed_at = ? "
                "WHERE command_id = ? AND status = ?",
                (ControlCommandStatus.PROCESSING.value, now, command_id,
                 ControlCommandStatus.PENDING.value),
            )
            return int(cur.rowcount or 0)
    except Exception:
        return 0


def try_claim_command(command_id: str) -> bool:
    """Atomically transition a pending command to processing — EXACTLY ONCE across both buses (E2).

    CP2 double-execute window: a command dual-written to Supabase AND local SQLite under the same
    command_id, if claimed on only one bus, re-appears as pending on the other bus's next poll and
    executes twice. Fix: when Supabase is configured, it is the AUTHORITATIVE claim bus for any
    command that EXISTS there — a successful CAS also drives the local mirror to processing, and a
    command present-but-not-pending in Supabase is NOT re-claimed locally. Local CAS is
    authoritative only for local-only commands (or when Supabase is unconfigured)."""
    now = datetime.now(timezone.utc).isoformat()
    client = get_supabase()
    if client is not None:
        try:
            resp = (
                client.table("control_commands")
                .update(
                    {
                        "status": ControlCommandStatus.PROCESSING.value,
                        "processed_at": now,
                    }
                )
                .eq("command_id", command_id)
                .eq("status", ControlCommandStatus.PENDING.value)
                .execute()
            )
            if resp.data:
                # Claimed on the authoritative bus — drive the local mirror too so it can't
                # be re-claimed on the next poll.
                _transition_local_pending_to_processing(command_id, now)
                return True
        except Exception:
            pass
        # Supabase CAS matched 0 rows. If the command EXISTS in Supabase (already processing /
        # terminal), it is Supabase-owned — do NOT double-claim the local mirror.
        try:
            sel = (
                client.table("control_commands")
                .select("command_id")
                .eq("command_id", command_id)
                .limit(1)
                .execute()
            )
            if sel.data:
                return False
        except Exception:
            pass  # Supabase read fault → fall through to local (best-effort, degraded mode)

    # Supabase unconfigured, or command is local-only → local CAS is authoritative.
    ensure_control_commands_schema()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            UPDATE control_commands
            SET status = ?, processed_at = ?
            WHERE command_id = ? AND status = ?
            """,
            (
                ControlCommandStatus.PROCESSING.value,
                now,
                command_id,
                ControlCommandStatus.PENDING.value,
            ),
        )
        return int(cursor.rowcount or 0) > 0


def fetch_and_claim_emergency_commands(*, limit: int = 16) -> list[ControlCommand]:
    """Claim emergency commands for immediate interrupt dispatch."""
    cap = max(int(limit), 1)
    claimed: list[ControlCommand] = []
    for command in fetch_pending_commands(limit=cap):
        if command.command_type not in EMERGENCY_COMMAND_TYPES:
            continue
        if not try_claim_command(command.command_id):
            continue
        claimed.append(
            ControlCommand(
                command_id=command.command_id,
                command_type=command.command_type,
                payload=command.payload,
                status=ControlCommandStatus.PROCESSING,
                requested_by=command.requested_by,
                idempotency_key=command.idempotency_key,
                error_message=None,
                created_at=command.created_at,
                processed_at=datetime.now(timezone.utc).isoformat(),
            )
        )
    return claimed


def fetch_command_by_idempotency_key(key: str) -> ControlCommand | None:
    client = get_supabase()
    if client is not None:
        try:
            resp = (
                client.table("control_commands")
                .select("*")
                .eq("idempotency_key", key)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            if rows:
                return _row_to_command(rows[0])
        except Exception:
            pass
    ensure_control_commands_schema()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM control_commands WHERE idempotency_key = ? LIMIT 1",
            (key,),
        ).fetchone()
    if row is None:
        return None
    return _sqlite_row_to_command(dict(row))


def mark_command_processing(command_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    client = get_supabase()
    if client is not None:
        try:
            client.table("control_commands").update(
                {
                    "status": ControlCommandStatus.PROCESSING.value,
                    "processed_at": now,
                }
            ).eq("command_id", command_id).execute()
            return
        except Exception:
            pass
    ensure_control_commands_schema()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE control_commands
            SET status = ?, processed_at = ?
            WHERE command_id = ?
            """,
            (ControlCommandStatus.PROCESSING.value, now, command_id),
        )


def _mark_terminal(command_id: str, *, status: str, error_message: str | None) -> None:
    """CQ-1: mark a command terminal, Supabase-first but with a ROWCOUNT FALL-THROUGH to local
    SQLite (mirroring try_claim_command). The prior code returned unconditionally after the
    Supabase update, so a command staged LOCALLY (enqueue_control_command_local -- the soak
    probe) matched 0 Supabase rows yet was never written locally, leaving it stuck 'processing'.

    Order: (1) Supabase update -- return ONLY if it actually matched a row (resp.data truthy);
    (2) else local update guarded on non-terminal status; (3) if local matched 0 rows, it is a
    genuine failure ONLY when the row is also ABSENT locally (a 0-row hit on an already-terminal
    row is an idempotent no-op). A genuine 0-rows-anywhere write logs ERROR (PC-1b shape)."""
    now = datetime.now(timezone.utc).isoformat()
    wrote = False
    # E2: mirror the terminal status FORWARD to BOTH buses (previously returned after Supabase,
    # leaving the local mirror non-terminal → dashboard/local history could disagree).
    client = get_supabase()
    if client is not None:
        try:
            resp = (
                client.table("control_commands")
                .update({"status": status, "processed_at": now, "error_message": error_message})
                .eq("command_id", command_id)
                .execute()
            )
            if resp.data:            # rowcount check -- only trust a remote write that hit a row
                wrote = True
        except Exception:
            pass                     # fall through to local on any Supabase fault
    ensure_control_commands_schema()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            f"""
            UPDATE control_commands
            SET status = ?, processed_at = ?, error_message = ?
            WHERE command_id = ? AND status NOT IN ({','.join('?' for _ in _TERMINAL_STATUSES)})
            """,
            (status, now, error_message, command_id, *_TERMINAL_STATUSES),
        )
        if cur.rowcount >= 1:
            wrote = True
        already_terminal = conn.execute(
            "SELECT 1 FROM control_commands WHERE command_id = ?", (command_id,)
        ).fetchone() is not None
    if wrote or already_terminal:
        return                       # wrote at least one bus, or an idempotent already-terminal no-op
    log.error("control_command_mark_terminal_failed", command_id=command_id, target=status,
              reason="0 rows updated in Supabase AND command absent locally")


def mirror_local_commands_to_supabase(*, limit: int = 500) -> int:
    """E2: forward local SQLite command history to Supabase so the dashboard (which reads Supabase
    only) sees the full history + consistent status. Idempotent upsert keyed by command_id; a
    command staged local-only (Supabase-outage fallback, soak probe) becomes visible after
    recovery. Returns rows mirrored; no-op when Supabase is unconfigured."""
    client = get_supabase()
    if client is None:
        return 0
    ensure_control_commands_schema()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT command_id, command_type, payload_json, status, requested_by, "
            "idempotency_key, error_message, created_at, processed_at "
            "FROM control_commands ORDER BY created_at DESC LIMIT ?",
            (max(int(limit), 1),),
        ).fetchall()
    mirrored = 0
    for row in rows:
        d = dict(row)
        try:
            payload = json.loads(d.get("payload_json") or "{}")
        except Exception:
            payload = {}
        record = {
            "command_id": d["command_id"],
            "command_type": d["command_type"],
            "payload_json": payload,
            "status": d["status"],
            "requested_by": d.get("requested_by"),
            "idempotency_key": d.get("idempotency_key"),
            "error_message": d.get("error_message"),
            "created_at": d.get("created_at"),
            "processed_at": d.get("processed_at"),
        }
        try:
            client.table("control_commands").upsert(record, on_conflict="command_id").execute()
            mirrored += 1
        except Exception as exc:
            log.warning("control_command_mirror_failed", command_id=d["command_id"], error=str(exc))
    return mirrored


def mark_command_completed(command_id: str) -> None:
    _mark_terminal(command_id, status=ControlCommandStatus.COMPLETED.value, error_message=None)


def mark_command_failed(command_id: str, error_message: str) -> None:
    _mark_terminal(command_id, status=ControlCommandStatus.FAILED.value,
                   error_message=error_message[:2000])


def reclaim_orphaned_processing_commands(
    *, max_attempts: int = MAX_RECLAIM_ATTEMPTS, db_path: Path | None = None,
    notifier: Any | None = None,
) -> int:
    """K1 crash-recovery: re-queue commands orphaned in `processing` by a prior
    process that died mid-execution.

    The bot is a single writer, so any `processing` row seen at consumer startup is
    an orphan (the previous process claimed it, began executing, and died before the
    complete-after-durable-success terminal-mark). Re-queue such rows to `pending`
    so they re-fire exactly once more via the normal dispatch path (the protective
    action -- flatten against a flat book, config reload -- is idempotent). This is
    the crash-recovery path required by the fix semantic: a crash mid-execution
    leaves the command re-fireable; a completed/failed command is TERMINAL and is
    never touched here. `attempts` bounds re-fire so a poison command (one that hard-
    crashes the process every attempt) is marked failed rather than looping forever.

    PC-1 (poison-path repair): the dead-letter UPDATE is now ROWCOUNT-CHECKED. A
    matched row (rowcount >= 1) is a SUCCESSFUL dead-letter -> logged at warning as
    ``control_command_poisoned`` (the terminal outcome is expected, not an error). A
    rowcount of 0 is a GENUINE failure to retire the command (it was not in
    ``processing`` at write time -- a concurrent transition, or a write that did not
    land) -> logged at error as ``control_command_poison_failed`` AND, because
    dead-lettering is the retirement mechanism for dangerous commands, it PAGES via
    ``notifier`` when one is supplied (PC-1d). Previously the error was logged
    unconditionally on every poison, so 172 SUCCESSFUL dead-letters read as failures.

    Returns the number of commands re-queued for re-fire (unchanged contract).
    """
    path = db_path if db_path is not None else DB_PATH
    ensure_control_commands_schema(path)
    now = datetime.now(timezone.utc).isoformat()
    requeued = 0
    poisoned = 0
    poison_failures: list[str] = []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        orphans = conn.execute(
            "SELECT command_id, attempts FROM control_commands WHERE status = ?",
            (ControlCommandStatus.PROCESSING.value,),
        ).fetchall()
        for row in orphans:
            attempts = int(row["attempts"] or 0)
            if attempts >= max_attempts:
                cur = conn.execute(
                    "UPDATE control_commands SET status = ?, processed_at = ?, "
                    "error_message = ? WHERE command_id = ? AND status = ?",
                    (ControlCommandStatus.FAILED.value, now,
                     f"poison: exceeded {max_attempts} reclaim attempts",
                     row["command_id"], ControlCommandStatus.PROCESSING.value),
                )
                if cur.rowcount >= 1:
                    poisoned += 1
                    log.warning("control_command_poisoned", command_id=row["command_id"],
                                attempts=attempts,
                                note="dead-lettered after max reclaim attempts; will not re-poll")
                else:
                    # PC-1: the retirement write did NOT land -> the command is NOT
                    # terminal and can re-poll. THIS is the real failure (dangerous
                    # commands must be retirable). Page, do not merely log.
                    poison_failures.append(row["command_id"])
                    log.error("control_command_poison_failed", command_id=row["command_id"],
                              attempts=attempts,
                              reason="dead-letter UPDATE matched 0 rows (row not in "
                                     "'processing' at write time)")
            else:
                conn.execute(
                    "UPDATE control_commands SET status = ?, attempts = ? "
                    "WHERE command_id = ? AND status = ?",
                    (ControlCommandStatus.PENDING.value, attempts + 1,
                     row["command_id"], ControlCommandStatus.PROCESSING.value),
                )
                requeued += 1
    if requeued:
        log.warning("control_commands_reclaimed_for_refire", count=requeued)
    if poisoned:
        log.warning("control_commands_dead_lettered", count=poisoned)
    if poison_failures:
        # PC-1d: dead-letter failure is the ONE control-command event that must page --
        # it means a command (possibly a FLATTEN/KILL) could not be retired.
        log.error("control_command_poison_failures_total", count=len(poison_failures))
        _page_poison_failures(notifier, poison_failures)
    return requeued


def retire_expired_commands(*, db_path: Path | None = None) -> int:
    """PB-2: retire any non-terminal command whose TTL has elapsed to terminal `failed`
    with an 'expired' stamp. Expiry is an EXPECTED terminal outcome (a probe outlived its
    window), so this is QUIET -- log.warning at most, NEVER an error and NEVER a page. Run
    at boot BEFORE the poison ladder so an orphaned expired PING self-retires instead of
    aging into ``reclaim_orphaned_processing_commands``. NULL expires_at = never expires, so
    pre-existing rows and all non-probe commands are untouched. Returns the count retired."""
    path = db_path if db_path is not None else DB_PATH
    ensure_control_commands_schema(path)
    now_iso = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            "UPDATE control_commands SET status = ?, processed_at = ?, error_message = ? "
            "WHERE status IN (?, ?) AND expires_at IS NOT NULL AND expires_at < ?",
            (ControlCommandStatus.FAILED.value, now_iso, "expired: TTL elapsed before terminal",
             ControlCommandStatus.PENDING.value, ControlCommandStatus.PROCESSING.value, now_iso),
        )
        retired = cur.rowcount
    if retired:
        log.warning("control_commands_expired", count=retired)   # expected; never an error/page
    return retired


def retire_stale_processing_orphans(
    *, reason: str, older_than_minutes: float = 10.0,
    command_type: ControlCommandType | None = None, db_path: Path | None = None,
) -> int:
    """PC-1c: retire STALE `processing` orphans by transitioning them to the terminal
    `failed` status with an audit stamp -- the FIXED path, never a raw DELETE (the row
    is preserved for audit). Age-gated (``older_than_minutes``) so a genuinely in-flight
    command claimed seconds ago by a live consumer is NEVER retired. Optionally scoped
    to one ``command_type``. Returns the number of rows retired.

    This is for clearing an accumulated backlog once (e.g. the soak-probe RELOAD_CONFIG
    orphans); routine retirement happens through ``reclaim_orphaned_processing_commands``.
    """
    path = db_path if db_path is not None else DB_PATH
    ensure_control_commands_schema(path)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=older_than_minutes)).isoformat()
    now_iso = now.isoformat()
    retired = 0
    query = ("SELECT command_id FROM control_commands "
             "WHERE status = ? AND created_at < ?")
    params: list[Any] = [ControlCommandStatus.PROCESSING.value, cutoff]
    if command_type is not None:
        query += " AND command_type = ?"
        params.append(command_type.value)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        for row in rows:
            cur = conn.execute(
                "UPDATE control_commands SET status = ?, processed_at = ?, "
                "error_message = ? WHERE command_id = ? AND status = ?",
                (ControlCommandStatus.FAILED.value, now_iso, f"retired: {reason}",
                 row["command_id"], ControlCommandStatus.PROCESSING.value),
            )
            if cur.rowcount >= 1:
                retired += 1
    if retired:
        log.warning("control_commands_stale_orphans_retired", count=retired,
                    reason=reason, older_than_minutes=older_than_minutes)
    return retired


def _page_poison_failures(notifier: Any | None, command_ids: list[str]) -> None:
    """Route a dead-letter failure to the operator alert path. Never raises into the
    reclaim caller (a paging fault must not break crash-recovery)."""
    if notifier is None:
        return
    try:
        notifier.notify({
            "kind": "control_command_poison_failed",
            "severity": "critical",
            "message": (f"{len(command_ids)} control command(s) could not be dead-lettered "
                        f"(retirement mechanism failed)"),
            "detail": {"command_ids": command_ids[:20], "count": len(command_ids)},
        })
    except Exception as exc:  # pragma: no cover - defensive
        log.error("control_command_poison_page_failed", error=str(exc))


def _row_to_command(row: Mapping[str, Any]) -> ControlCommand:
    payload_raw = row.get("payload_json") or {}
    if isinstance(payload_raw, str):
        payload = json.loads(payload_raw or "{}")
    elif isinstance(payload_raw, dict):
        payload = dict(payload_raw)
    else:
        payload = {}
    command_id = str(row.get("command_id") or "")
    return ControlCommand(
        command_id=command_id,
        command_type=ControlCommandType(str(row["command_type"])),
        payload=payload,
        status=ControlCommandStatus(str(row.get("status") or "pending")),
        requested_by=row.get("requested_by"),
        idempotency_key=row.get("idempotency_key"),
        error_message=row.get("error_message"),
        created_at=str(row.get("created_at") or ""),
        processed_at=row.get("processed_at"),
        expires_at=row.get("expires_at"),
    )


def _sqlite_row_to_command(row: dict[str, Any]) -> ControlCommand:
    payload = json.loads(str(row.get("payload_json") or "{}"))
    return ControlCommand(
        command_id=str(row["command_id"]),
        command_type=ControlCommandType(str(row["command_type"])),
        payload=payload,
        status=ControlCommandStatus(str(row.get("status") or "pending")),
        requested_by=row.get("requested_by"),
        idempotency_key=row.get("idempotency_key"),
        error_message=row.get("error_message"),
        created_at=str(row.get("created_at") or ""),
        processed_at=row.get("processed_at"),
        expires_at=row.get("expires_at"),
    )
