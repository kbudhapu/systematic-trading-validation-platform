"""
Macro regime intelligence — event calendar overlays, cross-asset stress, and
portfolio-level risk-off mode evaluation.
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import structlog

from src.router.risk_manager import (
    SESSION_MIDDAY_DOLDRUMS,
    SESSION_OPENING_CROSS,
    resolve_trading_session,
)

log = structlog.get_logger()

ET = ZoneInfo("America/New_York")

PORTFOLIO_RISK_NORMAL = "NORMAL"
PORTFOLIO_RISK_ELEVATED = "ELEVATED"
PORTFOLIO_RISK_OFF = "RISK_OFF"

RISK_OFF_GROSS_EXPOSURE_CAP = 0.55
ELEVATED_GROSS_EXPOSURE_CAP = 0.75
NORMAL_GROSS_EXPOSURE_CAP = 1.0

STRESS_RISK_OFF_THRESHOLD = 0.75
STRESS_ELEVATED_THRESHOLD = 0.45
MACRO_CORRELATION_SPIKE = 0.72

# Z2 recalibration (2026-07-15 forward-only). The credit + vix stress components were
# unit-mismatched: credit compared an SPY *price-level* mean against an HYG/LQD ratio, and
# vix "backwardation" compared VIXY vs VIXM *nominal ETP prices* (VIXY trades ~2x VIXM, so
# the comparison was near-constantly true -> ~87% pegged). Both are now scale-free rolling
# z-scores of the relevant RATIO. Defaults mirror config/regime_stress.schema.json and are
# asserted equal to it by tests/test_regime_stress_config_parity.py (no inert config).
REGIME_STRESS_Z_WINDOW = 252          # trailing sessions for the rolling z baseline
REGIME_STRESS_MIN_SESSIONS = 60       # below this a component is INCOMPUTABLE (not 0-stress)
REGIME_STRESS_Z_FULL_SCALE = 3.0      # z at which a component saturates to stress 1.0
REGIME_STRESS_BACKWARDATION_Z = 2.0   # VIXY/VIXM ratio z above which backwardation latches
REGIME_STRESS_WEIGHT_VIX = 0.40
REGIME_STRESS_WEIGHT_CREDIT = 0.35
REGIME_STRESS_WEIGHT_CORR = 0.25

VELOCITY_WINDOW_HOURS = 4
VELOCITY_OUTLIER_SIGMA = 3.0
HYSTERESIS_RECOVERY_SIGMA = 1.5
VELOCITY_MIN_SAMPLES = 6
SHOCK_COOL_OFF_REQUIRED_BARS = 40
SHOCK_COOL_OFF_PENALTY_WINDOW_DAYS = 7
SHOCK_COOL_OFF_DEMOTION_PENALTY_THRESHOLD = 1
SHOCK_COOL_OFF_PENALTY_MULTIPLIER = 2
VIX_DAY_SURGE_SHOCK_PCT = 0.12
CORRELATION_SPIKE_VELOCITY_SHOCK = 0.15

EVENT_FOMC = "FOMC"
EVENT_CPI = "CPI"
EVENT_OPEX = "MONTHLY_OPEX"
EVENT_INDEX_REBALANCE = "INDEX_REBALANCE"
EVENT_EX_DIVIDEND = "EX_DIVIDEND"

CORPORATE_EVENT_HALT = "CORPORATE_EVENT_HALT"

INDEX_ETF_SYMBOLS = frozenset({"SPY", "QQQ"})
QUARTERLY_EX_DIV_MONTHS = (3, 6, 9, 12)

SPY_EX_DIV_MANIFEST: tuple[tuple[str, float], ...] = (
    ("2025-03-21", 1.696),
    ("2025-06-20", 1.761),
    ("2025-09-19", 1.830),
    ("2025-12-19", 1.993),
    ("2026-03-20", 1.75),
    ("2026-06-19", 1.80),
    ("2026-09-18", 1.85),
    ("2026-12-18", 1.90),
)

QQQ_EX_DIV_MANIFEST: tuple[tuple[str, float], ...] = (
    ("2025-03-24", 0.527),
    ("2025-06-23", 0.591),
    ("2025-09-22", 0.670),
    ("2025-12-22", 0.794),
    ("2026-03-23", 0.55),
    ("2026-06-22", 0.60),
    ("2026-09-21", 0.65),
    ("2026-12-21", 0.70),
)

DEFAULT_QUARTERLY_DIVIDEND: dict[str, float] = {
    "SPY": 1.75,
    "QQQ": 0.60,
}

EX_DIV_MANIFEST_BY_SYMBOL: dict[str, tuple[tuple[str, float], ...]] = {
    "SPY": SPY_EX_DIV_MANIFEST,
    "QQQ": QQQ_EX_DIV_MANIFEST,
}

FOMC_DATES_UTC: tuple[datetime, ...] = tuple(
    datetime(y, m, d, 18, 0, tzinfo=timezone.utc)
    for y, m, d in (
        (2025, 1, 29),
        (2025, 3, 19),
        (2025, 5, 7),
        (2025, 6, 18),
        (2025, 7, 30),
        (2025, 9, 17),
        (2025, 11, 6),
        (2025, 12, 17),
        (2026, 1, 28),
        (2026, 3, 18),
        (2026, 4, 29),
        (2026, 6, 17),
        (2026, 7, 29),
        (2026, 9, 16),
        (2026, 11, 5),
        (2026, 12, 16),
    )
)

REBALANCE_MONTHS = frozenset({3, 6, 9, 12})


@dataclass(frozen=True)
class ScheduledEvent:
    event_type: str
    event_time: datetime
    label: str
    severity: float = 1.0


@dataclass(frozen=True)
class ExDividendEvent:
    symbol: str
    ex_date: date
    amount_per_share: float
    source: str


@dataclass(frozen=True)
class CorporateActionAdjustment:
    symbol: str
    active: bool
    ex_dividend_date: date | None
    offset_dollars: float
    apply_price_adjustment: float
    session_type: str
    halt_entries: bool
    directive: str | None
    amount_source: str
    reason: str


@dataclass(frozen=True)
class RiskPostureOverride:
    position_cap_multiplier: float
    entry_z_widen_sigma: float
    event_label: str | None
    active: bool
    hours_to_event: float | None = None


@dataclass(frozen=True)
class CrossAssetSnapshot:
    vix_proxy: float
    vix_term_proxy: float
    hyg_close: float
    lqd_close: float
    tlt_close: float
    spy_closes: tuple[float, ...]
    qqq_closes: tuple[float, ...]
    tlt_closes: tuple[float, ...]
    timestamp: datetime
    vix_closes: tuple[float, ...] = ()
    # Z2-1: full trailing series for the ratio-z stress components. The scalar *_close /
    # *_proxy fields above are retained (telemetry + velocity tracker) and default to the
    # last element of these when a snapshot is built from tuples.
    hyg_closes: tuple[float, ...] = ()
    lqd_closes: tuple[float, ...] = ()
    vixy_closes: tuple[float, ...] = ()
    vixm_closes: tuple[float, ...] = ()


@dataclass(frozen=True)
class StressFactorReading:
    vix_term_stress: float
    credit_spread_stress: float
    macro_correlation_stress: float
    composite: float
    vix_backwardation: bool
    credit_spread_change: float
    avg_pairwise_correlation: float
    # Z2-4: fail-safe visibility. ``stress_component_degraded`` is True when ANY component was
    # INCOMPUTABLE (insufficient history); ``computable_components`` names the ones that fed the
    # (weight-renormalized) composite. Additive with defaults so existing readers are unaffected.
    stress_component_degraded: bool = False
    computable_components: tuple[str, ...] = ()
    # Observability: the UNCLIPPED ratio z-scores that FEED the one-sided clips above. The clipped
    # components floor at 0.0 on the non-stress side, so a bare `*_stress=0.0` is ambiguous between
    # "genuinely calm (z is on the non-stress side, e.g. credit z=+2.57 risk-ON / vix z=-1.20
    # contango)" and "inputs flat/stale". These raw z's make the first case legible from the log
    # line alone. Signed: credit z>0 = HY OUT-performing (risk-on); vix z>0 = VIXY/VIXM backwardation
    # (stress). Additive with defaults; NOT used by any decision (composite/mode unchanged).
    credit_ratio_z: float = 0.0
    vix_ratio_z: float = 0.0


@dataclass(frozen=True)
class MacroVelocityReading:
    """Point-in-time macro structural variables for intraday velocity tracking."""

    timestamp: datetime
    vix_day_pct_surge: float
    vix_term_spread: float
    credit_spread_change: float
    avg_pairwise_correlation: float
    composite_stress: float


@dataclass(frozen=True)
class VelocityShockVerdict:
    shock_detected: bool
    triggered_metrics: tuple[str, ...]
    max_z_score: float
    reason: str
    latest_reading: MacroVelocityReading | None


@dataclass(frozen=True)
class RegimeStabilizationVerdict:
    stabilized: bool
    stable_sub_sigma_bars: int
    required_bars: int
    composite_stress_z_score: float
    seconds_since_last_shock: float | None
    reason: str
    recovery_sigma_threshold: float
    shock_event_count_7d: int
    shock_demotion_count_7d: int


@dataclass
class MacroVelocityTracker:
    """
    Rolling intraday tracker for structural macro variable velocity.

    Samples are retained for ``window_hours`` and compared against the in-window
    distribution to flag outlier standard-deviation extensions.
    """

    window_hours: float = VELOCITY_WINDOW_HOURS
    outlier_sigma: float = VELOCITY_OUTLIER_SIGMA
    hysteresis_recovery_sigma: float = HYSTERESIS_RECOVERY_SIGMA
    min_samples: int = VELOCITY_MIN_SAMPLES
    shock_cool_off_required_bars: int = SHOCK_COOL_OFF_REQUIRED_BARS
    shock_demotion_window_days: int = SHOCK_COOL_OFF_PENALTY_WINDOW_DAYS
    _samples: deque[MacroVelocityReading] = field(default_factory=deque)
    _last_shock_timestamp: datetime | None = None
    _stable_sub_sigma_bars: int = 0
    _shock_event_timestamps: deque[datetime] = field(default_factory=deque)
    _velocity_shock_demotion_timestamps: deque[datetime] = field(default_factory=deque)

    def ingest(
        self,
        snapshot: CrossAssetSnapshot,
        stress: StressFactorReading,
        *,
        anchor_time: datetime | None = None,
    ) -> MacroVelocityReading:
        now = anchor_time or snapshot.timestamp
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        vix_spot = max(snapshot.vix_proxy, 1e-6)
        vix_term = max(snapshot.vix_term_proxy, 1e-6)
        vix_day_pct_surge = 0.0
        if len(getattr(snapshot, "vix_closes", ())) >= 2:
            prior, current = snapshot.vix_closes[-2], snapshot.vix_closes[-1]
            if prior > 1e-6:
                vix_day_pct_surge = (current - prior) / prior
        elif len(snapshot.spy_closes) >= 2:
            # Proxy VIX surge from VIXY spot vs prior daily close embedded in snapshot.
            vix_day_pct_surge = (snapshot.vix_proxy - vix_term) / vix_term

        reading = MacroVelocityReading(
            timestamp=now,
            vix_day_pct_surge=float(vix_day_pct_surge),
            vix_term_spread=float((vix_spot - vix_term) / vix_spot),
            credit_spread_change=float(stress.credit_spread_change),
            avg_pairwise_correlation=float(stress.avg_pairwise_correlation),
            composite_stress=float(stress.composite),
        )
        self._samples.append(reading)
        self._prune(now)
        self._update_cool_off_progress()
        return reading

    def register_shock_event(self, anchor_time: datetime | None = None) -> None:
        now = anchor_time or (
            self._samples[-1].timestamp if self._samples else datetime.now(timezone.utc)
        )
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        self._last_shock_timestamp = now
        self._stable_sub_sigma_bars = 0
        self._record_shock_event(now)

    def register_velocity_shock_demotion(self, anchor_time: datetime | None = None) -> None:
        now = anchor_time or (
            self._last_shock_timestamp
            or (self._samples[-1].timestamp if self._samples else datetime.now(timezone.utc))
        )
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        self._velocity_shock_demotion_timestamps.append(now)
        self._prune_shock_history(now)

    def _record_shock_event(self, anchor_time: datetime) -> None:
        self._shock_event_timestamps.append(anchor_time)
        self._prune_shock_history(anchor_time)

    def _prune_shock_history(self, anchor_time: datetime) -> None:
        cutoff = anchor_time - timedelta(days=max(int(self.shock_demotion_window_days), 1))
        while self._shock_event_timestamps and self._shock_event_timestamps[0] < cutoff:
            self._shock_event_timestamps.popleft()
        while (
            self._velocity_shock_demotion_timestamps
            and self._velocity_shock_demotion_timestamps[0] < cutoff
        ):
            self._velocity_shock_demotion_timestamps.popleft()

    def shock_event_count_7d(self, *, anchor_time: datetime | None = None) -> int:
        now = anchor_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        self._prune_shock_history(now.astimezone(timezone.utc))
        return len(self._shock_event_timestamps)

    def velocity_shock_demotion_count_7d(self, *, anchor_time: datetime | None = None) -> int:
        now = anchor_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        self._prune_shock_history(now.astimezone(timezone.utc))
        return len(self._velocity_shock_demotion_timestamps)

    def effective_cool_off_required_bars(self, *, anchor_time: datetime | None = None) -> int:
        base = max(int(self.shock_cool_off_required_bars), 1)
        if (
            self.velocity_shock_demotion_count_7d(anchor_time=anchor_time)
            > SHOCK_COOL_OFF_DEMOTION_PENALTY_THRESHOLD
        ):
            return base * SHOCK_COOL_OFF_PENALTY_MULTIPLIER
        return base

    @property
    def last_shock_timestamp(self) -> datetime | None:
        return self._last_shock_timestamp

    @property
    def stable_sub_sigma_bars(self) -> int:
        return self._stable_sub_sigma_bars

    def seconds_since_last_shock(self, *, anchor_time: datetime | None = None) -> float | None:
        if self._last_shock_timestamp is None:
            return None
        now = anchor_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return max(0.0, (now.astimezone(timezone.utc) - self._last_shock_timestamp).total_seconds())

    def composite_stress_z_score(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        series = np.asarray(
            [float(sample.composite_stress) for sample in self._samples],
            dtype=np.float64,
        )
        return _zscore_extension(float(series[-1]), series)

    def _update_cool_off_progress(self) -> None:
        if self._last_shock_timestamp is None:
            return
        if self.composite_stress_z_score() < self.hysteresis_recovery_sigma:
            self._stable_sub_sigma_bars += 1
        else:
            self._stable_sub_sigma_bars = 0

    def _stabilization_verdict(
        self,
        *,
        stabilized: bool,
        reason: str,
        anchor_time: datetime | None = None,
    ) -> RegimeStabilizationVerdict:
        z_score = self.composite_stress_z_score()
        required = self.effective_cool_off_required_bars(anchor_time=anchor_time)
        return RegimeStabilizationVerdict(
            stabilized=stabilized,
            stable_sub_sigma_bars=self._stable_sub_sigma_bars,
            required_bars=required,
            composite_stress_z_score=z_score,
            seconds_since_last_shock=self.seconds_since_last_shock(anchor_time=anchor_time),
            reason=reason,
            recovery_sigma_threshold=self.hysteresis_recovery_sigma,
            shock_event_count_7d=self.shock_event_count_7d(anchor_time=anchor_time),
            shock_demotion_count_7d=self.velocity_shock_demotion_count_7d(
                anchor_time=anchor_time
            ),
        )

    def is_regime_stabilized(
        self,
        *,
        anchor_time: datetime | None = None,
    ) -> RegimeStabilizationVerdict:
        if self._last_shock_timestamp is None:
            return self._stabilization_verdict(
                stabilized=False,
                reason="no_recorded_velocity_shock",
                anchor_time=anchor_time,
            )
        required = self.effective_cool_off_required_bars(anchor_time=anchor_time)
        if self._stable_sub_sigma_bars < required:
            return self._stabilization_verdict(
                stabilized=False,
                reason="cool_off_window_incomplete",
                anchor_time=anchor_time,
            )
        z_score = self.composite_stress_z_score()
        if z_score >= self.hysteresis_recovery_sigma:
            return self._stabilization_verdict(
                stabilized=False,
                reason="composite_stress_above_hysteresis_threshold",
                anchor_time=anchor_time,
            )
        return self._stabilization_verdict(
            stabilized=True,
            reason="regime_stabilized",
            anchor_time=anchor_time,
        )

    def detect_velocity_shock_event(
        self,
        *,
        anchor_time: datetime | None = None,
    ) -> VelocityShockVerdict:
        now = anchor_time or (
            self._samples[-1].timestamp if self._samples else datetime.now(timezone.utc)
        )
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        self._prune(now)
        if len(self._samples) < self.min_samples:
            return VelocityShockVerdict(
                shock_detected=False,
                triggered_metrics=(),
                max_z_score=0.0,
                reason="insufficient_velocity_samples",
                latest_reading=self._samples[-1] if self._samples else None,
            )

        latest = self._samples[-1]
        triggered: list[str] = []
        max_z = 0.0

        metric_names = (
            "vix_day_pct_surge",
            "vix_term_spread",
            "credit_spread_change",
            "avg_pairwise_correlation",
            "composite_stress",
        )
        for metric in metric_names:
            series = np.asarray(
                [float(getattr(sample, metric)) for sample in self._samples],
                dtype=np.float64,
            )
            z_level = _zscore_extension(series[-1], series)
            if z_level > max_z:
                max_z = z_level
            if z_level >= self.outlier_sigma:
                triggered.append(f"{metric}:level:{z_level:.2f}σ")

            if series.size >= 3:
                velocity = np.diff(series)
                z_velocity = _zscore_extension(velocity[-1], velocity)
                if z_velocity > max_z:
                    max_z = z_velocity
                if z_velocity >= self.outlier_sigma:
                    triggered.append(f"{metric}:velocity:{z_velocity:.2f}σ")

        if latest.vix_day_pct_surge >= VIX_DAY_SURGE_SHOCK_PCT:
            triggered.append(
                f"vix_day_pct_surge:hard_floor:{latest.vix_day_pct_surge:.3f}"
            )
        if len(self._samples) >= 2:
            corr_delta = (
                latest.avg_pairwise_correlation
                - self._samples[-2].avg_pairwise_correlation
            )
            if corr_delta >= CORRELATION_SPIKE_VELOCITY_SHOCK:
                triggered.append(
                    f"avg_pairwise_correlation:intraday_spike:{corr_delta:.3f}"
                )

        shock = len(triggered) > 0
        reason = "|".join(triggered) if shock else "velocity_within_bounds"
        if shock:
            self.register_shock_event(now)
        return VelocityShockVerdict(
            shock_detected=shock,
            triggered_metrics=tuple(triggered),
            max_z_score=float(max_z),
            reason=reason,
            latest_reading=latest,
        )

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=self.window_hours)
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()


def _zscore_extension(value: float, series: np.ndarray) -> float:
    if series.size < 2:
        return 0.0
    mean = float(np.mean(series))
    std = float(np.std(series))
    if std < 1e-12:
        return abs(value - mean)
    return abs((value - mean) / std)


@dataclass(frozen=True)
class PortfolioRiskModeDecision:
  mode: str
  gross_exposure_cap_multiplier: float
  block_new_entries: bool
  stress: StressFactorReading
  calendar_override: RiskPostureOverride
  reason: str


def _third_friday(year: int, month: int) -> date:
    cursor = date(year, month, 1)
    fridays = 0
    while cursor.month == month:
        if cursor.weekday() == 4:
            fridays += 1
            if fridays == 3:
                return cursor
        cursor += timedelta(days=1)
    raise ValueError(f"no third Friday for {year}-{month}")


def _cpi_release_datetime(year: int, month: int) -> datetime:
    cursor = date(year, month, 1)
    tuesday_count = 0
    while cursor.month == month:
        if cursor.weekday() == 1:
            tuesday_count += 1
            if tuesday_count == 2:
                return datetime.combine(
                    cursor,
                    time(8, 30),
                    tzinfo=ET,
                ).astimezone(timezone.utc)
        cursor += timedelta(days=1)
    return datetime(year, month, 13, 13, 30, tzinfo=timezone.utc)


def _index_rebalance_datetime(year: int, month: int) -> datetime | None:
    if month not in REBALANCE_MONTHS:
        return None
    rebalance_day = _third_friday(year, month)
    return datetime.combine(
        rebalance_day,
        time(16, 0),
        tzinfo=ET,
    ).astimezone(timezone.utc)


def _quarterly_ex_div_date(year: int, month: int) -> date:
    """Approximate ETF ex-dividend date as the third Friday of quarter months."""
    if month not in QUARTERLY_EX_DIV_MONTHS:
        raise ValueError(f"{month} is not a quarterly ex-dividend month")
    return _third_friday(year, month)


def _manifest_amount_map(symbol: str) -> dict[str, float]:
    manifest = EX_DIV_MANIFEST_BY_SYMBOL.get(symbol.upper(), ())
    return {ex_date: amount for ex_date, amount in manifest}


class EventCalendarOverlay:
    """Tracks institutional shock windows, ex-dividend schedules, and posture overrides."""

    def __init__(
        self,
        *,
        fomc_dates: Sequence[datetime] = FOMC_DATES_UTC,
        lookforward_days: int = 45,
        ex_div_lookforward_days: int = 120,
        enforce_halt_on_unknown_offset: bool = True,
    ) -> None:
        self._fomc_dates = tuple(fomc_dates)
        self._lookforward_days = lookforward_days
        self._ex_div_lookforward_days = ex_div_lookforward_days
        self._enforce_halt_on_unknown_offset = enforce_halt_on_unknown_offset
        self._dynamic_dividend_cache: dict[str, dict[str, float]] = {}

    def _normalize_time(self, current_time: datetime) -> datetime:
        if current_time.tzinfo is None:
            return current_time.replace(tzinfo=timezone.utc)
        return current_time

    def refresh_dividend_schedule(
        self,
        symbol: str,
        *,
        api_key: str,
        secret_key: str,
        anchor_time: datetime | None = None,
    ) -> dict[str, float]:
        """Fetch and cache Alpaca cash-dividend corporate actions when credentials exist."""
        sym = symbol.upper()
        if sym not in INDEX_ETF_SYMBOLS:
            return {}
        now = self._normalize_time(anchor_time or datetime.now(timezone.utc))
        start = now - timedelta(days=30)
        end = now + timedelta(days=self._ex_div_lookforward_days)
        from src.backtest.vectorized_mr import fetch_cash_dividends

        events = fetch_cash_dividends(sym, start, end, api_key, secret_key)
        schedule = {ex_date: amount for ex_date, amount in events if amount > 0.0}
        if schedule:
            self._dynamic_dividend_cache[sym] = {
                **self._dynamic_dividend_cache.get(sym, {}),
                **schedule,
            }
        return dict(self._dynamic_dividend_cache.get(sym, {}))

    def _projected_ex_dividend_schedule(
        self,
        symbol: str,
        anchor_time: datetime,
    ) -> dict[str, float]:
        sym = symbol.upper()
        if sym not in INDEX_ETF_SYMBOLS:
            return {}
        et = anchor_time.astimezone(ET)
        projected: dict[str, float] = {}
        fallback_amount = DEFAULT_QUARTERLY_DIVIDEND.get(sym, 0.0)
        manifest = _manifest_amount_map(sym)
        trailing_amounts = [amount for _, amount in EX_DIV_MANIFEST_BY_SYMBOL.get(sym, ())]
        if trailing_amounts:
            fallback_amount = float(np.mean(trailing_amounts[-4:]))

        for month_offset in range(0, 8):
            month = et.month + month_offset
            year = et.year
            while month > 12:
                month -= 12
                year += 1
            if month not in QUARTERLY_EX_DIV_MONTHS:
                continue
            ex_day = _quarterly_ex_div_date(year, month)
            ex_key = ex_day.isoformat()
            if ex_key in manifest:
                projected[ex_key] = manifest[ex_key]
            else:
                projected[ex_key] = fallback_amount
        return projected

    def ex_dividend_schedule(
        self,
        symbol: str,
        current_time: datetime,
    ) -> dict[str, float]:
        sym = symbol.upper()
        if sym not in INDEX_ETF_SYMBOLS:
            return {}
        current_time = self._normalize_time(current_time)
        schedule = dict(_manifest_amount_map(sym))
        schedule.update(self._dynamic_dividend_cache.get(sym, {}))
        schedule.update(self._projected_ex_dividend_schedule(sym, current_time))
        return schedule

    def scheduled_ex_dividend_events(
        self,
        current_time: datetime,
    ) -> list[ExDividendEvent]:
        current_time = self._normalize_time(current_time)
        horizon = current_time + timedelta(days=self._ex_div_lookforward_days)
        events: list[ExDividendEvent] = []
        for symbol in sorted(INDEX_ETF_SYMBOLS):
            schedule = self.ex_dividend_schedule(symbol, current_time)
            for ex_date_str, amount in schedule.items():
                try:
                    ex_day = date.fromisoformat(ex_date_str[:10])
                except ValueError:
                    continue
                ex_ts = datetime.combine(ex_day, time(9, 30), tzinfo=ET).astimezone(
                    timezone.utc
                )
                if current_time - timedelta(days=1) <= ex_ts <= horizon:
                    source = "manifest"
                    if ex_date_str in self._dynamic_dividend_cache.get(symbol, {}):
                        source = "api"
                    elif ex_date_str not in _manifest_amount_map(symbol):
                        source = "projected"
                    events.append(
                        ExDividendEvent(
                            symbol=symbol,
                            ex_date=ex_day,
                            amount_per_share=float(amount),
                            source=source,
                        )
                    )
        events.sort(key=lambda item: (item.ex_date, item.symbol))
        return events

    def _resolve_ex_dividend_amount(
        self,
        symbol: str,
        ex_date: date,
        current_time: datetime,
    ) -> tuple[float, str]:
        sym = symbol.upper()
        ex_key = ex_date.isoformat()
        manifest = _manifest_amount_map(sym)
        if ex_key in manifest:
            return manifest[ex_key], "manifest"
        dynamic = self._dynamic_dividend_cache.get(sym, {})
        if ex_key in dynamic:
            return dynamic[ex_key], "api"
        projected = self._projected_ex_dividend_schedule(sym, current_time)
        if ex_key in projected and projected[ex_key] > 0.0:
            return projected[ex_key], "projected"
        return 0.0, "unknown"

    def is_ex_dividend_date(self, symbol: str, current_time: datetime) -> bool:
        current_time = self._normalize_time(current_time)
        ex_day = current_time.astimezone(ET).date()
        schedule = self.ex_dividend_schedule(symbol, current_time)
        return ex_day.isoformat() in schedule

    def calculate_corporate_action_offset(
        self,
        symbol: str,
        current_time: datetime,
    ) -> float:
        """
        Return the dividend gap dollar amount to neutralize on ex-dividend mornings.

        Non-zero only during the OPENING_CROSS window on a known ex-dividend session date.
        """
        sym = symbol.upper()
        if sym not in INDEX_ETF_SYMBOLS:
            return 0.0
        current_time = self._normalize_time(current_time)
        if resolve_trading_session(current_time) != SESSION_OPENING_CROSS:
            return 0.0
        ex_day = current_time.astimezone(ET).date()
        amount, source = self._resolve_ex_dividend_amount(sym, ex_day, current_time)
        if amount <= 0.0 or source == "unknown":
            return 0.0
        return float(amount)

    def get_corporate_action_adjustment(
        self,
        symbol: str,
        current_time: datetime,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
    ) -> CorporateActionAdjustment:
        sym = symbol.upper()
        inactive = CorporateActionAdjustment(
            symbol=sym,
            active=False,
            ex_dividend_date=None,
            offset_dollars=0.0,
            apply_price_adjustment=0.0,
            session_type=resolve_trading_session(current_time),
            halt_entries=False,
            directive=None,
            amount_source="none",
            reason="no_ex_dividend_event",
        )
        if sym not in INDEX_ETF_SYMBOLS:
            return inactive

        current_time = self._normalize_time(current_time)
        if api_key and secret_key:
            self.refresh_dividend_schedule(
                sym,
                api_key=api_key,
                secret_key=secret_key,
                anchor_time=current_time,
            )

        ex_day = current_time.astimezone(ET).date()
        if not self.is_ex_dividend_date(sym, current_time):
            return inactive

        session_type = resolve_trading_session(current_time)
        amount, source = self._resolve_ex_dividend_amount(sym, ex_day, current_time)
        offset = self.calculate_corporate_action_offset(sym, current_time)

        if offset > 0.0 and session_type == SESSION_OPENING_CROSS:
            return CorporateActionAdjustment(
                symbol=sym,
                active=True,
                ex_dividend_date=ex_day,
                offset_dollars=offset,
                apply_price_adjustment=offset,
                session_type=session_type,
                halt_entries=False,
                directive=None,
                amount_source=source,
                reason="opening_cross_ex_div_price_neutralization",
            )

        if (
            self._enforce_halt_on_unknown_offset
            and session_type == SESSION_OPENING_CROSS
            and (amount <= 0.0 or source == "unknown")
        ):
            return CorporateActionAdjustment(
                symbol=sym,
                active=True,
                ex_dividend_date=ex_day,
                offset_dollars=0.0,
                apply_price_adjustment=0.0,
                session_type=session_type,
                halt_entries=True,
                directive=CORPORATE_EVENT_HALT,
                amount_source=source,
                reason="unknown_ex_div_amount_opening_halt",
            )

        if session_type == SESSION_OPENING_CROSS and offset <= 0.0:
            return CorporateActionAdjustment(
                symbol=sym,
                active=True,
                ex_dividend_date=ex_day,
                offset_dollars=0.0,
                apply_price_adjustment=0.0,
                session_type=session_type,
                halt_entries=True,
                directive=CORPORATE_EVENT_HALT,
                amount_source=source,
                reason="ex_div_opening_halt_until_midday",
            )

        return CorporateActionAdjustment(
            symbol=sym,
            active=True,
            ex_dividend_date=ex_day,
            offset_dollars=max(amount, 0.0),
            apply_price_adjustment=0.0,
            session_type=session_type,
            halt_entries=False,
            directive=None,
            amount_source=source,
            reason="ex_div_post_opening_settled",
        )

    def apply_corporate_action_entry_adjustment(
        self,
        *,
        close: float,
        sma: float,
        std: float,
        price_adjustment: float,
    ) -> tuple[float, float, float]:
        """
        Neutralize ex-dividend opening gap in z-score inputs.

        Adds the dividend offset to the evaluated close so the artificial gap-down
        does not inflate mean-reversion entry z-scores.
        """
        if price_adjustment <= 0.0 or std <= 0.0:
            return close, sma, std
        adjusted_close = close + price_adjustment
        return adjusted_close, sma, std

    def ex_dividend_adjustment_events(
        self,
        symbol: str,
        *,
        range_min: datetime,
        range_max: datetime,
    ) -> tuple[tuple[datetime, float], ...]:
        """
        Return (ex-date market open, dividend amount) pairs that require backward
        OHLC adjustment for bars inside ``[range_min, range_max]``.
        """
        sym = symbol.upper()
        if sym not in INDEX_ETF_SYMBOLS:
            return ()
        range_min = self._normalize_time(range_min).astimezone(timezone.utc)
        range_max = self._normalize_time(range_max).astimezone(timezone.utc)
        schedule = self.ex_dividend_schedule(sym, range_max)
        events: list[tuple[datetime, float]] = []
        for ex_date_str, amount in schedule.items():
            dividend = float(amount)
            if dividend <= 0.0:
                continue
            try:
                ex_day = date.fromisoformat(ex_date_str[:10])
            except ValueError:
                continue
            ex_ts = datetime.combine(ex_day, time(9, 30), tzinfo=ET).astimezone(
                timezone.utc
            )
            if ex_ts <= range_min or ex_ts > range_max:
                continue
            events.append((ex_ts, dividend))
        events.sort(key=lambda item: item[0])
        return tuple(events)

    def refresh_schedule_for_range(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        """Hydrate dividend schedule for a historical research window when API keys exist."""
        if not api_key or not secret_key:
            return
        sym = symbol.upper()
        if sym not in INDEX_ETF_SYMBOLS:
            return
        self.refresh_dividend_schedule(
            sym,
            api_key=api_key,
            secret_key=secret_key,
            anchor_time=self._normalize_time(end),
        )

    def _opex_datetimes(self, anchor: datetime) -> list[datetime]:
        events: list[datetime] = []
        et = anchor.astimezone(ET)
        for month_offset in range(0, 3):
            month = et.month + month_offset
            year = et.year
            while month > 12:
                month -= 12
                year += 1
            opex_day = _third_friday(year, month)
            events.append(
                datetime.combine(opex_day, time(9, 30), tzinfo=ET).astimezone(
                    timezone.utc
                )
            )
        return events

    def scheduled_events(self, current_time: datetime) -> list[ScheduledEvent]:
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        horizon = current_time + timedelta(days=self._lookforward_days)
        events: list[ScheduledEvent] = []

        for fomc in self._fomc_dates:
            if current_time - timedelta(days=2) <= fomc <= horizon:
                events.append(
                    ScheduledEvent(EVENT_FOMC, fomc, f"FOMC_{fomc.date().isoformat()}", 1.0)
                )

        et = current_time.astimezone(ET)
        for month_offset in range(0, 3):
            month = et.month + month_offset
            year = et.year
            while month > 12:
                month -= 12
                year += 1
            cpi_ts = _cpi_release_datetime(year, month)
            if current_time - timedelta(days=1) <= cpi_ts <= horizon:
                events.append(
                    ScheduledEvent(
                        EVENT_CPI,
                        cpi_ts,
                        f"CPI_{year}_{month:02d}",
                        0.85,
                    )
                )
            rebalance_ts = _index_rebalance_datetime(year, month)
            if rebalance_ts is not None and current_time - timedelta(days=1) <= rebalance_ts <= horizon:
                events.append(
                    ScheduledEvent(
                        EVENT_INDEX_REBALANCE,
                        rebalance_ts,
                        f"REBALANCE_{year}_{month:02d}",
                        0.7,
                    )
                )

        for opex_ts in self._opex_datetimes(current_time):
            if current_time - timedelta(days=1) <= opex_ts <= horizon:
                events.append(
                    ScheduledEvent(
                        EVENT_OPEX,
                        opex_ts,
                        f"OPEX_{opex_ts.date().isoformat()}",
                        0.6,
                    )
                )

        events.sort(key=lambda item: item.event_time)
        return events

    def get_risk_posture_override(self, current_time: datetime) -> RiskPostureOverride:
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        events = self.scheduled_events(current_time)
        if not events:
            return RiskPostureOverride(
                position_cap_multiplier=1.0,
                entry_z_widen_sigma=0.0,
                event_label=None,
                active=False,
            )

        nearest: ScheduledEvent | None = None
        nearest_delta = timedelta(days=999)
        for event in events:
            delta = abs(event.event_time - current_time)
            if delta < nearest_delta:
                nearest = event
                nearest_delta = delta

        if nearest is None:
            return RiskPostureOverride(1.0, 0.0, None, False)

        hours = nearest_delta.total_seconds() / 3600.0
        cap_mult = 1.0
        z_widen = 0.0
        if hours <= 1.0:
            cap_mult = 0.25
            z_widen = 0.60
        elif hours <= 4.0:
            cap_mult = 0.35
            z_widen = 0.45
        elif hours <= 24.0:
            cap_mult = 0.50
            z_widen = 0.25
        elif hours <= 72.0:
            cap_mult = 0.70
            z_widen = 0.15

        cap_mult *= max(0.5, min(1.0, nearest.severity))
        active = cap_mult < 1.0 or z_widen > 0.0
        return RiskPostureOverride(
            position_cap_multiplier=cap_mult,
            entry_z_widen_sigma=z_widen,
            event_label=nearest.label,
            active=active,
            hours_to_event=hours,
        )


def _utc_timestamp_ns(ts: datetime) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.astimezone(timezone.utc).timestamp() * 1_000_000_000)


def compute_ex_dividend_back_adjustment_cumulative(
    timestamps: Sequence[datetime],
    events: Sequence[tuple[datetime, float]],
) -> np.ndarray:
    """Sum per-share dividend amounts for each bar preceding an ex-dividend open."""
    if not timestamps or not events:
        return np.zeros(len(timestamps), dtype=np.float64)
    ts_ns = np.asarray([_utc_timestamp_ns(ts) for ts in timestamps], dtype=np.int64)
    cumulative = np.zeros(len(timestamps), dtype=np.float64)
    for ex_ts, amount in events:
        ex_ns = _utc_timestamp_ns(ex_ts)
        cumulative[ts_ns < ex_ns] += float(amount)
    return cumulative


def apply_historical_ex_dividend_gap_neutralization(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    timestamps: Sequence[datetime],
    symbol: str,
    *,
    calendar: EventCalendarOverlay | None = None,
    api_key: str | None = None,
    secret_key: str | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    """
    Continuously back-adjust historical OHLC for ex-dividend gaps.

    For each ex-dividend event, adds the dividend dollar amount to every bar
    that occurred before that event's opening-cross timestamp so rolling
    indicators remain continuous across the full lookback horizon.
    """
    sym = symbol.upper()
    if sym not in INDEX_ETF_SYMBOLS or len(timestamps) == 0:
        return opens, highs, lows, closes, 0

    overlay = calendar or EventCalendarOverlay()
    if timestamps:
        overlay.refresh_schedule_for_range(
            sym,
            timestamps[0],
            timestamps[-1],
            api_key=api_key,
            secret_key=secret_key,
        )

    range_min = min(
        overlay._normalize_time(ts).astimezone(timezone.utc) for ts in timestamps
    )
    range_max = max(
        overlay._normalize_time(ts).astimezone(timezone.utc) for ts in timestamps
    )
    events = overlay.ex_dividend_adjustment_events(
        sym,
        range_min=range_min,
        range_max=range_max,
    )
    if not events:
        return opens, highs, lows, closes, 0

    cumulative = compute_ex_dividend_back_adjustment_cumulative(timestamps, events)
    adjusted_mask = cumulative > 0.0
    if not np.any(adjusted_mask):
        return opens, highs, lows, closes, 0

    return (
        np.asarray(opens, dtype=np.float64) + cumulative,
        np.asarray(highs, dtype=np.float64) + cumulative,
        np.asarray(lows, dtype=np.float64) + cumulative,
        np.asarray(closes, dtype=np.float64) + cumulative,
        int(np.count_nonzero(adjusted_mask)),
    )


class CrossAssetStressDashboard:
    """Computes macro stress from cross-asset market inputs or live proxies."""

    def __init__(
        self,
        *,
        correlation_lookback: int = 20,
        credit_baseline_window: int = 20,
        velocity_tracker: MacroVelocityTracker | None = None,
        z_window: int = REGIME_STRESS_Z_WINDOW,
        min_sessions: int = REGIME_STRESS_MIN_SESSIONS,
        z_full_scale: float = REGIME_STRESS_Z_FULL_SCALE,
        backwardation_z: float = REGIME_STRESS_BACKWARDATION_Z,
        weight_vix: float = REGIME_STRESS_WEIGHT_VIX,
        weight_credit: float = REGIME_STRESS_WEIGHT_CREDIT,
        weight_corr: float = REGIME_STRESS_WEIGHT_CORR,
    ) -> None:
        self.correlation_lookback = correlation_lookback
        self.credit_baseline_window = credit_baseline_window  # retained (telemetry continuity)
        self.velocity_tracker = velocity_tracker or MacroVelocityTracker()
        # Z2 recalibration parameters (config-sourced; see regime_stress block).
        self.z_window = int(z_window)
        self.min_sessions = int(min_sessions)
        self.z_full_scale = max(float(z_full_scale), 1e-6)
        self.backwardation_z = float(backwardation_z)
        self.stress_weights = {
            "vix": float(weight_vix),
            "credit": float(weight_credit),
            "corr": float(weight_corr),
        }
        self._last_velocity_verdict: VelocityShockVerdict | None = None

    @staticmethod
    def _pairwise_avg_correlation(
        series_map: Mapping[str, np.ndarray],
    ) -> tuple[float, int]:
        """Return (mean |correlation|, n_usable_pairs). n_usable_pairs == 0 means the
        correlation component is INCOMPUTABLE (too little/degenerate data)."""
        keys = sorted(series_map)
        if len(keys) < 2:
            return 0.0, 0
        corrs: list[float] = []
        for i, key_a in enumerate(keys):
            for key_b in keys[i + 1 :]:
                a = series_map[key_a]
                b = series_map[key_b]
                n = min(len(a), len(b))
                if n < 5:
                    continue
                ra = np.diff(np.log(a[-n:]))
                rb = np.diff(np.log(b[-n:]))
                if np.std(ra) < 1e-12 or np.std(rb) < 1e-12:
                    continue
                corrs.append(float(np.corrcoef(ra, rb)[0, 1]))
        if not corrs:
            return 0.0, 0
        return float(np.mean(np.abs(corrs))), len(corrs)

    def _rolling_ratio_z(
        self,
        numer: Sequence[float],
        denom: Sequence[float],
    ) -> tuple[float, float, float] | None:
        """Rolling z-score of the LATEST numer/denom ratio vs its trailing window.

        Returns (z, trailing_mean_ratio, ratio_now), or ``None`` when fewer than
        ``min_sessions`` finite ratio observations exist (component INCOMPUTABLE).
        Scale-free by construction -- this is the fix for the Z2 unit-mismatch bugs
        (SPY price-level vs HYG/LQD ratio; nominal VIXY vs VIXM ETP prices)."""
        n = min(len(numer), len(denom))
        if n < self.min_sessions:
            return None
        num = np.asarray(numer[-n:], dtype=np.float64)
        den = np.asarray(denom[-n:], dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = num / np.where(np.abs(den) < 1e-12, np.nan, den)
        ratio = ratio[np.isfinite(ratio)]
        if len(ratio) < self.min_sessions:
            return None
        window = min(self.z_window, len(ratio))
        trailing = ratio[-window:]
        mu = float(np.mean(trailing))
        sd = float(np.std(trailing))
        ratio_now = float(ratio[-1])
        z = 0.0 if sd < 1e-12 else (ratio_now - mu) / sd
        return z, mu, ratio_now

    def evaluate(self, snapshot: CrossAssetSnapshot) -> StressFactorReading:
        computable: list[str] = []
        degraded = False

        # --- VIX term structure: z-score of the VIXY/VIXM RATIO (Z2-3). The prior nominal
        # VIXY>VIXM comparison was ~always true (VIXY trades at a structurally higher price),
        # pegging this component. ---
        vix_term_stress = 0.0
        vix_backwardation = False
        vix_ratio_z = 0.0  # observability only (unclipped); not used by any decision
        vix_z = self._rolling_ratio_z(snapshot.vixy_closes, snapshot.vixm_closes)
        if vix_z is not None:
            z_r, _mu_r, _now_r = vix_z
            vix_ratio_z = float(z_r)
            vix_term_stress = float(np.clip(z_r / self.z_full_scale, 0.0, 1.0))
            vix_backwardation = bool(z_r > self.backwardation_z)
            computable.append("vix")
        else:
            degraded = True

        # --- Credit: z-score of the HYG/LQD RATIO (Z2-2). Negative z = HY underperforming IG
        # = spreads widening = stress. Replaces the SPY price-level baseline entirely. ---
        credit_spread_stress = 0.0
        credit_spread_change = 0.0
        credit_ratio_z = 0.0  # observability only (unclipped); not used by any decision
        credit_z = self._rolling_ratio_z(snapshot.hyg_closes, snapshot.lqd_closes)
        if credit_z is not None:
            z_c, mu_c, ratio_now = credit_z
            credit_ratio_z = float(z_c)
            credit_spread_stress = float(np.clip(-z_c / self.z_full_scale, 0.0, 1.0))
            credit_spread_change = float(mu_c - ratio_now)  # telemetry continuity
            computable.append("credit")
        else:
            degraded = True

        # --- Cross-asset correlation (unchanged form) ---
        lookback = self.correlation_lookback
        spy = np.asarray(snapshot.spy_closes[-lookback:], dtype=np.float64)
        qqq = np.asarray(snapshot.qqq_closes[-lookback:], dtype=np.float64)
        tlt = np.asarray(snapshot.tlt_closes[-lookback:], dtype=np.float64)
        avg_corr, n_pairs = self._pairwise_avg_correlation(
            {"SPY": spy, "QQQ": qqq, "TLT": tlt},
        )
        macro_correlation_stress = 0.0
        if n_pairs > 0:
            macro_correlation_stress = float(
                np.clip(
                    (avg_corr - MACRO_CORRELATION_SPIKE)
                    / max(1.0 - MACRO_CORRELATION_SPIKE, 1e-6),
                    0.0,
                    1.0,
                )
            )
            computable.append("corr")
        else:
            degraded = True

        # --- Composite over COMPUTABLE components with weights renormalized (Z2-4). A missing
        # component contributes nothing AND its weight is removed from the denominator, so an
        # incomputable leg can never masquerade as calm OR pin the composite. ---
        stress_map = {
            "vix": vix_term_stress,
            "credit": credit_spread_stress,
            "corr": macro_correlation_stress,
        }
        active_weights = {k: self.stress_weights[k] for k in computable}
        total_w = sum(active_weights.values())
        if total_w <= 0.0:
            composite = 0.0
            log.critical(
                "stress_all_components_degraded",
                reason="no computable cross-asset stress component (insufficient history)",
                min_sessions=self.min_sessions,
                vixy=len(snapshot.vixy_closes),
                vixm=len(snapshot.vixm_closes),
                hyg=len(snapshot.hyg_closes),
                lqd=len(snapshot.lqd_closes),
            )
        else:
            composite = float(
                np.clip(
                    sum(active_weights[k] / total_w * stress_map[k] for k in computable),
                    0.0,
                    1.0,
                )
            )

        return StressFactorReading(
            vix_term_stress=vix_term_stress,
            credit_spread_stress=credit_spread_stress,
            macro_correlation_stress=macro_correlation_stress,
            composite=composite,
            vix_backwardation=vix_backwardation,
            credit_spread_change=credit_spread_change,
            avg_pairwise_correlation=avg_corr,
            stress_component_degraded=degraded,
            computable_components=tuple(computable),
            credit_ratio_z=credit_ratio_z,
            vix_ratio_z=vix_ratio_z,
        )

    def record_macro_velocity_sample(
        self,
        snapshot: CrossAssetSnapshot,
        stress: StressFactorReading,
        *,
        anchor_time: datetime | None = None,
    ) -> MacroVelocityReading:
        """Ingest a macro structural reading into the rolling velocity tracker."""
        return self.velocity_tracker.ingest(
            snapshot,
            stress,
            anchor_time=anchor_time,
        )

    def detect_velocity_shock_event(
        self,
        *,
        anchor_time: datetime | None = None,
    ) -> VelocityShockVerdict:
        """
        Return True when any cross-asset parameter extends beyond outlier sigma
        within the rolling 4-hour intraday velocity window.
        """
        verdict = self.velocity_tracker.detect_velocity_shock_event(
            anchor_time=anchor_time,
        )
        self._last_velocity_verdict = verdict
        return verdict

    def is_regime_stabilized(self) -> RegimeStabilizationVerdict:
        """True when composite stress has remained below 3σ for the cool-off window."""
        return self.velocity_tracker.is_regime_stabilized()

    def seconds_since_last_velocity_shock(self) -> float | None:
        return self.velocity_tracker.seconds_since_last_shock()

    @property
    def last_velocity_verdict(self) -> VelocityShockVerdict | None:
        return self._last_velocity_verdict

    def fetch_live_snapshot(
        self,
        api_key: str,
        secret_key: str,
        *,
        lookback_bars: int = 270,
    ) -> CrossAssetSnapshot | None:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        from src.ingestor.market_data_feed import soak_market_data_feed

        client = StockHistoricalDataClient(api_key, secret_key)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_bars + 10)
        symbols = ("VIXY", "VIXM", "HYG", "LQD", "TLT", "SPY", "QQQ")
        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame(1, TimeFrameUnit.Day),
            start=start,
            end=end,
            # N1: one soak feed everywhere — regime detection must read the same tape the
            # signals and fills use, never the alpaca-py default (IEX).
            feed=soak_market_data_feed(),
        )
        response = client.get_stock_bars(request)
        closes: dict[str, list[float]] = {sym: [] for sym in symbols}
        timestamps: list[datetime] = []
        for sym in symbols:
            bars = response.data.get(sym, [])
            if not bars:
                return None
            if not timestamps:
                timestamps = [b.timestamp for b in bars]
            closes[sym] = [float(b.close) for b in bars]
        if not closes["SPY"] or not closes["QQQ"] or not closes["TLT"]:
            return None
        return CrossAssetSnapshot(
            vix_proxy=float(closes["VIXY"][-1]),
            vix_term_proxy=float(closes["VIXM"][-1]),
            hyg_close=float(closes["HYG"][-1]),
            lqd_close=float(closes["LQD"][-1]),
            tlt_close=float(closes["TLT"][-1]),
            spy_closes=tuple(closes["SPY"]),
            qqq_closes=tuple(closes["QQQ"]),
            tlt_closes=tuple(closes["TLT"]),
            timestamp=timestamps[-1] if timestamps else end,
            vix_closes=tuple(closes["VIXY"]),
            # Z2-1: retain the full HYG/LQD/VIXY/VIXM series so the ratio-z components have a
            # trailing baseline (>= min_sessions). lookback_bars=270 -> ~252 trading sessions.
            hyg_closes=tuple(closes["HYG"]),
            lqd_closes=tuple(closes["LQD"]),
            vixy_closes=tuple(closes["VIXY"]),
            vixm_closes=tuple(closes["VIXM"]),
        )

    @staticmethod
    def proxy_snapshot(inputs: Mapping[str, Any]) -> CrossAssetSnapshot:
        now = datetime.now(timezone.utc)
        spy = tuple(float(x) for x in inputs.get("spy_closes", (400.0, 401.0, 402.0)))
        qqq = tuple(float(x) for x in inputs.get("qqq_closes", (350.0, 351.0, 352.0)))
        tlt = tuple(float(x) for x in inputs.get("tlt_closes", (90.0, 89.5, 89.0)))
        # Z2-1: the ratio-z components read these series. When a tuple is supplied the matching
        # scalar (*_close / *_proxy) derives from its last element unless explicitly overridden.
        hyg_closes = tuple(float(x) for x in inputs.get("hyg_closes", ()))
        lqd_closes = tuple(float(x) for x in inputs.get("lqd_closes", ()))
        vixy_closes = tuple(float(x) for x in inputs.get("vixy_closes", ()))
        vixm_closes = tuple(float(x) for x in inputs.get("vixm_closes", ()))
        vix_proxy = float(inputs.get("vix_proxy", vixy_closes[-1] if vixy_closes else 18.0))
        vix_term_proxy = float(inputs.get("vix_term_proxy", vixm_closes[-1] if vixm_closes else 20.0))
        vix_closes = tuple(
            float(x) for x in inputs.get("vix_closes", vixy_closes or (vix_term_proxy, vix_proxy))
        )
        return CrossAssetSnapshot(
            vix_proxy=vix_proxy,
            vix_term_proxy=vix_term_proxy,
            hyg_close=float(inputs.get("hyg_close", hyg_closes[-1] if hyg_closes else 76.0)),
            lqd_close=float(inputs.get("lqd_close", lqd_closes[-1] if lqd_closes else 108.0)),
            tlt_close=float(inputs.get("tlt_close", tlt[-1] if tlt else 90.0)),
            spy_closes=spy,
            qqq_closes=qqq,
            tlt_closes=tlt,
            timestamp=inputs.get("timestamp", now),
            vix_closes=vix_closes,
            hyg_closes=hyg_closes,
            lqd_closes=lqd_closes,
            vixy_closes=vixy_closes,
            vixm_closes=vixm_closes,
        )


class PortfolioRiskModeEvaluator:
    """Aggregates calendar and stress inputs into portfolio risk mode."""

    def __init__(
        self,
        calendar: EventCalendarOverlay | None = None,
        stress_dashboard: CrossAssetStressDashboard | None = None,
    ) -> None:
        self.calendar = calendar or EventCalendarOverlay()
        self.stress_dashboard = stress_dashboard or CrossAssetStressDashboard()

    def evaluate(
        self,
        *,
        current_time: datetime,
        stress: StressFactorReading,
        calendar_override: RiskPostureOverride | None = None,
    ) -> PortfolioRiskModeDecision:
        calendar_override = calendar_override or self.calendar.get_risk_posture_override(
            current_time
        )
        reasons: list[str] = []
        mode = PORTFOLIO_RISK_NORMAL
        gross_cap = NORMAL_GROSS_EXPOSURE_CAP
        block_entries = False

        if stress.composite >= STRESS_RISK_OFF_THRESHOLD:
            mode = PORTFOLIO_RISK_OFF
            gross_cap = RISK_OFF_GROSS_EXPOSURE_CAP
            block_entries = True
            reasons.append("stress_composite_risk_off")
        elif stress.composite >= STRESS_ELEVATED_THRESHOLD:
            mode = PORTFOLIO_RISK_ELEVATED
            gross_cap = ELEVATED_GROSS_EXPOSURE_CAP
            reasons.append("stress_composite_elevated")

        if stress.vix_backwardation and stress.vix_term_stress >= 0.6:
            mode = PORTFOLIO_RISK_OFF
            gross_cap = min(gross_cap, RISK_OFF_GROSS_EXPOSURE_CAP)
            block_entries = True
            reasons.append("vix_backwardation")

        if calendar_override.active and calendar_override.position_cap_multiplier <= 0.35:
            mode = PORTFOLIO_RISK_ELEVATED if mode == PORTFOLIO_RISK_NORMAL else mode
            gross_cap = min(gross_cap, calendar_override.position_cap_multiplier)
            reasons.append(f"calendar:{calendar_override.event_label}")

        if (
            calendar_override.active
            and calendar_override.hours_to_event is not None
            and calendar_override.hours_to_event <= 1.0
        ):
            mode = PORTFOLIO_RISK_OFF
            gross_cap = min(gross_cap, RISK_OFF_GROSS_EXPOSURE_CAP)
            block_entries = True
            reasons.append("calendar_imminent_event")

        return PortfolioRiskModeDecision(
            mode=mode,
            gross_exposure_cap_multiplier=gross_cap,
            block_new_entries=block_entries,
            stress=stress,
            calendar_override=calendar_override,
            reason="|".join(reasons) if reasons else "normal",
        )


@dataclass
class RegimeIntelligenceState:
    risk_mode: PortfolioRiskModeDecision | None = None
    last_snapshot: CrossAssetSnapshot | None = None
    last_corporate_adjustment: CorporateActionAdjustment | None = None
    last_velocity_verdict: VelocityShockVerdict | None = None


@dataclass
class DailyRegimeCache:
    allowed_dates: set[str]
    refreshed_at: datetime


class RegimeIntelligenceEngine:
    """Facade wiring calendar, stress dashboard, and portfolio risk mode."""

    def __init__(self) -> None:
        self.calendar = EventCalendarOverlay()
        self.stress_dashboard = CrossAssetStressDashboard()
        self.risk_mode_evaluator = PortfolioRiskModeEvaluator(
            self.calendar,
            self.stress_dashboard,
        )
        self.state = RegimeIntelligenceState()
        self._api_key: str | None = None
        self._secret_key: str | None = None
        self._uptrend_dates_by_symbol: dict[str, DailyRegimeCache] = {}

    def refresh_uptrend_session_dates(
        self,
        symbol: str,
        timestamps: Sequence[datetime],
        closes: Sequence[float],
        *,
        period: int = 200,
    ) -> set[str]:
        """Cache sweep-equivalent uptrend session dates for a symbol."""
        from src.strategies.regime_filter import build_uptrend_session_dates

        allowed = build_uptrend_session_dates(timestamps, closes, period=period)
        self._uptrend_dates_by_symbol[symbol.upper()] = DailyRegimeCache(
            allowed_dates=allowed,
            refreshed_at=datetime.now(timezone.utc),
        )
        return allowed

    def refresh_uptrend_from_intraday(
        self,
        symbol: str,
        timestamps: Sequence[datetime],
        closes: Sequence[float],
        *,
        period: int = 200,
    ) -> set[str]:
        """Build daily regime reference from intraday closes when daily API is unavailable."""
        from src.strategies.regime_filter import daily_closes_from_intraday_bars

        daily_ts, daily_closes = daily_closes_from_intraday_bars(timestamps, closes)
        return self.refresh_uptrend_session_dates(
            symbol,
            daily_ts,
            daily_closes,
            period=period,
        )

    def evaluate_long_entry_regime_mask(
        self,
        symbol: str,
        bar_timestamp: datetime,
        *,
        regime_filter_enabled: bool,
    ) -> bool:
        """
        Return True when long entries are permitted (regime_mask == 1 semantics).

        When ``regime_filter_enabled`` is False, always returns True.
        """
        from src.strategies.regime_filter import is_long_entry_regime_allowed

        cache = self._uptrend_dates_by_symbol.get(symbol.upper())
        allowed_dates = cache.allowed_dates if cache is not None else None
        return is_long_entry_regime_allowed(
            bar_timestamp,
            allowed_dates,
            regime_filter_enabled=regime_filter_enabled,
        )

    def evaluate_live(
        self,
        *,
        current_time: datetime | None = None,
        api_key: str | None = None,
        secret_key: str | None = None,
        proxy_inputs: Mapping[str, Any] | None = None,
    ) -> PortfolioRiskModeDecision:
        self._api_key = api_key
        self._secret_key = secret_key
        now = current_time or datetime.now(timezone.utc)
        snapshot: CrossAssetSnapshot | None = None
        if api_key and secret_key:
            try:
                snapshot = self.stress_dashboard.fetch_live_snapshot(api_key, secret_key)
            except Exception as e:
                log.warning(
                    "regime_snapshot_fetch_failed",
                    error=str(e),
                    fallback="proxy_defaults",
                    timestamp=now.isoformat(),
                )
                snapshot = None
        if snapshot is None:
            snapshot = CrossAssetStressDashboard.proxy_snapshot(proxy_inputs or {})
        stress = self.stress_dashboard.evaluate(snapshot)
        calendar_override = self.calendar.get_risk_posture_override(now)
        self.stress_dashboard.record_macro_velocity_sample(snapshot, stress, anchor_time=now)
        velocity_verdict = self.stress_dashboard.detect_velocity_shock_event(anchor_time=now)
        decision = self.risk_mode_evaluator.evaluate(
            current_time=now,
            stress=stress,
            calendar_override=calendar_override,
        )
        self.state.risk_mode = decision
        self.state.last_snapshot = snapshot
        self.state.last_velocity_verdict = velocity_verdict
        return decision

    def evaluate_velocity_shock(
        self,
        *,
        current_time: datetime | None = None,
        proxy_inputs: Mapping[str, Any] | None = None,
    ) -> VelocityShockVerdict:
        """Evaluate macro velocity shock from the latest cached or proxy snapshot."""
        now = current_time or datetime.now(timezone.utc)
        snapshot = self.state.last_snapshot
        if snapshot is None:
            snapshot = CrossAssetStressDashboard.proxy_snapshot(proxy_inputs or {})
        stress = self.stress_dashboard.evaluate(snapshot)
        self.stress_dashboard.record_macro_velocity_sample(snapshot, stress, anchor_time=now)
        verdict = self.stress_dashboard.detect_velocity_shock_event(anchor_time=now)
        self.state.last_velocity_verdict = verdict
        return verdict

    def apply_routing_overrides(
        self,
        params: dict[str, Any],
        *,
        current_time: datetime | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        decision = self.state.risk_mode
        adjusted = dict(params)
        symbol_key = (symbol or str(params.get("symbol", ""))).upper()
        evaluation_time = current_time or datetime.now(timezone.utc)
        corporate = self.calendar.get_corporate_action_adjustment(
            symbol_key,
            evaluation_time,
            api_key=self._api_key,
            secret_key=self._secret_key,
        )
        self.state.last_corporate_adjustment = corporate
        if corporate.active:
            adjusted["corporate_action_active"] = True
            adjusted["corporate_action_ex_date"] = (
                corporate.ex_dividend_date.isoformat()
                if corporate.ex_dividend_date is not None
                else None
            )
            adjusted["corporate_action_session"] = corporate.session_type
            adjusted["corporate_action_amount_source"] = corporate.amount_source
            if corporate.apply_price_adjustment > 0.0:
                adjusted["corporate_action_price_adjustment"] = (
                    corporate.apply_price_adjustment
                )
            if corporate.halt_entries:
                adjusted["corporate_event_halt"] = True
                adjusted["macro_block_new_entries"] = True
                if corporate.directive:
                    adjusted["corporate_action_directive"] = corporate.directive

        if decision is None:
            regime_filter_enabled = bool(adjusted.get("regime_filter", False))
            adjusted["regime_mask_active"] = self.evaluate_long_entry_regime_mask(
                symbol_key,
                evaluation_time,
                regime_filter_enabled=regime_filter_enabled,
            )
            return adjusted
        calendar = decision.calendar_override
        if calendar.active:
            base_cap = float(adjusted.get("max_position_pct", 0.95) or 0.95)
            adjusted["max_position_pct"] = base_cap * calendar.position_cap_multiplier
            if calendar.entry_z_widen_sigma > 0.0:
                for key in ("long_threshold_sigma", "short_threshold_sigma"):
                    if key in adjusted:
                        adjusted[key] = float(adjusted[key]) + calendar.entry_z_widen_sigma
            adjusted["event_calendar_overlay"] = calendar.event_label
        adjusted["portfolio_risk_mode"] = decision.mode
        adjusted["gross_exposure_cap_multiplier"] = decision.gross_exposure_cap_multiplier
        if decision.block_new_entries:
            adjusted["macro_block_new_entries"] = True

        regime_filter_enabled = bool(adjusted.get("regime_filter", False))
        adjusted["regime_mask_active"] = self.evaluate_long_entry_regime_mask(
            symbol_key,
            evaluation_time,
            regime_filter_enabled=regime_filter_enabled,
        )
        return adjusted

    def to_metadata(self) -> dict[str, Any]:
        decision = self.state.risk_mode
        if decision is None:
            metadata: dict[str, Any] = {}
        else:
            metadata = {
                "portfolio_risk_mode": decision.mode,
                "reason": decision.reason,
                "gross_exposure_cap_multiplier": decision.gross_exposure_cap_multiplier,
                "block_new_entries": decision.block_new_entries,
                "stress_composite": decision.stress.composite,
                "calendar_event": decision.calendar_override.event_label,
            }
        corporate = self.state.last_corporate_adjustment
        if corporate is not None and corporate.active:
            metadata["corporate_action"] = {
                "symbol": corporate.symbol,
                "ex_dividend_date": (
                    corporate.ex_dividend_date.isoformat()
                    if corporate.ex_dividend_date is not None
                    else None
                ),
                "offset_dollars": corporate.offset_dollars,
                "halt_entries": corporate.halt_entries,
                "directive": corporate.directive,
                "session_type": corporate.session_type,
                "amount_source": corporate.amount_source,
                "reason": corporate.reason,
            }
        velocity = self.state.last_velocity_verdict
        if velocity is not None:
            metadata["macro_velocity"] = {
                "shock_detected": velocity.shock_detected,
                "reason": velocity.reason,
                "max_z_score": velocity.max_z_score,
                "triggered_metrics": list(velocity.triggered_metrics),
            }
        return metadata
