"""Tests for live short borrow carry cost accrual."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.engine.short_carry_cost import (
    accrue_session_short_borrow_fee,
    apply_borrow_drag_to_account,
    compute_session_borrow_fee,
    trading_session_key,
)
from src.models import Account, Position


def test_compute_session_borrow_fee_matches_vectorized_rate() -> None:
    fee = compute_session_borrow_fee(100.0, 430.0, annual_rate=0.005)
    assert fee == pytest.approx(100.0 * 430.0 * (0.005 / 252.0))


def test_accrue_session_short_borrow_fee_once_per_session() -> None:
    position = Position(symbol="QQQ", qty=-50.0, side="short", avg_entry_price=430.0)
    ts = datetime(2024, 7, 2, 15, 30, tzinfo=timezone.utc)
    accrued, billed, accrual = accrue_session_short_borrow_fee(
        position=position,
        symbol="QQQ",
        asset_class="stock",
        bar_timestamp=ts,
        mark_price=430.0,
        annual_rate=0.005,
        last_billed_session=None,
        accrued_total=0.0,
    )
    assert accrual is not None
    assert accrued > 0.0
    assert billed == trading_session_key(ts)

    accrued_again, billed_again, second = accrue_session_short_borrow_fee(
        position=position,
        symbol="QQQ",
        asset_class="stock",
        bar_timestamp=ts,
        mark_price=430.0,
        annual_rate=0.005,
        last_billed_session=billed,
        accrued_total=accrued,
    )
    assert second is None
    assert accrued_again == accrued
    assert billed_again == billed


def test_apply_borrow_drag_to_account_reduces_equity() -> None:
    account = Account(equity=100_000.0, cash=100_000.0, buying_power=100_000.0)
    adjusted = apply_borrow_drag_to_account(account, 250.0)
    assert adjusted.equity == 99_750.0
    assert adjusted.buying_power == 99_750.0
