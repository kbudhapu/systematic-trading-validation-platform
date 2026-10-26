"""
Numba-compiled technical indicators.

All indicator math runs on contiguous numpy arrays extracted from RollingWindow
ring buffers. Polars is never used here — only array structures per .cursorrules.
"""

from __future__ import annotations

import numpy as np
from numba import njit

# Signal action codes consumed by strategy modules.
HOLD = 0
LONG = 1
SHORT = 2
EXIT = 3


@njit(cache=True)
def rolling_sma(arr: np.ndarray, period: int) -> float:
    """Simple moving average over the trailing `period` elements of `arr`."""
    n = len(arr)
    if n < period or period <= 0:
        return np.nan
    total = 0.0
    for i in range(n - period, n):
        total += arr[i]
    return total / period


@njit(cache=True)
def rolling_std(arr: np.ndarray, period: int) -> float:
    """Population standard deviation over the trailing `period` elements of `arr`."""
    n = len(arr)
    if n < period or period <= 0:
        return np.nan
    mean = rolling_sma(arr, period)
    var = 0.0
    for i in range(n - period, n):
        diff = arr[i] - mean
        var += diff * diff
    return (var / period) ** 0.5


@njit(cache=True)
def compute_atr(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int,
) -> float:
    """Average True Range over the trailing `period` bars."""
    n = len(closes)
    if n < period + 1 or period <= 0:
        return 0.0
    tr_sum = 0.0
    for i in range(n - period, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1]) if i > 0 else hl
        lc = abs(lows[i] - closes[i - 1]) if i > 0 else hl
        tr = max(hl, hc, lc)
        tr_sum += tr
    return tr_sum / period


@njit(cache=True)
def mean_reversion_action(
    close: float,
    sma: float,
    std: float,
    threshold: float,
    exit_sigma: float,
) -> int:
    """Map z-score distance from SMA to a HOLD/LONG/SHORT/EXIT action code."""
    if std <= 0.0 or np.isnan(std) or np.isnan(sma):
        return HOLD
    z = (close - sma) / std
    if z < -threshold:
        return LONG
    if z > threshold:
        return SHORT
    if abs(z) < exit_sigma:
        return EXIT
    return HOLD


@njit(cache=True)
def vwap_bands_last(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    period: int,
) -> tuple[float, float]:
    """Rolling VWAP and its volume-weighted deviation over the trailing `period`
    bars, CAUSAL: uses only the last `period` elements (indices n-period .. n-1),
    never any bar after the current one. Typical price = (high+low+close)/3.

    Returns (vwap, vwstd). If total volume over the window is zero (or the array
    is shorter than `period`), returns (nan, nan).
    """
    n = len(closes)
    if n < period or period <= 0:
        return (np.nan, np.nan)
    vol_sum = 0.0
    pv_sum = 0.0
    for i in range(n - period, n):
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        vol_sum += volumes[i]
        pv_sum += tp * volumes[i]
    if vol_sum <= 0.0:
        return (np.nan, np.nan)
    vwap = pv_sum / vol_sum
    var = 0.0
    for i in range(n - period, n):
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        diff = tp - vwap
        var += volumes[i] * diff * diff
    vwstd = (var / vol_sum) ** 0.5
    return (vwap, vwstd)


@njit(cache=True)
def vwap_mr_action(
    close: float,
    vwap: float,
    vwstd: float,
    deviation_sigma: float,
    exit_sigma: float,
    position_side: int,
    enable_long: int,
    long_deviation_sigma: float,
) -> int:
    """VWAP mean-reversion action code (SHORT-biased).

    z = (close - vwap) / vwstd (distance above/below VWAP in volume-weighted
    deviations). position_side: 0 flat, 1 long, -1 short.

    Flat: SHORT when z >= deviation_sigma (fade a rally above VWAP). LONG only
    when enable_long != 0 AND z <= -long_deviation_sigma -- the long side is a
    separate, independently-gated path (default off), NOT the mirror of short.
    In position: EXIT when |z| <= exit_sigma (reverted toward VWAP). The
    holding-period cap is enforced by the caller/sim, not here.
    """
    if np.isnan(vwap) or np.isnan(vwstd) or vwstd <= 0.0:
        return HOLD
    z = (close - vwap) / vwstd
    if position_side == 0:
        if z >= deviation_sigma:
            return SHORT
        if enable_long != 0 and z <= -long_deviation_sigma:
            return LONG
    else:
        if abs(z) <= exit_sigma:
            return EXIT
    return HOLD


@njit(cache=True)
def mean_reversion_action_decoupled(
    z_long: float,
    z_short: float,
    long_threshold: float,
    short_threshold: float,
    exit_sigma: float,
    position_side: int,
) -> int:
    """
    Decoupled horizons: long z for long entries/exits, short z for short entries/exits.

    position_side: 0 flat, 1 long, -1 short
    """
    if np.isnan(z_long) or np.isnan(z_short):
        return HOLD
    if position_side == 0:
        if z_long < -long_threshold:
            return LONG
        if z_short > short_threshold:
            return SHORT
    elif position_side > 0:
        if abs(z_long) < exit_sigma:
            return EXIT
    elif position_side < 0:
        if abs(z_short) < exit_sigma:
            return EXIT
    return HOLD


@njit(cache=True)
def mean_reversion_action_asymmetric(
    close: float,
    sma: float,
    std: float,
    long_threshold: float,
    short_threshold: float,
    exit_sigma: float,
) -> int:
    """
    Asymmetric mean reversion: wider long entry, tighter short entry.

    Long when z < -long_threshold (fade panic sell-offs).
    Short when z > short_threshold (fade extended rallies).
    """
    if std <= 0.0 or np.isnan(std) or np.isnan(sma):
        return HOLD
    z = (close - sma) / std
    if z < -long_threshold:
        return LONG
    if z > short_threshold:
        return SHORT
    if abs(z) < exit_sigma:
        return EXIT
    return HOLD


@njit(cache=True)
def scan_mean_reversion_signals_asymmetric(
    closes: np.ndarray,
    sma_period: int,
    long_threshold: float,
    short_threshold: float,
    exit_sigma: float,
    max_bars_in_trade: int = 0,
) -> np.ndarray:
    """Batch asymmetric mean-reversion scan for research backtests.

    ``max_bars_in_trade`` > 0 enforces the live path's time-stop, matching
    evaluate_mean_reversion (checked BEFORE the per-bar action). The asymmetric
    action itself is position-unaware, so the bar clock resets only on an actual
    position-side change (a repeated same-side signal does not reset it, mirroring
    the orchestrator, which files no new fill when already on that side). Default 0
    disables the time-stop (backward-compatible with flat-book research callers).
    """
    n = len(closes)
    actions = np.zeros(n, dtype=np.int8)
    if sma_period <= 0 or n < sma_period:
        return actions

    window_sum = 0.0
    window_sq_sum = 0.0
    position_side = 0
    bars_in_trade = 0

    for i in range(n):
        c = closes[i]
        window_sum += c
        window_sq_sum += c * c

        if i >= sma_period:
            old = closes[i - sma_period]
            window_sum -= old
            window_sq_sum -= old * old

        if i >= sma_period - 1:
            # time-stop first (live parity: checked before the per-bar action)
            if max_bars_in_trade > 0 and position_side != 0:
                bars_in_trade += 1
                if bars_in_trade >= max_bars_in_trade:
                    actions[i] = EXIT
                    position_side = 0
                    bars_in_trade = 0
                    continue

            sma = window_sum / sma_period
            var = window_sq_sum / sma_period - sma * sma
            if var < 0.0:
                var = 0.0
            std = var ** 0.5
            code = mean_reversion_action_asymmetric(
                c, sma, std, long_threshold, short_threshold, exit_sigma
            )
            if max_bars_in_trade > 0:
                if code == LONG:
                    if position_side != 1:
                        bars_in_trade = 0
                    position_side = 1
                elif code == SHORT:
                    if position_side != -1:
                        bars_in_trade = 0
                    position_side = -1
                elif code == EXIT:
                    position_side = 0
                    bars_in_trade = 0
            actions[i] = code

    return actions


@njit(cache=True)
def scan_mean_reversion_signals_decoupled(
    closes: np.ndarray,
    sma_period_long: int,
    sma_period_short: int,
    long_threshold: float,
    short_threshold: float,
    exit_sigma: float,
    max_bars_in_trade: int = 0,
) -> np.ndarray:
    """Batch decoupled mean-reversion scan (flat-book signal research).

    ``max_bars_in_trade`` > 0 enforces the live path's time-stop: a position held
    that many bars is force-EXITed, matching evaluate_mean_reversion (the time-stop
    is checked BEFORE the entry/exit action, exactly as the live path does). The
    bar clock resets on any position-side change and never during warmup. Default 0
    disables the time-stop (backward-compatible with flat-book research callers).
    """
    n = len(closes)
    actions = np.zeros(n, dtype=np.int8)
    max_period = sma_period_long
    if sma_period_short > max_period:
        max_period = sma_period_short
    if max_period <= 0 or n < max_period:
        return actions

    position_side = 0
    bars_in_trade = 0
    for i in range(n):
        if i < sma_period_long - 1 or i < sma_period_short - 1:
            continue

        # time-stop first (live parity: checked before the entry/exit action)
        if max_bars_in_trade > 0 and position_side != 0:
            bars_in_trade += 1
            if bars_in_trade >= max_bars_in_trade:
                actions[i] = EXIT
                position_side = 0
                bars_in_trade = 0
                continue

        cl = closes[i - sma_period_long + 1 : i + 1]
        cs = closes[i - sma_period_short + 1 : i + 1]
        sma_l = rolling_sma(cl, sma_period_long)
        std_l = rolling_std(cl, sma_period_long)
        sma_s = rolling_sma(cs, sma_period_short)
        std_s = rolling_std(cs, sma_period_short)
        if std_l <= 0.0 or std_s <= 0.0:
            continue
        z_long = (closes[i] - sma_l) / std_l
        z_short = (closes[i] - sma_s) / std_s
        code = mean_reversion_action_decoupled(
            z_long,
            z_short,
            long_threshold,
            short_threshold,
            exit_sigma,
            position_side,
        )
        if code == LONG:
            if position_side != 1:
                bars_in_trade = 0
            position_side = 1
        elif code == SHORT:
            if position_side != -1:
                bars_in_trade = 0
            position_side = -1
        elif code == EXIT:
            position_side = 0
            bars_in_trade = 0
        actions[i] = code

    return actions


@njit(cache=True)
def scan_mean_reversion_signals(
    closes: np.ndarray,
    sma_period: int,
    threshold: float,
    exit_sigma: float,
) -> np.ndarray:
    """
    O(n) symmetric batch scan for backtest — sliding-window SMA/variance.

    E5/R5: this is the SYMMETRIC special case of the general single-window scan
    (`scan_mean_reversion_signals_asymmetric`) — equal long/short thresholds and no
    time-stop. It delegates so the sliding-window arithmetic and the z→action map
    live in exactly ONE place. Bit-identical to the pre-E5 standalone body: the
    asymmetric action with long_threshold == short_threshold reduces to
    `mean_reversion_action`, and `max_bars_in_trade=0` disables all position/
    time-stop bookkeeping (proven over the QQQ+SPY SIP decade in
    tests/test_mr_kernel_equivalence.py). Signature preserved for callers.

    Returns an int8 action code per bar (HOLD=0, LONG=1, SHORT=2, EXIT=3).
    """
    return scan_mean_reversion_signals_asymmetric(
        closes, sma_period, threshold, threshold, exit_sigma, 0
    )


@njit(cache=True)
def ema_last(closes: np.ndarray, period: int) -> float:
    """Exponential moving average evaluated at the last bar."""
    n = len(closes)
    if n < period or period <= 0:
        return np.nan
    total = 0.0
    for i in range(period):
        total += closes[i]
    ema = total / period
    alpha = 2.0 / (period + 1.0)
    for i in range(period, n):
        ema = alpha * closes[i] + (1.0 - alpha) * ema
    return ema


VOLUME_CONFIRMATION_RATIO = 1.5


def breakout_action(
    close: float,
    highs: np.ndarray,
    lows: np.ndarray,
    lookback: int,
    volumes: np.ndarray | None = None,
    volume_sma_period: int = 20,
) -> int:
    """Donchian-style breakout on prior `lookback` bars (excludes current bar).

    When `volumes` is provided, LONG signals are suppressed if the latest volume
    is below VOLUME_CONFIRMATION_RATIO * SMA(volume, volume_sma_period).
    EXIT signals are never gated by volume.
    """
    n = len(highs)
    if n < lookback + 1 or lookback <= 0:
        return HOLD
    start = n - lookback - 1
    end = n - 1
    max_high = highs[start]
    min_low = lows[start]
    for i in range(start + 1, end):
        if highs[i] > max_high:
            max_high = highs[i]
        if lows[i] < min_low:
            min_low = lows[i]
    if close < min_low:
        return EXIT
    if close > max_high:
        if volumes is not None and len(volumes) > 0:
            current_vol = volumes[-1]
            prior_vols = volumes[:-1]
            vol_sma = rolling_sma(prior_vols, volume_sma_period)
            if vol_sma > 0.0 and current_vol < VOLUME_CONFIRMATION_RATIO * vol_sma:
                return HOLD
        return LONG
    return HOLD


@njit(cache=True)
def trend_cross_action(fast_ema: float, slow_ema: float) -> int:
    """EMA crossover: long when fast > slow, short when fast < slow."""
    if np.isnan(fast_ema) or np.isnan(slow_ema):
        return HOLD
    if fast_ema > slow_ema:
        return LONG
    if fast_ema < slow_ema:
        return SHORT
    return HOLD
