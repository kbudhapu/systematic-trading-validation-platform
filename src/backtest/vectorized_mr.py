"""Numba-accelerated mean-reversion backtest for large parameter sweeps."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
from numba import njit, prange

from src.math.indicators import EXIT, LONG, SHORT

ANNUALIZE = math.sqrt(252 * 26)
TRADING_DAYS_PER_YEAR = 252.0

SHORT_MARGIN_INITIAL = 0.50
SHORT_MARGIN_MAINT = 0.25
SHORT_BORROW_FEE_ANNUAL = 0.005
SHORT_SIZE_MULTIPLIER = 0.75
LIQUIDATION_SLIP_MULTIPLIER = 3.0
Z_PERIOD_MIN = 15
Z_PERIOD_MAX = 80
MAX_FULL_DD_SIEVE = 0.08


@dataclass(frozen=True)
class VectorizedExecParams:
    slippage_pct: float = 0.0005
    max_risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.95
    initial_equity: float = 100_000.0
    short_borrow_fee_annual: float = SHORT_BORROW_FEE_ANNUAL
    short_size_multiplier: float = SHORT_SIZE_MULTIPLIER
    short_margin_initial: float = SHORT_MARGIN_INITIAL
    short_margin_maint: float = SHORT_MARGIN_MAINT
    liquidation_slip_multiplier: float = LIQUIDATION_SLIP_MULTIPLIER
    atr_period: int = 14


STRATEGY_PARAM_LEDGER: list[tuple[str, str, str]] = [
    ("sma_period", "int", "Legacy unified SMA lookback (coupled mode)"),
    ("sma_period_long", "int", "Independent SMA lookback for long-leg z-score"),
    ("sma_period_short", "int", "Independent SMA lookback for short-leg z-score"),
    ("long_threshold_sigma", "float", "Long entry when z_long < -threshold"),
    ("short_threshold_sigma", "float", "Short entry when z_short > threshold"),
    ("exit_sigma", "float", "Exit when |z_leg| < threshold"),
    ("max_bars_in_trade", "int", "Time-stop forced exit after N bars"),
    ("regime_filter", "bool", "Gate long entries to uptrend session dates"),
]

EXEC_PARAM_LEDGER: list[tuple[str, str, Any]] = [
    ("slippage_pct", "float", VectorizedExecParams.slippage_pct),
    ("max_risk_per_trade_pct", "float", VectorizedExecParams.max_risk_per_trade_pct),
    ("max_position_pct", "float", VectorizedExecParams.max_position_pct),
    ("initial_equity", "float", VectorizedExecParams.initial_equity),
    ("short_borrow_fee_annual", "float", SHORT_BORROW_FEE_ANNUAL),
    ("short_size_multiplier", "float", SHORT_SIZE_MULTIPLIER),
    ("short_margin_initial", "float", SHORT_MARGIN_INITIAL),
    ("short_margin_maint", "float", SHORT_MARGIN_MAINT),
    ("liquidation_slip_multiplier", "float", LIQUIDATION_SLIP_MULTIPLIER),
    ("atr_period", "int", VectorizedExecParams.atr_period),
    ("trading_days_per_year", "float", TRADING_DAYS_PER_YEAR),
]


def build_session_boundary_mask(timestamps: list) -> np.ndarray:
    """Mark bars that open a new trading session (calendar day change)."""
    n = len(timestamps)
    mask = np.zeros(n, dtype=np.int8)
    if n == 0:
        return mask
    mask[0] = 1
    prev_day = timestamps[0].date()
    for i in range(1, n):
        day = timestamps[i].date()
        if day != prev_day:
            mask[i] = 1
            prev_day = day
    return mask


def build_dividend_array(
    timestamps: list,
    events: list[tuple[str, float]],
) -> np.ndarray:
    """Per-bar cash dividend per share (ex-date credited on first bar of that date)."""
    by_date: dict[str, float] = {}
    for ex_date, amount in events:
        by_date[ex_date] = by_date.get(ex_date, 0.0) + float(amount)
    arr = np.zeros(len(timestamps), dtype=np.float64)
    for i, ts in enumerate(timestamps):
        amt = by_date.get(ts.date().isoformat(), 0.0)
        if amt > 0.0:
            arr[i] = amt
    return arr


def fetch_cash_dividends(
    symbol: str,
    start: datetime,
    end: datetime,
    api_key: str,
    secret_key: str,
) -> list[tuple[str, float]]:
    """Fetch Alpaca cash dividend corporate actions for ex-date scheduling."""
    params = urllib.parse.urlencode(
        {
            "symbols": symbol,
            "types": "cash_dividend",
            "start": start.date().isoformat(),
            "end": end.date().isoformat(),
        }
    )
    url = f"https://data.alpaca.markets/v1/corporate-actions?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []

    events: list[tuple[str, float]] = []
    for entry in payload.get("cash_dividends", []) or []:
        ex_date = entry.get("ex_date") or entry.get("record_date")
        rate = entry.get("rate") or entry.get("cash")
        if ex_date and rate is not None:
            events.append((str(ex_date)[:10], float(rate)))
    return events


def precompute_z_stack(
    closes: np.ndarray,
    min_period: int = Z_PERIOD_MIN,
    max_period: int = Z_PERIOD_MAX,
) -> np.ndarray:
    """Stack z-score series for periods [min_period, max_period] inclusive."""
    count = max_period - min_period + 1
    n = len(closes)
    stack = np.zeros((count, n), dtype=np.float64)
    for p in range(min_period, max_period + 1):
        sma, std = precompute_sma_std(closes, p)
        stack[p - min_period] = precompute_z_scores(closes, sma, std)
    return stack


def z_from_stack(
    z_stack: np.ndarray, period: int, min_period: int = Z_PERIOD_MIN
) -> np.ndarray:
    return z_stack[period - min_period]


@njit(cache=True)
def precompute_z_stack_numba(
    closes: np.ndarray,
    min_period: int,
    max_period: int,
) -> np.ndarray:
    count = max_period - min_period + 1
    n = len(closes)
    stack = np.zeros((count, n), dtype=np.float64)
    for p in range(min_period, max_period + 1):
        sma, std = precompute_sma_std(closes, p)
        stack[p - min_period] = precompute_z_scores(closes, sma, std)
    return stack


@njit(cache=True)
def precompute_sma_std(closes: np.ndarray, sma_period: int) -> tuple[np.ndarray, np.ndarray]:
    n = len(closes)
    sma = np.zeros(n, dtype=np.float64)
    std = np.zeros(n, dtype=np.float64)
    if sma_period <= 0 or n < sma_period:
        return sma, std

    window_sum = 0.0
    window_sq_sum = 0.0
    for i in range(n):
        c = closes[i]
        window_sum += c
        window_sq_sum += c * c
        if i >= sma_period:
            old = closes[i - sma_period]
            window_sum -= old
            window_sq_sum -= old * old
        if i >= sma_period - 1:
            mean = window_sum / sma_period
            var = window_sq_sum / sma_period - mean * mean
            if var < 0.0:
                var = 0.0
            sma[i] = mean
            std[i] = var ** 0.5
    return sma, std


@njit(cache=True)
def precompute_z_scores(closes: np.ndarray, sma: np.ndarray, std: np.ndarray) -> np.ndarray:
    n = len(closes)
    z = np.zeros(n, dtype=np.float64)
    for i in range(n):
        if std[i] > 0.0:
            z[i] = (closes[i] - sma[i]) / std[i]
    return z


@njit(cache=True)
def precompute_atr_series(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int,
) -> np.ndarray:
    n = len(closes)
    atr = np.zeros(n, dtype=np.float64)
    if period <= 0 or n < period:
        return atr
    for i in range(period - 1, n):
        tr_sum = 0.0
        for j in range(i - period + 1, i + 1):
            if j == 0:
                tr = highs[j] - lows[j]
            else:
                hl = highs[j] - lows[j]
                hc = abs(highs[j] - closes[j - 1])
                lc = abs(lows[j] - closes[j - 1])
                tr = max(hl, hc, lc)
            tr_sum += tr
        atr[i] = tr_sum / period
    return atr


@njit(cache=True)
def _size_shares(
    equity: float,
    cash: float,
    atr_val: float,
    price: float,
    max_risk_pct: float,
    max_pos_pct: float,
) -> int:
    if atr_val <= 0.0 or price <= 0.0 or equity <= 0.0:
        return 0
    risk_dollars = equity * max_risk_pct
    by_risk = risk_dollars / atr_val
    by_buying_power = cash / price
    by_concentration = (equity * max_pos_pct) / price
    shares = min(by_risk, by_buying_power, by_concentration)
    if shares < 1.0:
        return 0
    return int(shares)


@njit(cache=True)
def _size_shares_short(
    equity: float,
    atr_val: float,
    price: float,
    max_risk_pct: float,
    max_pos_pct: float,
    initial_margin_pct: float,
    short_size_multiplier: float,
) -> int:
    if atr_val <= 0.0 or price <= 0.0 or equity <= 0.0 or initial_margin_pct <= 0.0:
        return 0
    risk_dollars = equity * max_risk_pct
    by_risk = risk_dollars / atr_val
    by_concentration = (equity * max_pos_pct) / price
    by_margin = equity / (price * initial_margin_pct)
    shares = min(by_risk, by_concentration, by_margin) * short_size_multiplier
    if shares < 1.0:
        return 0
    return int(shares)


@njit(cache=True)
def _mark_equity(cash: float, position_qty: float, mark_price: float) -> float:
    return cash + position_qty * mark_price


@njit(cache=True)
def _apply_borrow_fee(
    cash: float,
    position_qty: float,
    mark_notional_price: float,
    borrow_fee_annual: float,
) -> float:
    if position_qty >= 0.0:
        return cash
    notional = abs(position_qty) * mark_notional_price
    daily_fee = notional * (borrow_fee_annual / TRADING_DAYS_PER_YEAR)
    return cash - daily_fee


@njit(cache=True)
def _cover_short(
    cash: float,
    position_qty: float,
    entry_cost: float,
    fill_raw: float,
    slippage_pct: float,
    slip_multiplier: float,
) -> tuple[float, float, float, float, float, int]:
    """Return cash, position_qty, entry_cost, pnl_win, pnl_loss, fills."""
    slip = fill_raw * slippage_pct * slip_multiplier
    price = fill_raw + slip
    cover_cost = price * abs(position_qty)
    pnl = entry_cost - cover_cost
    win = 0.0
    loss = 0.0
    if pnl > 0.0:
        win = pnl
    elif pnl < 0.0:
        loss = -pnl
    cash -= cover_cost
    return cash, 0.0, 0.0, win, loss, 1


@njit(cache=True)
def simulate_mr_slice(
    opens: np.ndarray,
    closes: np.ndarray,
    z_scores: np.ndarray,
    regime_mask: np.ndarray,
    atr: np.ndarray,
    session_boundary: np.ndarray,
    dividend_per_share: np.ndarray,
    sma_period: int,
    long_th: float,
    short_th: float,
    exit_sigma: float,
    max_bars: int,
    slippage_pct: float,
    max_risk_pct: float,
    max_pos_pct: float,
    initial_equity: float,
    short_borrow_fee_annual: float,
    short_size_multiplier: float,
    short_margin_initial: float,
    short_margin_maint: float,
    liquidation_slip_multiplier: float,
    start: int,
    end: int,
) -> tuple[float, float, float, int, float]:
    """Return total_return, sharpe, max_drawdown, trades, profit_factor for [start, end)."""
    n = end - start
    if n < sma_period + 2:
        return 0.0, 0.0, 0.0, 0, 0.0

    cash = initial_equity
    position_qty = 0.0
    bars_in_trade = 0
    pending_action = 0
    n_fills = 0
    gross_win = 0.0
    gross_loss = 0.0
    entry_cost = 0.0
    liquidated = False

    equity_start = initial_equity
    peak = initial_equity
    max_dd = 0.0
    ret_sum = 0.0
    ret_sq_sum = 0.0
    ret_count = 0
    prev_equity = initial_equity

    for local_i in range(n):
        i = start + local_i
        liquidated = False

        if local_i > 0 and pending_action != 0:
            fill_raw = opens[i]
            slip = fill_raw * slippage_pct
            if pending_action == LONG and position_qty == 0.0:
                price = fill_raw + slip
                mark_eq = _mark_equity(cash, position_qty, closes[i - 1])
                qty = _size_shares(
                    mark_eq,
                    cash,
                    atr[i - 1],
                    price,
                    max_risk_pct,
                    max_pos_pct,
                )
                if qty > 0:
                    cost = price * qty
                    cash -= cost
                    position_qty = float(qty)
                    entry_cost = cost
                    n_fills += 1
            elif pending_action == SHORT and position_qty == 0.0:
                price = fill_raw - slip
                mark_eq = _mark_equity(cash, position_qty, closes[i - 1])
                qty = _size_shares_short(
                    mark_eq,
                    atr[i - 1],
                    price,
                    max_risk_pct,
                    max_pos_pct,
                    short_margin_initial,
                    short_size_multiplier,
                )
                if qty > 0:
                    proceeds = price * qty
                    cash += proceeds
                    position_qty = -float(qty)
                    entry_cost = proceeds
                    n_fills += 1
            elif pending_action == EXIT:
                if position_qty > 0.0:
                    price = fill_raw - slip
                    proceeds = price * position_qty
                    pnl = proceeds - entry_cost
                    if pnl > 0.0:
                        gross_win += pnl
                    elif pnl < 0.0:
                        gross_loss -= pnl
                    cash += proceeds
                    position_qty = 0.0
                    entry_cost = 0.0
                    n_fills += 1
                elif position_qty < 0.0:
                    cash, position_qty, entry_cost, win, loss, fills = _cover_short(
                        cash,
                        position_qty,
                        entry_cost,
                        fill_raw,
                        slippage_pct,
                        1.0,
                    )
                    gross_win += win
                    gross_loss += loss
                    n_fills += fills
            pending_action = 0

        if local_i > 0 and session_boundary[i] == 1 and position_qty < 0.0:
            cash = _apply_borrow_fee(
                cash,
                position_qty,
                closes[i - 1],
                short_borrow_fee_annual,
            )

        if position_qty < 0.0 and dividend_per_share[i] > 0.0:
            cash -= abs(position_qty) * dividend_per_share[i]

        if position_qty != 0.0:
            bars_in_trade += 1
        else:
            bars_in_trade = 0

        equity = _mark_equity(cash, position_qty, closes[i])

        if position_qty < 0.0:
            liability = abs(position_qty) * closes[i]
            maint_req = liability * short_margin_maint
            if equity < maint_req:
                cash, position_qty, entry_cost, win, loss, fills = _cover_short(
                    cash,
                    position_qty,
                    entry_cost,
                    closes[i],
                    slippage_pct,
                    liquidation_slip_multiplier,
                )
                gross_win += win
                gross_loss += loss
                n_fills += fills
                bars_in_trade = 0
                equity = _mark_equity(cash, position_qty, closes[i])
                liquidated = True

        if local_i >= sma_period - 1 and pending_action == 0 and not liquidated:
            if position_qty != 0.0 and bars_in_trade >= max_bars:
                pending_action = EXIT
            else:
                z = z_scores[i]
                if position_qty == 0.0 and z < -long_th and regime_mask[i] == 1:
                    pending_action = LONG
                elif position_qty == 0.0 and z > short_th:
                    pending_action = SHORT
                elif position_qty != 0.0 and abs(z) < exit_sigma:
                    pending_action = EXIT

        if equity > peak:
            peak = equity
        if peak > 0.0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
        if local_i > 0 and prev_equity > 0.0:
            r = (equity - prev_equity) / prev_equity
            ret_sum += r
            ret_sq_sum += r * r
            ret_count += 1
        prev_equity = equity

    equity_end = _mark_equity(cash, position_qty, closes[end - 1])
    if equity_start > 0.0:
        total_return = (equity_end - equity_start) / equity_start
    else:
        total_return = 0.0

    if ret_count > 1:
        mean = ret_sum / ret_count
        var = (ret_sq_sum / ret_count) - mean * mean
        if var < 0.0:
            var = 0.0
        std = var ** 0.5
        sharpe = (mean / std) * ANNUALIZE if std > 0.0 else 0.0
    else:
        sharpe = 0.0

    if gross_loss > 0.0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0.0:
        profit_factor = 1e9
    else:
        profit_factor = 0.0

    return total_return, sharpe, max_dd, n_fills, profit_factor


@njit(cache=True)
def simulate_mr_slice_decoupled(
    opens: np.ndarray,
    closes: np.ndarray,
    z_long: np.ndarray,
    z_short: np.ndarray,
    regime_mask: np.ndarray,
    atr: np.ndarray,
    session_boundary: np.ndarray,
    dividend_per_share: np.ndarray,
    sma_period_long: int,
    sma_period_short: int,
    long_th: float,
    short_th: float,
    exit_sigma: float,
    max_bars: int,
    slippage_pct: float,
    max_risk_pct: float,
    max_pos_pct: float,
    initial_equity: float,
    short_borrow_fee_annual: float,
    short_size_multiplier: float,
    short_margin_initial: float,
    short_margin_maint: float,
    liquidation_slip_multiplier: float,
    start: int,
    end: int,
) -> tuple[float, float, float, int, float, int, int]:
    """Decoupled long/short horizons — independent z_long and z_short series."""
    n = end - start
    warmup = sma_period_long
    if sma_period_short > warmup:
        warmup = sma_period_short
    if n < warmup + 2:
        return 0.0, 0.0, 0.0, 0, 0.0, 0, 0

    cash = initial_equity
    position_qty = 0.0
    bars_in_trade = 0
    pending_action = 0
    n_fills = 0
    n_fills_long = 0
    n_fills_short = 0
    gross_win = 0.0
    gross_loss = 0.0
    entry_cost = 0.0
    liquidated = False

    equity_start = initial_equity
    peak = initial_equity
    max_dd = 0.0
    ret_sum = 0.0
    ret_sq_sum = 0.0
    ret_count = 0
    prev_equity = initial_equity

    for local_i in range(n):
        i = start + local_i
        liquidated = False

        if local_i > 0 and pending_action != 0:
            fill_raw = opens[i]
            slip = fill_raw * slippage_pct
            if pending_action == LONG and position_qty == 0.0:
                price = fill_raw + slip
                mark_eq = _mark_equity(cash, position_qty, closes[i - 1])
                qty = _size_shares(
                    mark_eq,
                    cash,
                    atr[i - 1],
                    price,
                    max_risk_pct,
                    max_pos_pct,
                )
                if qty > 0:
                    cost = price * qty
                    cash -= cost
                    position_qty = float(qty)
                    entry_cost = cost
                    n_fills += 1
                    n_fills_long += 1
            elif pending_action == SHORT and position_qty == 0.0:
                price = fill_raw - slip
                mark_eq = _mark_equity(cash, position_qty, closes[i - 1])
                qty = _size_shares_short(
                    mark_eq,
                    atr[i - 1],
                    price,
                    max_risk_pct,
                    max_pos_pct,
                    short_margin_initial,
                    short_size_multiplier,
                )
                if qty > 0:
                    proceeds = price * qty
                    cash += proceeds
                    position_qty = -float(qty)
                    entry_cost = proceeds
                    n_fills += 1
                    n_fills_short += 1
            elif pending_action == EXIT:
                if position_qty > 0.0:
                    price = fill_raw - slip
                    proceeds = price * position_qty
                    pnl = proceeds - entry_cost
                    if pnl > 0.0:
                        gross_win += pnl
                    elif pnl < 0.0:
                        gross_loss -= pnl
                    cash += proceeds
                    position_qty = 0.0
                    entry_cost = 0.0
                    n_fills += 1
                elif position_qty < 0.0:
                    cash, position_qty, entry_cost, win, loss, fills = _cover_short(
                        cash,
                        position_qty,
                        entry_cost,
                        fill_raw,
                        slippage_pct,
                        1.0,
                    )
                    gross_win += win
                    gross_loss += loss
                    n_fills += fills
            pending_action = 0

        if local_i > 0 and session_boundary[i] == 1 and position_qty < 0.0:
            cash = _apply_borrow_fee(
                cash,
                position_qty,
                closes[i - 1],
                short_borrow_fee_annual,
            )

        if position_qty < 0.0 and dividend_per_share[i] > 0.0:
            cash -= abs(position_qty) * dividend_per_share[i]

        if position_qty != 0.0:
            bars_in_trade += 1
        else:
            bars_in_trade = 0

        equity = _mark_equity(cash, position_qty, closes[i])

        if position_qty < 0.0:
            liability = abs(position_qty) * closes[i]
            maint_req = liability * short_margin_maint
            if equity < maint_req:
                cash, position_qty, entry_cost, win, loss, fills = _cover_short(
                    cash,
                    position_qty,
                    entry_cost,
                    closes[i],
                    slippage_pct,
                    liquidation_slip_multiplier,
                )
                gross_win += win
                gross_loss += loss
                n_fills += fills
                bars_in_trade = 0
                equity = _mark_equity(cash, position_qty, closes[i])
                liquidated = True

        if local_i >= warmup - 1 and pending_action == 0 and not liquidated:
            if position_qty != 0.0 and bars_in_trade >= max_bars:
                pending_action = EXIT
            else:
                zl = z_long[i]
                zs = z_short[i]
                if position_qty == 0.0 and zl < -long_th and regime_mask[i] == 1:
                    pending_action = LONG
                elif position_qty == 0.0 and zs > short_th:
                    pending_action = SHORT
                elif position_qty > 0.0 and abs(zl) < exit_sigma:
                    pending_action = EXIT
                elif position_qty < 0.0 and abs(zs) < exit_sigma:
                    pending_action = EXIT

        if equity > peak:
            peak = equity
        if peak > 0.0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
        if local_i > 0 and prev_equity > 0.0:
            r = (equity - prev_equity) / prev_equity
            ret_sum += r
            ret_sq_sum += r * r
            ret_count += 1
        prev_equity = equity

    equity_end = _mark_equity(cash, position_qty, closes[end - 1])
    if equity_start > 0.0:
        total_return = (equity_end - equity_start) / equity_start
    else:
        total_return = 0.0

    if ret_count > 1:
        mean = ret_sum / ret_count
        var = (ret_sq_sum / ret_count) - mean * mean
        if var < 0.0:
            var = 0.0
        std = var ** 0.5
        sharpe = (mean / std) * ANNUALIZE if std > 0.0 else 0.0
    else:
        sharpe = 0.0

    if gross_loss > 0.0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0.0:
        profit_factor = 1e9
    else:
        profit_factor = 0.0

    return total_return, sharpe, max_dd, n_fills, profit_factor, n_fills_long, n_fills_short


@njit(parallel=True, cache=True)
def sweep_decoupled_inner_grid(
    opens: np.ndarray,
    closes: np.ndarray,
    z_long: np.ndarray,
    z_short: np.ndarray,
    regime_mask: np.ndarray,
    atr: np.ndarray,
    session_boundary: np.ndarray,
    dividend_per_share: np.ndarray,
    sma_period_long: int,
    sma_period_short: int,
    long_grid: np.ndarray,
    short_grid: np.ndarray,
    exit_grid: np.ndarray,
    max_bars_grid: np.ndarray,
    slippage_pct: float,
    max_risk_pct: float,
    max_pos_pct: float,
    initial_equity: float,
    short_borrow_fee_annual: float,
    short_size_multiplier: float,
    short_margin_initial: float,
    short_margin_maint: float,
    liquidation_slip_multiplier: float,
    start: int,
    end: int,
    out_returns: np.ndarray,
    out_sharpes: np.ndarray,
    out_dds: np.ndarray,
    out_trades: np.ndarray,
    out_pf: np.ndarray,
    out_trades_long: np.ndarray,
    out_trades_short: np.ndarray,
) -> None:
    n_long = len(long_grid)
    n_short = len(short_grid)
    n_exit = len(exit_grid)
    n_max = len(max_bars_grid)
    n_inner = n_long * n_short * n_exit * n_max

    for idx in prange(n_inner):
        mi = idx % n_max
        t = idx // n_max
        ei = t % n_exit
        t = t // n_exit
        si = t % n_short
        li = t // n_short
        ret, sharpe, dd, trades, pf, tl, ts = simulate_mr_slice_decoupled(
            opens,
            closes,
            z_long,
            z_short,
            regime_mask,
            atr,
            session_boundary,
            dividend_per_share,
            sma_period_long,
            sma_period_short,
            long_grid[li],
            short_grid[si],
            exit_grid[ei],
            int(max_bars_grid[mi]),
            slippage_pct,
            max_risk_pct,
            max_pos_pct,
            initial_equity,
            short_borrow_fee_annual,
            short_size_multiplier,
            short_margin_initial,
            short_margin_maint,
            liquidation_slip_multiplier,
            start,
            end,
        )
        out_returns[idx] = ret
        out_sharpes[idx] = sharpe
        out_dds[idx] = dd
        out_trades[idx] = trades
        out_pf[idx] = pf
        out_trades_long[idx] = tl
        out_trades_short[idx] = ts


def simulate_mr_slice_decoupled_exec(
    opens: np.ndarray,
    closes: np.ndarray,
    z_long: np.ndarray,
    z_short: np.ndarray,
    regime_mask: np.ndarray,
    atr: np.ndarray,
    session_boundary: np.ndarray,
    dividend_per_share: np.ndarray,
    sma_period_long: int,
    sma_period_short: int,
    long_th: float,
    short_th: float,
    exit_sigma: float,
    max_bars: int,
    exec_params: VectorizedExecParams,
    start: int,
    end: int,
) -> tuple[float, float, float, int, float, int, int]:
    return simulate_mr_slice_decoupled(
        opens,
        closes,
        z_long,
        z_short,
        regime_mask,
        atr,
        session_boundary,
        dividend_per_share,
        sma_period_long,
        sma_period_short,
        long_th,
        short_th,
        exit_sigma,
        max_bars,
        exec_params.slippage_pct,
        exec_params.max_risk_per_trade_pct,
        exec_params.max_position_pct,
        exec_params.initial_equity,
        exec_params.short_borrow_fee_annual,
        exec_params.short_size_multiplier,
        exec_params.short_margin_initial,
        exec_params.short_margin_maint,
        exec_params.liquidation_slip_multiplier,
        start,
        end,
    )


def decode_inner_index(
    idx: int,
    n_long: int,
    n_short: int,
    n_exit: int,
    n_max: int,
) -> tuple[int, int, int, int]:
    mi = idx % n_max
    t = idx // n_max
    ei = t % n_exit
    t = t // n_exit
    si = t % n_short
    li = t // n_short
    return li, si, ei, mi


def simulate_mr_slice_exec(
    opens: np.ndarray,
    closes: np.ndarray,
    z_scores: np.ndarray,
    regime_mask: np.ndarray,
    atr: np.ndarray,
    session_boundary: np.ndarray,
    dividend_per_share: np.ndarray,
    sma_period: int,
    long_th: float,
    short_th: float,
    exit_sigma: float,
    max_bars: int,
    exec_params: VectorizedExecParams,
    start: int,
    end: int,
) -> tuple[float, float, float, int, float]:
    """Python wrapper binding execution defaults into the Numba kernel."""
    return simulate_mr_slice(
        opens,
        closes,
        z_scores,
        regime_mask,
        atr,
        session_boundary,
        dividend_per_share,
        sma_period,
        long_th,
        short_th,
        exit_sigma,
        max_bars,
        exec_params.slippage_pct,
        exec_params.max_risk_per_trade_pct,
        exec_params.max_position_pct,
        exec_params.initial_equity,
        exec_params.short_borrow_fee_annual,
        exec_params.short_size_multiplier,
        exec_params.short_margin_initial,
        exec_params.short_margin_maint,
        exec_params.liquidation_slip_multiplier,
        start,
        end,
    )


def print_system_state_report() -> None:
    """Emit feature inventory and accounting trace to stdout."""
    sep = "=" * 72
    print(sep)
    print("SYSTEM STATE & FEATURE INVENTORY REPORT — vectorized_mr.py")
    print(sep)

    print("\nA. MODIFIED LOGIC BLOCKS")
    print("-" * 72)
    print(
        "  [Decoupled horizons] sma_period_long drives z_long; sma_period_short "
        "drives z_short; exits use the active leg's z-score."
    )
    print(
        "  [Borrow fees] At each session-boundary bar (calendar day change) while "
        "short, cash is debited by notional * (short_borrow_fee_annual / 252) "
        "using the prior bar close as mark."
    )
    print(
        "  [Short down-sizing] _size_shares_short scales ATR/margin/concentration "
        "sizing by short_size_multiplier (default 0.75) before int truncation."
    )
    print(
        "  [Dividends] dividend_per_share[i] > 0 on ex-date bars debits "
        "abs(position_qty) * dividend when short — short seller dividend liability."
    )
    print(
        "  [Margin liquidation] Maintenance breach (equity < 25% of short "
        "liability) triggers immediate cover at bar close via _cover_short with "
        "liquidation_slip_multiplier * slippage (default 3x), not next-bar open."
    )
    print(
        "  [Long/short book] Signed position_qty; equity = cash + position_qty * "
        "mark; long fills at next open; short open/cover symmetric with slippage."
    )

    print("\nB. GLOBAL STATE PARAMETER LEDGER")
    print("-" * 72)
    print("  Strategy parameters (per sweep combo):")
    for name, dtype, note in STRATEGY_PARAM_LEDGER:
        print(f"    {name:<28} {dtype:<8}  {note}")
    print("  Execution / physics parameters (VectorizedExecParams):")
    for name, dtype, default in EXEC_PARAM_LEDGER:
        print(f"    {name:<28} {dtype:<8}  default={default}")

    print("\nC. ACCOUNTING FLOW (single bar, active short)")
    print("-" * 72)
    steps = [
        "1. If pending signal from prior close, fill at this bar OPEN (long buy, "
        "short sell, or scheduled exit).",
        "2. If session_boundary[i] and still short, debit overnight borrow fee on "
        "prior-close notional.",
        "3. If ex-dividend amount on this bar, debit abs(qty) * dividend_per_share.",
        "4. Increment bars_in_trade when |position_qty| > 0.",
        "5. Mark equity = cash + position_qty * close[i].",
        "6. If short and equity < maintenance requirement, intra-bar cover at "
        "close[i] with penalized slippage; reset position.",
        "7. Else evaluate z-score signals for next-bar pending action (entry, "
        "exit_sigma exit, or max_bars time-stop).",
        "8. Update peak equity, drawdown, and bar return statistics.",
    ]
    for step in steps:
        print(f"  {step}")
    print(sep)


if __name__ == "__main__":
    print_system_state_report()
