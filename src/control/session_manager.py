"""
Performance session lifecycle — paper/live baselines for % return chart.

Handles go-live transition: close paper session, open live at current equity.
Supports one active session per strategy leg plus portfolio aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

from src.control.supabase_client import get_supabase, run_with_supabase_retry

log = structlog.get_logger()


@dataclass
class ActiveSession:
    """In-memory cache of the active performance session."""

    session_id: str
    strategy_id: str
    environment: str
    baseline_equity: float


class SessionManager:
    """Create, close, and query performance_sessions in Supabase."""

    def __init__(self) -> None:
        self._sessions: dict[str, ActiveSession] = {}

    @property
    def active(self) -> ActiveSession | None:
        """First cached session — backward compat for single-strategy callers."""
        if not self._sessions:
            return None
        return next(iter(self._sessions.values()))

    def ensure_session(
        self, strategy_id: str, environment: str, initial_baseline: float
    ) -> ActiveSession | None:
        """Ensure an active session exists for the given strategy UUID.

        Always queries the DB so that sessions closed externally (e.g. via a
        direct SQL UPDATE that bypasses close_active()) are detected immediately
        and a fresh session is created rather than returning a stale cache entry.
        The cache is still updated on every call so repeated calls within the
        same cycle stay cheap on the object-allocation side.
        """
        try:
            resp = run_with_supabase_retry(
                lambda c: c.table("performance_sessions")
                .select("*")
                .eq("strategy_id", strategy_id)
                .eq("is_active", True)
                .limit(1)
                .execute(),
                label="ensure_session",
            )
            if resp is None:
                return None
            if resp.data:
                row = resp.data[0]
                cached = self._sessions.get(strategy_id)
                # Fast path: cache is still valid — reuse the existing object.
                if (
                    cached is not None
                    and cached.session_id == row["id"]
                    and cached.environment == environment
                ):
                    return cached
                session = ActiveSession(
                    session_id=row["id"],
                    strategy_id=strategy_id,
                    environment=row["environment"],
                    baseline_equity=float(row["baseline_equity"]),
                )
                self._sessions[strategy_id] = session
                return session

            # No active session in the DB — clear any stale cache entry and
            # create a new session so the next snapshot lands in an active row.
            self._sessions.pop(strategy_id, None)
            session = self._create_session(
                strategy_id, environment, initial_baseline
            )
            if session:
                self._sessions[strategy_id] = session
            return session
        except Exception as e:
            log.error("ensure_session_failed", error=str(e))
            return None

    def invalidate(self, strategy_id: str) -> None:
        """Drop the cached session for a strategy so the next ensure_session
        call re-validates against the DB.  Call this whenever a config reload
        may have closed or rotated a session outside the normal API path.
        """
        self._sessions.pop(strategy_id, None)

    def _create_session(
        self, strategy_id: str, environment: str, equity: float
    ) -> ActiveSession | None:
        client = get_supabase()
        if client is None:
            return None

        now = datetime.now(timezone.utc).isoformat()
        row = {
            "strategy_id": strategy_id,
            "environment": environment,
            "started_at": now,
            "baseline_equity": equity,
            "is_active": True,
        }
        resp = client.table("performance_sessions").insert(row).execute()
        if not resp.data:
            return None

        data = resp.data[0]
        session = ActiveSession(
            session_id=data["id"],
            strategy_id=strategy_id,
            environment=environment,
            baseline_equity=float(equity),
        )
        log.info(
            "session_created",
            session_id=session.session_id,
            strategy_id=strategy_id,
            environment=environment,
            baseline=equity,
        )
        return session

    def go_live(self, strategy_id: str, live_equity: float) -> ActiveSession | None:
        """Close paper session and start a new live session at 0% baseline."""
        client = get_supabase()
        if client is None:
            return None

        now = datetime.now(timezone.utc).isoformat()
        try:
            client.table("performance_sessions").update(
                {"is_active": False, "ended_at": now}
            ).eq("strategy_id", strategy_id).eq("is_active", True).execute()

            client.table("strategies").update(
                {"environment": "live", "updated_at": now}
            ).eq("id", strategy_id).execute()

            client.table("system_events").insert(
                {
                    "event_type": "go_live",
                    "severity": "warning",
                    "message": f"Go live confirmed — baseline equity ${live_equity:,.2f}",
                    "metadata": {"strategy_id": strategy_id, "equity": live_equity},
                }
            ).execute()

            self._sessions.pop(strategy_id, None)
            session = self._create_session(strategy_id, "live", live_equity)
            if session:
                self._sessions[strategy_id] = session
                client.table("equity_snapshots").insert(
                    {
                        "session_id": session.session_id,
                        "equity": live_equity,
                        "cash": live_equity,
                        "pct_return": 0.0,
                    }
                ).execute()
            return session
        except Exception as e:
            log.error("go_live_failed", error=str(e))
            return None

    def close_active(self, strategy_id: str) -> None:
        """End the active session without starting a new one."""
        client = get_supabase()
        if client is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        client.table("performance_sessions").update(
            {"is_active": False, "ended_at": now}
        ).eq("strategy_id", strategy_id).eq("is_active", True).execute()
        self._sessions.pop(strategy_id, None)
