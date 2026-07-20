"""
Tests for SessionManager.ensure_session lifecycle.

Covers:
  1. No active session in DB → creates a new session automatically.
  2. Active session in DB → returned and cached; no duplicate created.
  3. Session closed externally (DB updated without going through close_active)
     → stale in-memory cache is NOT returned; new session created instead.
     This is the exact failure mode that caused the dashboard to show $0 for
     all legs after a direct SQL UPDATE closed sessions without a bot restart.
  4. Disabled legs with no active session → new session created when they
     become due (same code path as case 1).
  5. invalidate() drops the cache so the next call re-queries the DB.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.control.session_manager import ActiveSession, SessionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID = "0f1fec70-0a75-42f7-b9f7-464544e25b51"
_SESSION_ID_A = "17727c76-4be5-4c32-9f97-b8f65925b148"
_SESSION_ID_B = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"
_ENV = "paper"
_BASELINE = 50_000.0


def _active_row(session_id: str = _SESSION_ID_A) -> dict:
    return {
        "id": session_id,
        "strategy_id": _UUID,
        "environment": _ENV,
        "baseline_equity": str(_BASELINE),
        "is_active": True,
        "started_at": "2026-06-24T01:51:14+00:00",
        "ended_at": None,
    }


def _make_client(*, active_rows: list[dict] | None = None) -> MagicMock:
    """Return a mock Supabase client whose performance_sessions query returns the given rows."""
    client = MagicMock()

    select_resp = MagicMock()
    select_resp.data = active_rows if active_rows is not None else []

    insert_resp = MagicMock()
    insert_resp.data = [
        {
            "id": _SESSION_ID_B,
            "strategy_id": _UUID,
            "environment": _ENV,
            "baseline_equity": _BASELINE,
            "is_active": True,
        }
    ]

    (
        client.table.return_value
        .select.return_value
        .eq.return_value
        .eq.return_value
        .limit.return_value
        .execute.return_value
    ) = select_resp

    client.table.return_value.insert.return_value.execute.return_value = insert_resp

    return client


# ---------------------------------------------------------------------------
# Case 1: no active session → creates a new one
# ---------------------------------------------------------------------------

def test_no_active_session_creates_new_session() -> None:
    """When the DB has no active session, ensure_session must create one."""
    sm = SessionManager()
    client = _make_client(active_rows=[])  # empty — no active session

    # ensure_session's DB read now flows through run_with_supabase_retry (shape 1),
    # which resolves get_supabase in the supabase_client namespace; _create_session
    # still uses the session_manager-local import. Patch both so the fake client is used.
    with patch("src.control.supabase_client.get_supabase", return_value=client), patch(
        "src.control.session_manager.get_supabase", return_value=client
    ):
        session = sm.ensure_session(_UUID, _ENV, _BASELINE)

    assert session is not None
    assert session.session_id == _SESSION_ID_B  # newly created
    assert session.environment == _ENV
    assert session.baseline_equity == _BASELINE
    client.table.return_value.insert.assert_called_once()


# ---------------------------------------------------------------------------
# Case 2: active session in DB → returned and cached; no insert
# ---------------------------------------------------------------------------

def test_active_session_in_db_is_returned_without_creating_new() -> None:
    """When the DB already has an active session, return it; do not create a duplicate."""
    sm = SessionManager()
    client = _make_client(active_rows=[_active_row(_SESSION_ID_A)])

    # ensure_session's DB read now flows through run_with_supabase_retry (shape 1),
    # which resolves get_supabase in the supabase_client namespace; _create_session
    # still uses the session_manager-local import. Patch both so the fake client is used.
    with patch("src.control.supabase_client.get_supabase", return_value=client), patch(
        "src.control.session_manager.get_supabase", return_value=client
    ):
        session = sm.ensure_session(_UUID, _ENV, _BASELINE)

    assert session is not None
    assert session.session_id == _SESSION_ID_A
    client.table.return_value.insert.assert_not_called()


# ---------------------------------------------------------------------------
# Case 3: stale cache — externally-closed session must NOT be returned
# ---------------------------------------------------------------------------

def test_externally_closed_session_is_not_returned_from_stale_cache() -> None:
    """
    Regression test for the 2026-06-26 incident:

    A direct SQL UPDATE closed the QQQ and SPY performance_sessions rows
    (is_active=false, ended_at set) without going through SessionManager.close_active().
    The bot's in-memory cache still held the old ActiveSession object.
    The old ensure_session() checked only the cache (not the DB), so it kept
    returning the stale session — snapshots wrote to a closed session_id and
    the dashboard showed $0 for every leg.

    Post-fix: ensure_session() always queries the DB. When the DB returns no
    active session despite the cache having one, the stale entry is evicted and
    a new session is created.
    """
    sm = SessionManager()

    # Manually inject a stale cached session (simulates the bot's in-memory state
    # after a session was closed externally without a process restart).
    stale = ActiveSession(
        session_id=_SESSION_ID_A,
        strategy_id=_UUID,
        environment=_ENV,
        baseline_equity=_BASELINE,
    )
    sm._sessions[_UUID] = stale

    # DB reports no active session (it was closed externally).
    client = _make_client(active_rows=[])

    # ensure_session's DB read now flows through run_with_supabase_retry (shape 1),
    # which resolves get_supabase in the supabase_client namespace; _create_session
    # still uses the session_manager-local import. Patch both so the fake client is used.
    with patch("src.control.supabase_client.get_supabase", return_value=client), patch(
        "src.control.session_manager.get_supabase", return_value=client
    ):
        session = sm.ensure_session(_UUID, _ENV, _BASELINE)

    # Must NOT return the stale cached session.
    assert session is not None
    assert session.session_id != _SESSION_ID_A, (
        "ensure_session returned the stale closed session — the external-close "
        "bug has not been fixed"
    )
    assert session.session_id == _SESSION_ID_B  # freshly created
    # Stale cache entry must be gone.
    assert sm._sessions.get(_UUID) is not None
    assert sm._sessions[_UUID].session_id == _SESSION_ID_B
    client.table.return_value.insert.assert_called_once()


# ---------------------------------------------------------------------------
# Case 4: cache hit with matching DB row → fast path, no insert
# ---------------------------------------------------------------------------

def test_cache_hit_matching_db_uses_fast_path() -> None:
    """When cache and DB agree on the same session_id, return the cached object."""
    sm = SessionManager()
    cached = ActiveSession(
        session_id=_SESSION_ID_A,
        strategy_id=_UUID,
        environment=_ENV,
        baseline_equity=_BASELINE,
    )
    sm._sessions[_UUID] = cached

    client = _make_client(active_rows=[_active_row(_SESSION_ID_A)])

    # ensure_session's DB read now flows through run_with_supabase_retry (shape 1),
    # which resolves get_supabase in the supabase_client namespace; _create_session
    # still uses the session_manager-local import. Patch both so the fake client is used.
    with patch("src.control.supabase_client.get_supabase", return_value=client), patch(
        "src.control.session_manager.get_supabase", return_value=client
    ):
        session = sm.ensure_session(_UUID, _ENV, _BASELINE)

    assert session is cached  # same object — fast path preserved
    client.table.return_value.insert.assert_not_called()


# ---------------------------------------------------------------------------
# Case 5: invalidate() drops cache so next call re-validates
# ---------------------------------------------------------------------------

def test_invalidate_drops_cache_entry() -> None:
    """invalidate() must evict the cached session so the next call queries the DB."""
    sm = SessionManager()
    sm._sessions[_UUID] = ActiveSession(
        session_id=_SESSION_ID_A,
        strategy_id=_UUID,
        environment=_ENV,
        baseline_equity=_BASELINE,
    )

    sm.invalidate(_UUID)

    assert _UUID not in sm._sessions
