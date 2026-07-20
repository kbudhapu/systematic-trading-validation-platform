"""T1 — log_fill invariant: a row in `trades` means a FILL OCCURRED.

Before the fix, db.log_fill hardcoded status='filled' and wrote qty/entry_price
unconditionally, so a rejected/expired/unfilled order became a phantom 'filled' trade
(the 61 rows: qty=0, entry_price=0). These tests lock: real fill -> exactly one trades
row with the broker status; any non-fill -> ZERO trades rows, recorded in `orders`.
Mock OrderResults only; no broker, no real order.
"""
from __future__ import annotations

import sqlite3

import pytest

from datetime import datetime, timezone

from src.models import OrderResult, Side
from src.persistence import db as dbm


def _counts(db_path):
    with sqlite3.connect(db_path) as c:
        t = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        o = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    return t, o


def _result(*, qty, filled_price, status, side=Side.BUY, symbol="QQQ"):
    return OrderResult(symbol=symbol, side=side, qty=qty, filled_price=filled_price,
                       filled_at=datetime.now(timezone.utc), status=status)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "trading.db"
    dbm.init_db(p)
    return p


def test_rejected_order_writes_zero_trades_and_one_order(db):
    dbm.log_fill(_result(qty=0.0, filled_price=0.0, status="rejected"), "mean_reversion_qqq", db_path=db)
    t, o = _counts(db)
    assert t == 0 and o == 1
    with sqlite3.connect(db) as c:
        row = c.execute("SELECT status, reject_reason FROM orders").fetchone()
    assert row[0] == "rejected" and "non_fill" in row[1]


def test_expired_order_writes_zero_trades(db):
    dbm.log_fill(_result(qty=0.0, filled_price=0.0, status="expired"), "s", db_path=db)
    assert _counts(db) == (0, 1)


def test_accepted_but_unfilled_submitted_writes_zero_trades(db):
    """The case a qty-only guard would MISS: _resolved_fill_qty returns order.qty>0 for a
    'submitted' order that never filled, but filled_price stays 0 -> must NOT be a trade."""
    dbm.log_fill(_result(qty=5.0, filled_price=0.0, status="submitted"), "s", db_path=db)
    assert _counts(db) == (0, 1)


def test_partial_fill_writes_one_trade_with_actual_qty_and_price(db):
    dbm.log_fill(_result(qty=3.0, filled_price=100.5, status="partially_filled"), "s", db_path=db)
    t, o = _counts(db)
    assert t == 1 and o == 0
    with sqlite3.connect(db) as c:
        qty, price, status = c.execute("SELECT qty, entry_price, status FROM trades").fetchone()
    assert qty == 3.0 and price == 100.5 and status == "partially_filled"   # broker status, not a literal


def test_full_fill_writes_one_correct_trade(db):
    dbm.log_fill(_result(qty=10.0, filled_price=99.9, status="filled", side=Side.SELL), "s", db_path=db)
    t, o = _counts(db)
    assert t == 1 and o == 0
    with sqlite3.connect(db) as c:
        qty, price, direction, status = c.execute(
            "SELECT qty, entry_price, direction, status FROM trades").fetchone()
    assert qty == 10.0 and price == 99.9 and direction == "short" and status == "filled"


def test_regression_the_61_phantom_condition_yields_zero_trades(db):
    """Replay the exact shape that produced the 61 phantoms: a non-fill whose resolved qty
    is 0 and price 0. Even if a stale path defaults status to 'filled', the fill predicate
    (price>0 AND qty>0) must reject it -> ZERO trades rows, and the qty=0/price=0/'filled'
    phantom signature can never be written again."""
    for status in ("rejected", "expired", "canceled", "cancelled", "filled"):   # incl. mislabeled 'filled'
        dbm.log_fill(_result(qty=0.0, filled_price=0.0, status=status), "mean_reversion_qqq", db_path=db)
    t, _ = _counts(db)
    assert t == 0
    with sqlite3.connect(db) as c:
        phantoms = c.execute(
            "SELECT COUNT(*) FROM trades WHERE qty=0 AND (entry_price=0 OR entry_price IS NULL)"
        ).fetchone()[0]
    assert phantoms == 0
