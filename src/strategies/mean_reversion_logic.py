"""Shared mean-reversion evaluation — asymmetric entries and time-stops."""

from __future__ import annotations

from dataclasses import dataclass

from src.core.rolling_window import RollingWindow
from src.math.indicators import (
    EXIT,
    LONG,
    SHORT,
    mean_reversion_action_asymmetric,
    mean_reversion_action_decoupled,
    rolling_sma,
    rolling_std,
)
from src.models import Signal, SignalAction


@dataclass(frozen=True)
class MeanReversionDefaults:
    symbol: str
    sma_period: int
    long_threshold_sigma: float
    short_threshold_sigma: float
    exit_sigma: float
    max_bars_in_trade: int = 40
    sma_period_long: int | None = None
    sma_period_short: int | None = None


def _resolved_params(params: dict, defaults: MeanReversionDefaults) -> dict:
    legacy = params.get("threshold_sigma")
    long_th = params.get(
        "long_threshold_sigma",
        legacy if legacy is not None else defaults.long_threshold_sigma,
    )
    short_th = params.get(
        "short_threshold_sigma",
        legacy if legacy is not None else defaults.short_threshold_sigma,
    )
    coupled = int(params.get("sma_period", defaults.sma_period))
    sma_long = int(
        params.get("sma_period_long", defaults.sma_period_long or coupled)
    )
    sma_short = int(
        params.get("sma_period_short", defaults.sma_period_short or coupled)
    )
    side = params.get("position_side", "flat")
    if side == "long":
        position_side = 1
    elif side == "short":
        position_side = -1
    else:
        position_side = 0

    return {
        "sma_period": coupled,
        "sma_period_long": sma_long,
        "sma_period_short": sma_short,
        "decoupled": sma_long != sma_short
        or defaults.sma_period_long is not None
        or defaults.sma_period_short is not None
        or "sma_period_long" in params
        or "sma_period_short" in params,
        "long_threshold_sigma": float(long_th),
        "short_threshold_sigma": float(short_th),
        "exit_sigma": float(params.get("exit_sigma", defaults.exit_sigma)),
        "max_bars_in_trade": int(
            params.get("max_bars_in_trade", defaults.max_bars_in_trade)
        ),
        "bars_in_trade": int(params.get("bars_in_trade", 0)),
        "in_position": bool(params.get("in_position", False)),
        "position_side": position_side,
        "corporate_action_price_adjustment": float(
            params.get("corporate_action_price_adjustment", 0.0) or 0.0
        ),
        "corporate_action_window_adjusted": bool(
            params.get("corporate_action_window_adjusted", False)
        ),
        "corporate_event_halt": bool(params.get("corporate_event_halt", False)),
        "regime_filter": bool(params.get("regime_filter", False)),
        "regime_mask_active": bool(params.get("regime_mask_active", True)),
    }


def _warmup_period(p: dict) -> int:
    if p["decoupled"]:
        return max(p["sma_period_long"], p["sma_period_short"])
    return p["sma_period"]


def evaluate_mean_reversion(
    window: RollingWindow,
    params: dict,
    defaults: MeanReversionDefaults,
    strategy_id: str,
) -> Signal | None:
    """
    Asymmetric z-score entries + time-stop exit when a trade stalls.

    Supports decoupled SMA horizons (separate long/short lookbacks).
    """
    p = _resolved_params(params, defaults)
    latest = window.latest()
    if latest is None:
        return None

    if p["in_position"] and p["bars_in_trade"] >= p["max_bars_in_trade"]:
        return Signal(
            symbol=defaults.symbol,
            action=SignalAction.EXIT,
            price=latest.close,
            timestamp=latest.timestamp,
            strategy_id=strategy_id,
            metadata={"reason": "time_stop", "bars_in_trade": p["bars_in_trade"]},
        )

    warmup = _warmup_period(p)
    if not window.is_ready(warmup):
        return None

    price_adjustment = (
        0.0
        if p["corporate_action_window_adjusted"]
        else p["corporate_action_price_adjustment"]
    )
    eval_close = latest.close + price_adjustment

    if p["decoupled"]:
        closes_long = window.tail_closes(p["sma_period_long"])
        closes_short = window.tail_closes(p["sma_period_short"])
        sma_l = rolling_sma(closes_long, p["sma_period_long"])
        std_l = rolling_std(closes_long, p["sma_period_long"])
        sma_s = rolling_sma(closes_short, p["sma_period_short"])
        std_s = rolling_std(closes_short, p["sma_period_short"])
        if std_l <= 0.0 or std_s <= 0.0:
            return None
        z_long = (eval_close - sma_l) / std_l
        z_short = (eval_close - sma_s) / std_s
        action_code = mean_reversion_action_decoupled(
            z_long,
            z_short,
            p["long_threshold_sigma"],
            p["short_threshold_sigma"],
            p["exit_sigma"],
            p["position_side"],
        )
        z_meta = z_long if p["position_side"] >= 0 else z_short
        sma_meta = sma_l if p["position_side"] >= 0 else sma_s
    else:
        closes = window.tail_closes(p["sma_period"])
        sma = rolling_sma(closes, p["sma_period"])
        std = rolling_std(closes, p["sma_period"])
        action_code = mean_reversion_action_asymmetric(
            eval_close,
            sma,
            std,
            p["long_threshold_sigma"],
            p["short_threshold_sigma"],
            p["exit_sigma"],
        )
        z_meta = (eval_close - sma) / std if std > 0 else 0.0
        sma_meta = sma

    if action_code == 0:
        return None

    if (
        p["regime_filter"]
        and action_code == LONG
        and not p["regime_mask_active"]
    ):
        return None

    if p["corporate_event_halt"] and action_code in (LONG, SHORT):
        return None

    mapping = {
        LONG: SignalAction.LONG,
        SHORT: SignalAction.SHORT,
        EXIT: SignalAction.EXIT,
    }
    if action_code not in mapping:
        return None

    return Signal(
        symbol=defaults.symbol,
        action=mapping[action_code],
        price=latest.close,
        timestamp=latest.timestamp,
        strategy_id=strategy_id,
        metadata={
            "z_score": z_meta,
            "sma": sma_meta,
            "corporate_action_price_adjustment": price_adjustment,
            "regime_mask_active": p["regime_mask_active"],
        },
    )
