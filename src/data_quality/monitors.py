"""Pre-signal data-quality monitors (garage G1.4).

Three non-blocking, per-instrument checks that run BEFORE signal evaluation in the
live loop. They never raise into the hot path; they update per-instrument state and
emit structured reports. All are OFF by default (config `data_quality.enabled`)
until the paper soak arms them.

  (a) staleness       -- last-bar age vs a timeframe-specific threshold, market-
                         calendar aware (a closed market is NOT stale) -> block new
                         entries on the affected instrument.
  (b) bar sanity      -- NaN/zero/negative OHLC, high<low, non-monotonic timestamps
                         -> quarantine the bar.
  (c) corporate-action guard -- an overnight per-instrument jump beyond a registered
                         threshold (default 25%) WITHOUT a comparable market-wide
                         move -> quarantine the instrument (block entries, flag
                         positions for the operator) until cleared.

Precedent for (c): the USO 1:8 reverse split (Apr 2020) moved USO's price ~8x
overnight with the broad market flat -- a per-instrument dislocation that must NOT
be traded as a real return. The market-move comparison is what separates a true
split/corporate action from a genuine market-wide gap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from src.core.market_calendar import MarketSessionCalendar
from src.engine.bar_freshness import bar_staleness_seconds
from src.models import Bar


class DataQualityVerdict(str, Enum):
    ALLOW = "ALLOW"
    BLOCK_NEW_ENTRIES = "BLOCK_NEW_ENTRIES"   # staleness: instrument-scoped entry halt
    QUARANTINE_BAR = "QUARANTINE_BAR"         # bar sanity: drop this bar, keep trading
    QUARANTINE_INSTRUMENT = "QUARANTINE_INSTRUMENT"  # corp action: halt + flag positions


@dataclass
class DataQualityConfig:
    enabled: bool = False
    staleness_bar_age_multiple: float = 2.5
    corporate_action_jump_threshold: float = 0.25
    corporate_action_market_move_ratio: float = 0.5

    @classmethod
    def from_mapping(cls, payload: dict | None) -> "DataQualityConfig":
        p = payload or {}
        return cls(
            enabled=bool(p.get("enabled", False)),
            staleness_bar_age_multiple=float(p.get("staleness_bar_age_multiple", 2.5)),
            corporate_action_jump_threshold=float(p.get("corporate_action_jump_threshold", 0.25)),
            corporate_action_market_move_ratio=float(p.get("corporate_action_market_move_ratio", 0.5)),
        )


@dataclass
class InstrumentQualityState:
    symbol: str
    entries_blocked: bool = False
    quarantined: bool = False
    reason: str = ""


@dataclass
class DataQualityResult:
    verdict: DataQualityVerdict
    symbol: str
    reason: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verdict == DataQualityVerdict.ALLOW


def _bad_number(x: float) -> bool:
    return x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))


# --------------------------------------------------------------------------- #
# Individual checks (pure)
# --------------------------------------------------------------------------- #

def check_bar_sanity(bar: Bar, *, prev_timestamp: datetime | None = None) -> DataQualityResult:
    """NaN/zero/negative OHLC, high<low, or a non-monotonic timestamp -> quarantine
    the bar (drop it; do not feed it to the signal path)."""
    sym = bar.symbol
    for name in ("open", "high", "low", "close"):
        v = getattr(bar, name)
        if _bad_number(v) or v <= 0.0:
            return DataQualityResult(DataQualityVerdict.QUARANTINE_BAR, sym,
                                     f"bad_{name}", {name: v})
    if _bad_number(bar.volume) or bar.volume < 0.0:
        return DataQualityResult(DataQualityVerdict.QUARANTINE_BAR, sym, "bad_volume",
                                 {"volume": bar.volume})
    if bar.high < bar.low:
        return DataQualityResult(DataQualityVerdict.QUARANTINE_BAR, sym, "high_lt_low",
                                 {"high": bar.high, "low": bar.low})
    if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
        return DataQualityResult(DataQualityVerdict.QUARANTINE_BAR, sym, "ohlc_inconsistent",
                                 {"open": bar.open, "high": bar.high,
                                  "low": bar.low, "close": bar.close})
    if prev_timestamp is not None and bar.timestamp <= prev_timestamp:
        return DataQualityResult(DataQualityVerdict.QUARANTINE_BAR, sym, "non_monotonic_timestamp",
                                 {"prev": prev_timestamp.isoformat(),
                                  "bar": bar.timestamp.isoformat()})
    return DataQualityResult(DataQualityVerdict.ALLOW, sym)


def check_staleness(
    symbol: str,
    last_bar_ts: datetime,
    now: datetime,
    *,
    timeframe_minutes: float,
    config: DataQualityConfig,
    calendar: MarketSessionCalendar,
    asset_class: str = "stock",
) -> DataQualityResult:
    """Block new entries when the last bar is older than
    ``staleness_bar_age_multiple x timeframe`` -- but MARKET-CALENDAR AWARE: while
    the equity market is closed (weekend/holiday/after-hours) staleness is measured
    from the last session close, so a normal overnight/weekend gap is never flagged.
    Crypto trades 24/7 so wall-clock age is used directly."""
    threshold_min = config.staleness_bar_age_multiple * max(timeframe_minutes, 1e-9)
    if asset_class == "crypto":
        reference = now
    else:
        # if the market is open now, compare to wall clock; if closed, compare to
        # the last session close (so a closed market cannot be "stale").
        reference = now if calendar.is_within_rth(now) else calendar.last_session_close(now)
    # G3: staleness = seconds since the bar CLOSED (shared measurement), not since it opened.
    age_min = bar_staleness_seconds(last_bar_ts, max(timeframe_minutes, 1e-9) * 60.0, reference) / 60.0
    if age_min > threshold_min:
        return DataQualityResult(DataQualityVerdict.BLOCK_NEW_ENTRIES, symbol, "stale_feed",
                                 {"age_min": age_min, "threshold_min": threshold_min})
    return DataQualityResult(DataQualityVerdict.ALLOW, symbol)


def check_corporate_action(
    symbol: str,
    prev_close: float,
    today_open: float,
    *,
    market_prev_close: float | None,
    market_open: float | None,
    config: DataQualityConfig,
) -> DataQualityResult:
    """Quarantine an instrument on an overnight jump beyond the registered threshold
    (default 25%) that is NOT matched by a comparable market-wide move -- the USO
    1:8 reverse-split signature (instrument moves ~8x, market flat)."""
    if prev_close <= 0.0 or today_open <= 0.0:
        return DataQualityResult(DataQualityVerdict.ALLOW, symbol)
    jump = abs(today_open / prev_close - 1.0)
    if jump < config.corporate_action_jump_threshold:
        return DataQualityResult(DataQualityVerdict.ALLOW, symbol)
    market_move = 0.0
    if market_prev_close and market_open and market_prev_close > 0.0:
        market_move = abs(market_open / market_prev_close - 1.0)
    # a comparable market-wide move exonerates the jump (genuine market gap)
    if market_move >= config.corporate_action_jump_threshold * config.corporate_action_market_move_ratio:
        return DataQualityResult(DataQualityVerdict.ALLOW, symbol, "market_wide_gap",
                                 {"jump": jump, "market_move": market_move})
    return DataQualityResult(DataQualityVerdict.QUARANTINE_INSTRUMENT, symbol,
                             "suspected_corporate_action",
                             {"jump": jump, "market_move": market_move})


# --------------------------------------------------------------------------- #
# Coordinator (per-instrument state; awaited pre-signal in the live loop)
# --------------------------------------------------------------------------- #

class DataQualityMonitors:
    """Runs the three checks pre-signal, maintaining per-instrument state. OFF by
    default (config.enabled); when disabled every evaluation returns ALLOW so the
    live loop is untouched until the soak arms it."""

    def __init__(
        self,
        config: DataQualityConfig | None = None,
        *,
        calendar: MarketSessionCalendar | None = None,
        report_sink=None,
    ) -> None:
        self.config = config or DataQualityConfig()
        self._calendar = calendar or MarketSessionCalendar()
        self._report_sink = report_sink or (lambda _r: None)
        self._state: dict[str, InstrumentQualityState] = {}

    def state(self, symbol: str) -> InstrumentQualityState:
        return self._state.setdefault(symbol, InstrumentQualityState(symbol=symbol))

    def entries_blocked(self, symbol: str) -> bool:
        st = self._state.get(symbol)
        return bool(st and (st.entries_blocked or st.quarantined))

    def clear(self, symbol: str) -> None:
        """Operator-driven clear of a quarantine/entry-block once resolved."""
        self._state.pop(symbol, None)

    def _report(self, result: DataQualityResult) -> None:
        self._report_sink({"kind": "data_quality", "verdict": result.verdict.value,
                           "symbol": result.symbol, "reason": result.reason,
                           "detail": result.detail})

    async def evaluate(
        self,
        bar: Bar,
        *,
        now: datetime,
        timeframe_minutes: float,
        prev_timestamp: datetime | None = None,
        prev_close: float | None = None,
        today_open: float | None = None,
        market_prev_close: float | None = None,
        market_open: float | None = None,
        asset_class: str = "stock",
    ) -> DataQualityResult:
        """Awaited pre-signal hook. Non-blocking: returns a verdict and updates
        per-instrument state; the caller consults `entries_blocked` before entering.
        The checks run cheaply and never raise into the hot path."""
        if not self.config.enabled:
            return DataQualityResult(DataQualityVerdict.ALLOW, bar.symbol)

        sym = bar.symbol
        st = self.state(sym)

        sanity = check_bar_sanity(bar, prev_timestamp=prev_timestamp)
        if sanity.verdict != DataQualityVerdict.ALLOW:
            self._report(sanity)
            return sanity   # quarantine this bar; leave prior entry state intact

        if prev_close is not None and today_open is not None:
            corp = check_corporate_action(sym, prev_close, today_open,
                                          market_prev_close=market_prev_close,
                                          market_open=market_open, config=self.config)
            if corp.verdict == DataQualityVerdict.QUARANTINE_INSTRUMENT:
                st.quarantined = True
                st.entries_blocked = True
                st.reason = corp.reason
                self._report(corp)
                return corp

        stale = check_staleness(sym, bar.timestamp, now,
                                timeframe_minutes=timeframe_minutes, config=self.config,
                                calendar=self._calendar, asset_class=asset_class)
        if stale.verdict == DataQualityVerdict.BLOCK_NEW_ENTRIES:
            st.entries_blocked = True
            st.reason = stale.reason
            self._report(stale)
            return stale

        # healthy bar: staleness clears the entry block (quarantine needs operator clear)
        if not st.quarantined:
            st.entries_blocked = False
            st.reason = ""
        return DataQualityResult(DataQualityVerdict.ALLOW, sym)
