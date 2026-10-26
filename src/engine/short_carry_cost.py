"""
Short borrow carry costs — live economic friction aligned with vectorized_mr.

Applies session-boundary borrow fee debits for stock short legs so sizing,
drawdown, and hold economics mirror optimization sweeps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from src.ingestor.assets import infer_asset_class
from src.models import Account, Position

ET = ZoneInfo("America/New_York")
TRADING_DAYS_PER_YEAR = 252.0
DEFAULT_SHORT_BORROW_FEE_ANNUAL = 0.005


@dataclass(frozen=True)
class ShortBorrowAccrual:
    session_key: str
    fee_amount: float
    notional: float
    annual_rate: float


def resolve_short_borrow_fee_annual(
    strategy_params: dict | None,
    *,
    default: float = DEFAULT_SHORT_BORROW_FEE_ANNUAL,
) -> float:
    if strategy_params is None:
        return default
    raw = strategy_params.get("short_borrow_fee_annual")
    if raw is None:
        return default
    return max(float(raw), 0.0)


def trading_session_key(timestamp: datetime) -> str:
    return timestamp.astimezone(ET).date().isoformat()


def compute_session_borrow_fee(
    abs_qty: float,
    mark_price: float,
    *,
    annual_rate: float,
) -> float:
    if abs_qty <= 0.0 or mark_price <= 0.0 or annual_rate <= 0.0:
        return 0.0
    notional = abs_qty * mark_price
    return notional * (annual_rate / TRADING_DAYS_PER_YEAR)


def accrue_session_short_borrow_fee(
    *,
    position: Position | None,
    symbol: str,
    asset_class: str,
    bar_timestamp: datetime,
    mark_price: float,
    annual_rate: float,
    last_billed_session: str | None,
    accrued_total: float,
) -> tuple[float, str | None, ShortBorrowAccrual | None]:
    """
    Debit one session borrow fee when a new trading day begins while short.

    Returns (updated_accrued_total, updated_last_billed_session, accrual_or_none).
    """
    if position is None or position.qty >= 0:
        return accrued_total, last_billed_session, None
    if asset_class == "crypto" or infer_asset_class(symbol) == "crypto":
        return accrued_total, last_billed_session, None

    session_key = trading_session_key(bar_timestamp)
    if last_billed_session == session_key:
        return accrued_total, last_billed_session, None

    abs_qty = abs(float(position.qty))
    fee = compute_session_borrow_fee(abs_qty, mark_price, annual_rate=annual_rate)
    if fee <= 0.0:
        return accrued_total, session_key, None

    accrual = ShortBorrowAccrual(
        session_key=session_key,
        fee_amount=fee,
        notional=abs_qty * mark_price,
        annual_rate=annual_rate,
    )
    return accrued_total + fee, session_key, accrual


def apply_borrow_drag_to_account(
    account: Account,
    borrow_fee_drag: float,
) -> Account:
    """Return an economic account view with accrued short borrow drag deducted."""
    drag = max(float(borrow_fee_drag), 0.0)
    adjusted_equity = max(0.0, account.equity - drag)
    adjusted_cash = max(0.0, account.cash - drag)
    adjusted_buying_power = max(0.0, account.buying_power - drag)
    return Account(
        equity=adjusted_equity,
        cash=adjusted_cash,
        buying_power=adjusted_buying_power,
    )


def total_short_borrow_drag(legs: dict[str, object]) -> float:
    """Sum accrued borrow drag across leg states exposing accrued_short_borrow_fees."""
    total = 0.0
    for leg in legs.values():
        total += float(getattr(leg, "accrued_short_borrow_fees", 0.0) or 0.0)
    return total
