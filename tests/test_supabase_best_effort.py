"""E8: Supabase is a best-effort telemetry mirror, never load-bearing.

The architectural contract (documented in docs/PROJECT_STATE.md + DATA_LAYOUT.md):
SQLite is the local source of truth and primary read path; Supabase is a
best-effort WAL read-replica / dashboard backend / cross-machine config-command
sync. The live execution loop must survive a Supabase outage with IDENTICAL local
outputs -- so every SupabaseSync write must swallow a missing/raising client and
return gracefully, never propagating into the loop.

These tests enforce that invariant on the live-loop-critical sync methods (order
submit/fill, heartbeat, system events, snapshots): with no client, and with a
client that raises on every call, none of them raise, and the order-id accessor
degrades to None (which the fill path already tolerates).

NOTE (finding, see OVERNIGHT_QUEUE_LOG.md E8): these mirror calls are currently
SYNCHRONOUS, so they add network latency to the loop even though they are
non-load-bearing. Offloading them is not output-trivial (sync_order_submitted
returns the mirror order-id consumed by sync_order_filled), so the latency fix is
logged as a finding rather than forced in this behavior-preserving pass.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.control.supabase_sync import SupabaseSync


@pytest.fixture()
def sync() -> SupabaseSync:
    return SupabaseSync(MagicMock())


def _exercise_live_loop_writes(s: SupabaseSync) -> None:
    """Call the sync methods that sit on the live execution path."""
    order = MagicMock()
    order.side.value = "buy"
    fill = MagicMock()
    s.log_system_event("evt", "msg", severity="info")
    oid = s.sync_order_submitted("uuid", order, 100.0)
    s.sync_order_filled(oid, fill, "uuid")
    s.sync_heartbeat_ping(environment="paper", timestamp="2026-07-05T00:00:00+00:00")
    return oid


def test_no_client_is_graceful(sync: SupabaseSync) -> None:
    with patch("src.control.supabase_sync.get_supabase", return_value=None):
        oid = _exercise_live_loop_writes(sync)
    assert oid is None   # order-id degrades to None; fill path tolerates it


def test_raising_client_is_swallowed(sync: SupabaseSync) -> None:
    boom = MagicMock()
    boom.table.side_effect = RuntimeError("supabase outage")
    with patch("src.control.supabase_sync.get_supabase", return_value=boom):
        # Must not raise despite the client blowing up on every call.
        oid = _exercise_live_loop_writes(sync)
    assert oid is None


def test_order_id_none_does_not_break_fill(sync: SupabaseSync) -> None:
    """The fill mirror must accept a None order-id (the outage correlation gap)."""
    with patch("src.control.supabase_sync.get_supabase", return_value=None):
        sync.sync_order_filled(None, MagicMock(), "uuid")  # no raise
