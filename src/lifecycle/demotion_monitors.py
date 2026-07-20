"""Demotion monitors (LLD section 4).

Non-blocking evaluators run off the telemetry stream. Each returns a trip verdict;
the lifecycle state machine maps Tier-1 trips to WATCH and Tier-2 trips to
SAFE_MODE. Parameters default to the doctrine values (registered per leg at
promotion). Arming rules (section 6.2): the rolling-Sharpe and hit-rate monitors
require a minimum armed history before they can fire, and flat periods extend the
window rather than trip it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from scipy import stats


class Tier(str, Enum):
    NONE = "NONE"
    WATCH = "WATCH"       # Tier 1 (early warning)
    SAFE_MODE = "SAFE_MODE"  # Tier 2 (demotion)


@dataclass
class MonitorVerdict:
    name: str
    tier: Tier
    detail: dict

    @property
    def tripped(self) -> bool:
        return self.tier != Tier.NONE


# --------------------------------------------------------------------------- #
# Tier 2 -- CUSUM drift (grinding decay) and drawdown bound (single-event)
# --------------------------------------------------------------------------- #

def cusum_drift(returns, mu0: float, sigma: float, *, k: float = 0.5, h: float = 5.0) -> MonitorVerdict:
    """One-sided downward CUSUM on weekly returns (LLD monitor 4):
    S_t = max(0, S_{t-1} + (mu0 - r_t)/sigma - k); trip when S_t > h.
    Detects sustained downward drift from the validated mean; a single fat-tail
    week is intentionally NOT enough to trip (that is the drawdown bound's job)."""
    if sigma <= 0:
        return MonitorVerdict("cusum", Tier.NONE, {"reason": "invalid_sigma"})
    s = 0.0
    trip_index = None
    s_max = 0.0
    for i, r in enumerate(returns):
        s = max(0.0, s + (mu0 - float(r)) / sigma - k)
        s_max = max(s_max, s)
        if s > h and trip_index is None:
            trip_index = i
    tier = Tier.SAFE_MODE if trip_index is not None else Tier.NONE
    return MonitorVerdict("cusum", tier, {"trip_index": trip_index, "s_max": s_max, "h": h})


def drawdown_bound(equity_curve, oos_max_dd: float, *, multiplier: float = 1.25) -> MonitorVerdict:
    """Trip when live peak-to-trough drawdown exceeds multiplier x the leg's
    decade-OOS MaxDD (LLD monitor 5). Immediate, no confirmation window."""
    peak = -math.inf
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)
    threshold = multiplier * abs(oos_max_dd)
    tier = Tier.SAFE_MODE if max_dd > threshold else Tier.NONE
    return MonitorVerdict("drawdown_bound", tier,
                          {"live_max_dd": max_dd, "threshold": threshold})


# --------------------------------------------------------------------------- #
# Tier 1 -- rolling Sharpe, hit rate, cost divergence (with arming)
# --------------------------------------------------------------------------- #

def _rolling_sharpe(returns, window: int, periods_per_year: float = 52.0):
    out = []
    for end in range(window, len(returns) + 1):
        w = returns[end - window:end]
        mean = sum(w) / window
        var = sum((x - mean) ** 2 for x in w) / (window - 1) if window > 1 else 0.0
        sd = var ** 0.5
        out.append((mean / sd * math.sqrt(periods_per_year)) if sd > 0 else 0.0)
    return out


def rolling_sharpe_monitor(
    weekly_returns, oos_ci_lower: float, *, window: int = 26, breach_consecutive: int = 4,
    min_armed_weeks: int = 12,
) -> MonitorVerdict:
    """WATCH when the rolling Sharpe sits below the OOS bootstrap CI lower bound for
    `breach_consecutive` consecutive weeks. Unarmed (too little history) -> silent."""
    armed_weeks = len(weekly_returns)
    if armed_weeks < max(min_armed_weeks, window):
        return MonitorVerdict("rolling_sharpe", Tier.NONE, {"armed": False, "weeks": armed_weeks})
    series = _rolling_sharpe(list(weekly_returns), window)
    run = 0
    for s in series:
        run = run + 1 if s < oos_ci_lower else 0
        if run >= breach_consecutive:
            return MonitorVerdict("rolling_sharpe", Tier.WATCH,
                                  {"armed": True, "consecutive": run})
    return MonitorVerdict("rolling_sharpe", Tier.NONE, {"armed": True, "consecutive": run})


def hit_rate_monitor(
    trade_wins, oos_hit_rate: float, *, n: int = 40, min_armed_trades: int = 40,
) -> MonitorVerdict:
    """WATCH when the rolling n-trade hit rate falls below the binomial 95% lower
    bound of the OOS hit rate. Unarmed (< min_armed_trades) -> silent."""
    wins = list(trade_wins)
    if len(wins) < max(min_armed_trades, n):
        return MonitorVerdict("hit_rate", Tier.NONE, {"armed": False, "trades": len(wins)})
    # binomial 95% lower bound on the OOS hit rate over n trades
    lower = stats.binom.ppf(0.05, n, oos_hit_rate) / n
    window = wins[-n:]
    rate = sum(1 for w in window if w) / n
    tier = Tier.WATCH if rate < lower else Tier.NONE
    return MonitorVerdict("hit_rate", tier, {"armed": True, "rate": rate, "lower_bound": lower})


def cost_divergence_monitor(
    realized_costs, modeled_costs, *, multiplier: float = 1.5, consecutive_days: int = 10,
) -> MonitorVerdict:
    """WATCH when realized round-trip cost exceeds multiplier x modeled for
    `consecutive_days` consecutive trading days (LLD monitor 2)."""
    run = 0
    for realized, modeled in zip(realized_costs, modeled_costs):
        if modeled > 0 and realized > multiplier * modeled:
            run += 1
            if run >= consecutive_days:
                return MonitorVerdict("cost_divergence", Tier.WATCH, {"consecutive": run})
        else:
            run = 0
    return MonitorVerdict("cost_divergence", Tier.NONE, {"consecutive": run})


def integrity_trip(*, hash_ok: bool, parity_ok: bool, reconciliation_ok: bool) -> MonitorVerdict:
    """Instant SAFE_MODE on any integrity violation (LLD monitor 7) -- these are
    not performance trips."""
    if hash_ok and parity_ok and reconciliation_ok:
        return MonitorVerdict("integrity", Tier.NONE, {})
    return MonitorVerdict("integrity", Tier.SAFE_MODE,
                          {"hash_ok": hash_ok, "parity_ok": parity_ok,
                           "reconciliation_ok": reconciliation_ok})


def watch_persistence_trip(watch_weeks: int, *, max_weeks: int = 8) -> MonitorVerdict:
    """A Tier-1 condition unresolved past its registered window escalates to
    SAFE_MODE (LLD monitor 6)."""
    tier = Tier.SAFE_MODE if watch_weeks >= max_weeks else Tier.NONE
    return MonitorVerdict("watch_persistence", tier,
                          {"watch_weeks": watch_weeks, "max_weeks": max_weeks})


@dataclass
class OOSStats:
    """Registered out-of-sample statistics for a leg (from VTD Stage 2)."""
    weekly_mean: float
    weekly_std: float
    sharpe_ci_lower: float
    max_drawdown: float
    hit_rate: float
    modeled_cost: float


def worst_tier(verdicts: list[MonitorVerdict]) -> Tier:
    """The most severe tier across a set of monitor verdicts."""
    if any(v.tier == Tier.SAFE_MODE for v in verdicts):
        return Tier.SAFE_MODE
    if any(v.tier == Tier.WATCH for v in verdicts):
        return Tier.WATCH
    return Tier.NONE
