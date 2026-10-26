"""
Tests: ensure_db_writable() is called before every runtime write path.

We patch ensure_db_writable to raise PersistenceOwnershipError and confirm
that the write functions propagate the error rather than silently connecting.
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch

from src.persistence.ownership_guard import PersistenceOwnershipError


_GUARD = "src.persistence.db.ensure_db_writable"
_GUARD_GOV = "src.persistence.governance_state_store.ensure_db_writable"

_ERROR = PersistenceOwnershipError("db owned by root — test sentinel")


def _raise(_path: Path) -> None:
    raise _ERROR


# ---------------------------------------------------------------------------
# db.py — critical hot-path functions
# ---------------------------------------------------------------------------

def test_log_trade_raises_on_ownership_error(tmp_path: Path) -> None:
    from src.persistence.db import log_trade
    db = tmp_path / "t.db"
    with patch(_GUARD, side_effect=_raise):
        with pytest.raises(PersistenceOwnershipError):
            log_trade("SPY", "spy", "long", 10.0, db_path=db)


def test_log_fill_raises_on_ownership_error(tmp_path: Path) -> None:
    from src.persistence.db import log_fill
    from src.models import OrderResult, Side
    from datetime import datetime, timezone
    result = OrderResult(symbol="SPY", qty=5.0, filled_price=400.0, side=Side.BUY,
                         order_id="o1", status="filled",
                         filled_at=datetime.now(timezone.utc))
    db = tmp_path / "t.db"
    with patch(_GUARD, side_effect=_raise):
        with pytest.raises(PersistenceOwnershipError):
            log_fill(result, "spy", db_path=db)


def test_upsert_daily_pnl_raises_on_ownership_error(tmp_path: Path) -> None:
    from src.persistence.db import upsert_daily_pnl
    db = tmp_path / "t.db"
    with patch(_GUARD, side_effect=_raise):
        with pytest.raises(PersistenceOwnershipError):
            upsert_daily_pnl("SPY", "spy", 100.0, 50000.0, db_path=db)


def test_set_peak_equity_raises_on_ownership_error(tmp_path: Path) -> None:
    from src.persistence.db import set_peak_equity
    db = tmp_path / "t.db"
    with patch(_GUARD, side_effect=_raise):
        with pytest.raises(PersistenceOwnershipError):
            set_peak_equity(50000.0, db_path=db)


def test_log_bot_run_raises_on_ownership_error(tmp_path: Path) -> None:
    from src.persistence.db import log_bot_run
    db = tmp_path / "t.db"
    with patch(_GUARD, side_effect=_raise):
        with pytest.raises(PersistenceOwnershipError):
            log_bot_run("live", "ok", db_path=db)


def test_log_system_event_raises_on_ownership_error(tmp_path: Path) -> None:
    from src.persistence.db import log_system_event
    db = tmp_path / "t.db"
    with patch(_GUARD, side_effect=_raise):
        with pytest.raises(PersistenceOwnershipError):
            log_system_event("test_event", "msg", db_path=db)


# ---------------------------------------------------------------------------
# governance_state_store.py — PendingOrderStore write paths
# ---------------------------------------------------------------------------

def test_pending_order_store_stage_raises_on_ownership_error(tmp_path: Path) -> None:
    from src.persistence.governance_state_store import PendingOrderStore
    db = tmp_path / "g.db"
    store = PendingOrderStore(db_path=db)
    with patch(_GUARD_GOV, side_effect=_raise):
        with pytest.raises(PersistenceOwnershipError):
            store.stage("key1", strategy_id="spy", symbol="SPY",
                        side="buy", broker_order_id="b1")


def test_pending_order_store_pop_raises_on_ownership_error(tmp_path: Path) -> None:
    from src.persistence.governance_state_store import PendingOrderStore
    db = tmp_path / "g.db"
    store = PendingOrderStore(db_path=db)
    # stage without guard active so it's in the DB
    store.stage("key1", strategy_id="spy", symbol="SPY",
                side="buy", broker_order_id="b1")
    with patch(_GUARD_GOV, side_effect=_raise):
        with pytest.raises(PersistenceOwnershipError):
            store.pop("key1")


def test_pending_order_store_clear_prefix_raises_on_ownership_error(tmp_path: Path) -> None:
    from src.persistence.governance_state_store import PendingOrderStore
    db = tmp_path / "g.db"
    store = PendingOrderStore(db_path=db)
    with patch(_GUARD_GOV, side_effect=_raise):
        with pytest.raises(PersistenceOwnershipError):
            store.clear_prefix("spy", "SPY")


def test_pending_order_store_clear_all_raises_on_ownership_error(tmp_path: Path) -> None:
    from src.persistence.governance_state_store import PendingOrderStore
    db = tmp_path / "g.db"
    store = PendingOrderStore(db_path=db)
    with patch(_GUARD_GOV, side_effect=_raise):
        with pytest.raises(PersistenceOwnershipError):
            store.clear_all()


def test_pending_order_store_reconcile_raises_on_ownership_error(tmp_path: Path) -> None:
    from src.persistence.governance_state_store import PendingOrderStore
    db = tmp_path / "g.db"
    store = PendingOrderStore(db_path=db)
    # stage a key so reconcile has stale_keys to delete
    store.stage("spy:SPY:o1", strategy_id="spy", symbol="SPY",
                side="buy", broker_order_id="broker-1")
    # reconcile with empty broker set → stale_keys = ["spy:SPY:o1"] → hits write path
    with patch(_GUARD_GOV, side_effect=_raise):
        with pytest.raises(PersistenceOwnershipError):
            store.reconcile_with_broker_open_orders(set())
