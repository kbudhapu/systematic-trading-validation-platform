"""
Operational SLO monitor — bar freshness, gap detection, and feed health validation.
"""

from __future__ import annotations

import sqlite3
import structlog
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from src.engine.bar_freshness import bar_staleness_seconds, stale_after_seconds
from src.ingestor.alpaca import TIMEFRAME_MINUTES
from src.persistence.db import RESEARCH_VAULT_PATH, RL_BACKFILL_MIN_AGE_HOURS

# E5/R4: the market session calendar moved to its canonical home in src.core. These
# names (ET/RTH constants, holiday sets, SessionBounds, MarketSessionCalendar) are
# re-exported here for backward compatibility -- existing importers of
# `src.engine.slo_monitor` keep working; new code should import from
# `src.core.market_calendar` directly.
from src.core.market_calendar import (  # noqa: F401  (re-export shim)
    ET,
    NYSE_EARLY_CLOSE,
    NYSE_HOLIDAYS,
    RTH_CLOSE_EARLY,
    RTH_CLOSE_REGULAR,
    RTH_OPEN,
    MarketSessionCalendar,
    SessionBounds,
)

# T1: these are POST-CLOSE DELIVERY LATENCIES (how late may a bar arrive after it closes), NOT
# absolute bar ages. The effective stale threshold is period-relative: one bar period + this
# latency (stale_after_seconds). 150s/600s vs a 900s bar OR a 3600s bar behaves identically now.
BAR_DELIVERY_LATENCY_SOFT_SECONDS = 150.0
BAR_DELIVERY_LATENCY_HARD_SECONDS = 600.0
MISSING_BAR_RATE_SOFT = 0.06
MISSING_BAR_RATE_HARD = 0.18
NBBO_SUCCESS_SOFT = 0.85
NBBO_SUCCESS_HARD = 0.55
RL_BACKFILL_LAG_SOFT_HOURS = 8.0
RL_BACKFILL_LAG_HARD_HOURS = 30.0
INGEST_ARRIVAL_LAG_SOFT_SECONDS = 15.0
INGEST_ARRIVAL_LAG_HARD_SECONDS = 30.0
# Cadence-relative arrival-lag tiers (registry SLO-001): lag as a fraction of the bar
# period, calibrated to the 15Min baseline (15s/900s soft, 30s/900s hard) so a benign
# longer-cadence poll lag never trips HARD while genuine over-cadence lag still does.
INGEST_ARRIVAL_LAG_SOFT_FRACTION = INGEST_ARRIVAL_LAG_SOFT_SECONDS / 900.0
INGEST_ARRIVAL_LAG_HARD_FRACTION = INGEST_ARRIVAL_LAG_HARD_SECONDS / 900.0
INGEST_SEQUENCE_MISALIGNMENT_SOFT = 1
INGEST_SEQUENCE_MISALIGNMENT_HARD = 5
GAP_INTERVAL_MULT = 1.75
NBBO_TRACK_WINDOW = 40
MISSING_BAR_LOOKBACK = 48
MAX_CYCLE_BUDGET_MS = 1000.0
MAX_PENDING_BUNDLES = 5

log = structlog.get_logger()


class IntegritySeverity(str, Enum):
    OK = "OK"
    WARNING = "WARNING"
    SOFT_BREACH = "SOFT_BREACH"
    HARD_BREACH = "HARD_BREACH"


@dataclass(frozen=True)
class DataIntegrityVerdict:
    passed: bool
    severity: IntegritySeverity
    bar_freshness_seconds: float
    missing_bar_rate: float
    nbbo_success_rate: float
    rl_backfill_lag_hours: float
    calendar_session_minutes: float
    is_early_close_session: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class CycleLatencyVerdict:
    total_cycle_ms: float
    phase_a_ms: float
    phase_b_ms: float
    phase_c_ms: float
    max_cycle_budget_ms: float
    violated: bool


@dataclass(frozen=True)
class BackpressureVerdict:
    pending_bundles: int
    max_pending_bundles: int
    blocks_new_entries: bool


@dataclass
class SLOMonitor:
    """Validates live data feeds against operational SLO agreements."""

    db_path: Path = RESEARCH_VAULT_PATH
    calendar: MarketSessionCalendar = field(default_factory=MarketSessionCalendar)
    max_cycle_budget_ms: float = MAX_CYCLE_BUDGET_MS
    max_pending_bundles: int = MAX_PENDING_BUNDLES
    # U1a: PER-SYMBOL nbbo history. One shared deque made every leg's verdict a function of every
    # OTHER leg's data (BTC HARD-breached on equity NBBO misses while its own nbbo never failed once).
    _nbbo_attempts_by_symbol: dict[str, deque[int]] = field(default_factory=dict)

    def note_nbbo_fetch(
        self,
        success: bool,
        *,
        symbol: str,
        asset_class: str = "stock",
        reference_ts: datetime | None = None,
    ) -> None:
        # U1b: a `None` quote from a CLOSED market is NOT a failure -- the market is closed, there is
        # no quote, nothing failed. Record NO sample (not success, not failure). Same session
        # awareness as T2e (MarketSessionCalendar). Crypto trades 24/7 and always records.
        if asset_class != "crypto":
            ref = _coerce_ts(reference_ts) if reference_ts is not None else datetime.now(timezone.utc)
            if not self.calendar.is_within_rth(ref):
                return
        self._nbbo_attempts_by_symbol.setdefault(
            symbol.upper(), deque(maxlen=NBBO_TRACK_WINDOW)
        ).append(1 if success else 0)

    def nbbo_success_rate(self, symbol: str) -> float:
        dq = self._nbbo_attempts_by_symbol.get(symbol.upper())
        if not dq:
            return 1.0
        return float(np.mean(dq))

    def evaluate_cycle_latency(
        self,
        *,
        phase_a_ms: float,
        phase_b_ms: float,
        phase_c_ms: float,
        total_cycle_ms: float,
    ) -> CycleLatencyVerdict:
        violated = float(total_cycle_ms) > float(self.max_cycle_budget_ms)
        verdict = CycleLatencyVerdict(
            total_cycle_ms=float(total_cycle_ms),
            phase_a_ms=float(phase_a_ms),
            phase_b_ms=float(phase_b_ms),
            phase_c_ms=float(phase_c_ms),
            max_cycle_budget_ms=float(self.max_cycle_budget_ms),
            violated=violated,
        )
        if verdict.violated:
            log.warning(
                "slo_verdict: LATENCY_VIOLATION",
                total_cycle_ms=verdict.total_cycle_ms,
                phase_a_ms=verdict.phase_a_ms,
                phase_b_ms=verdict.phase_b_ms,
                phase_c_ms=verdict.phase_c_ms,
                max_cycle_budget_ms=verdict.max_cycle_budget_ms,
            )
        return verdict

    def evaluate_fill_sieve_backpressure(
        self,
        pending_bundles: int,
    ) -> BackpressureVerdict:
        verdict = BackpressureVerdict(
            pending_bundles=int(pending_bundles),
            max_pending_bundles=int(self.max_pending_bundles),
            blocks_new_entries=int(pending_bundles) > int(self.max_pending_bundles),
        )
        if verdict.blocks_new_entries:
            log.warning(
                "backpressure_alert: SIEVE_QUEUE_HIGH",
                pending_bundles=verdict.pending_bundles,
                max_pending_bundles=verdict.max_pending_bundles,
            )
        return verdict

    def is_market_dormant(self, asset_class: str, exchange_reference: datetime) -> bool:
        """T2e/W2 — THE single dormancy predicate. A non-crypto venue outside its RTH session is
        DORMANT (no expected bars, quotes, or arriving ticks), not FAILED. Shared by
        evaluate_data_integrity AND the orchestrator's feed-quality path so that NO health/quality
        path can sample a closed market — T4 clause (4), made unrepresentable rather than patched
        four times (bar_freshness/T2e, nbbo/U1b, feed_stream_health/W2). Crypto is never dormant."""
        return (
            str(asset_class or "stock") != "crypto"
            and not self.calendar.is_within_rth(exchange_reference)
        )

    def evaluate_data_integrity(self, data_payload: Mapping[str, Any]) -> DataIntegrityVerdict:
        exchange_reference = _coerce_ts(data_payload["exchange_reference_ts"])
        asset_class = str(data_payload.get("asset_class") or "stock")

        # T2e — SESSION AWARENESS. A leg whose venue is CLOSED has NO EXPECTED BARS: it is DORMANT,
        # not STALE. A closed equity market produces no fresh bars, no quotes, no arriving ticks;
        # treating that as a data-integrity FAILURE would (via the shared degradation manager)
        # force-flatten the 24/7 crypto legs every night and weekend — ~118 hours a week — on
        # evidence that says nothing about crypto. Equities are DORMANT outside their session; crypto
        # trades 24/7 and is never dormant. (This is the same session-awareness gap as N3/G1.)
        if self.is_market_dormant(asset_class, exchange_reference):
            return DataIntegrityVerdict(
                passed=True, severity=IntegritySeverity.OK,
                bar_freshness_seconds=0.0, missing_bar_rate=0.0, nbbo_success_rate=1.0,
                rl_backfill_lag_hours=0.0, calendar_session_minutes=0.0,
                is_early_close_session=False, reasons=("dormant_market_closed",),
            )
        bar_minutes = int(
            data_payload.get("bar_minutes")
            or TIMEFRAME_MINUTES.get(str(data_payload.get("timeframe") or "15Min"), 15)
        )
        latest_bar_ts = _coerce_ts(data_payload.get("latest_bar_timestamp"))
        reference_ts = self.calendar.effective_reference_time(
            exchange_reference,
            asset_class=asset_class,
        )

        bar_timestamps = _coerce_bar_timestamps(data_payload.get("bar_timestamps"))
        missing_bar_rate = compute_missing_bar_rate(
            bar_timestamps,
            bar_minutes=bar_minutes,
            calendar=self.calendar,
            lookback_bars=int(data_payload.get("lookback_bars") or MISSING_BAR_LOOKBACK),
            asset_class=asset_class,
        )

        freshness_seconds = bar_staleness_seconds(latest_bar_ts, bar_minutes * 60.0, reference_ts)
        freshness_seconds = _calendar_adjusted_freshness_limit(
            freshness_seconds,
            bar_minutes=bar_minutes,
            calendar=self.calendar,
            exchange_reference=exchange_reference,
            asset_class=asset_class,
        )

        symbol = str(data_payload.get("symbol") or "")
        if data_payload.get("nbbo_fetch_success") is not None:
            self.note_nbbo_fetch(
                bool(data_payload.get("nbbo_fetch_success")),
                symbol=symbol, asset_class=asset_class, reference_ts=exchange_reference,
            )
        nbbo_success_rate = float(
            data_payload.get("nbbo_success_rate") or self.nbbo_success_rate(symbol)
        )

        rl_backfill_lag_hours = float(
            data_payload.get("rl_backfill_lag_hours")
            if data_payload.get("rl_backfill_lag_hours") is not None
            else query_rl_backfill_lag_hours(
                self.db_path,
                reference_ts=exchange_reference,
            )
        )

        et_reference = exchange_reference.astimezone(ET)
        bounds = self.calendar.session_bounds(et_reference.date())

        reasons: list[str] = []
        severity = IntegritySeverity.OK

        # T1: STALE only when the NEXT bar is overdue -> one bar period + the delivery latency.
        bar_period_secs = bar_minutes * 60.0
        if freshness_seconds > stale_after_seconds(bar_period_secs, BAR_DELIVERY_LATENCY_SOFT_SECONDS):
            reasons.append("bar_freshness_drift")
            severity = _max_severity(severity, IntegritySeverity.SOFT_BREACH)
        if freshness_seconds > stale_after_seconds(bar_period_secs, BAR_DELIVERY_LATENCY_HARD_SECONDS):
            reasons.append("bar_freshness_critical")
            severity = IntegritySeverity.HARD_BREACH

        if missing_bar_rate > MISSING_BAR_RATE_SOFT:
            reasons.append("missing_bar_rate_elevated")
            severity = _max_severity(severity, IntegritySeverity.SOFT_BREACH)
        if missing_bar_rate > MISSING_BAR_RATE_HARD:
            reasons.append("missing_bar_rate_critical")
            severity = IntegritySeverity.HARD_BREACH

        if nbbo_success_rate < NBBO_SUCCESS_SOFT:
            reasons.append("nbbo_fetch_degraded")
            severity = _max_severity(severity, IntegritySeverity.SOFT_BREACH)
        if nbbo_success_rate < NBBO_SUCCESS_HARD:
            reasons.append("nbbo_fetch_critical")
            severity = IntegritySeverity.HARD_BREACH

        # S2: rl_backfill_lag is a RESEARCH-PIPELINE metric (the shadow RL policy's reward backfill
        # in research_vault.db). The shadow policy does not trade. A stale reward backfill says
        # NOTHING about whether it is safe to place an order — not the price, not the broker, not the
        # feed — so it MUST NOT appear in the live-trading SLO verdict at all. (Capping it at SOFT
        # was a half-measure that kept a category error in the live path at a lower dose; via the
        # HARD-recovery reset it still trapped the engine in six days of force-flatten.)
        # `rl_backfill_lag_hours` is still measured and carried on the verdict for reporting, and is
        # surfaced on its own RESEARCH-pipeline health channel (evaluate_research_pipeline_health),
        # where a genuine 6.5-day-dead backfill ALERTS — but it never gates the order path.

        if bool(data_payload.get("ingest_circuit_breaker_tripped")):
            # SLO-001: a LONE circuit trip is SOFT (recoverable), not an unconditional HARD.
            # HARD is reserved for genuine over-cadence lag (below) or co-occurring critical
            # signals (bar_freshness / missing_bar_rate / nbbo_fetch).
            reasons.append("ingest_circuit_breaker_tripped")
            severity = _max_severity(severity, IntegritySeverity.SOFT_BREACH)

        sequence_misalignments = int(
            data_payload.get("sequence_misalignment_events") or 0
        )
        if sequence_misalignments >= INGEST_SEQUENCE_MISALIGNMENT_HARD:
            reasons.append("sequence_misalignment_critical")
            severity = IntegritySeverity.HARD_BREACH
        elif sequence_misalignments >= INGEST_SEQUENCE_MISALIGNMENT_SOFT:
            reasons.append("sequence_misalignment")
            severity = _max_severity(severity, IntegritySeverity.SOFT_BREACH)

        ingest_arrival_lag = float(
            data_payload.get("ingest_arrival_lag_seconds") or 0.0
        )
        # SLO-001: judge arrival lag as a fraction of the bar period (cadence-relative), so a
        # benign longer-cadence poll lag lands at OK/SOFT and never HARD, while genuine
        # over-cadence lag still escalates to HARD.
        bar_period_seconds = TIMEFRAME_MINUTES.get(
            str(data_payload.get("timeframe") or "15Min"), 15
        ) * 60.0
        arrival_lag_fraction = (
            ingest_arrival_lag / bar_period_seconds if bar_period_seconds > 0 else 0.0
        )
        if arrival_lag_fraction > INGEST_ARRIVAL_LAG_HARD_FRACTION:
            reasons.append("ingest_arrival_lag_critical")
            severity = IntegritySeverity.HARD_BREACH
        elif arrival_lag_fraction > INGEST_ARRIVAL_LAG_SOFT_FRACTION:
            reasons.append("ingest_arrival_lag")
            severity = _max_severity(severity, IntegritySeverity.SOFT_BREACH)

        passed = severity in (IntegritySeverity.OK, IntegritySeverity.WARNING)
        return DataIntegrityVerdict(
            passed=passed,
            severity=severity,
            bar_freshness_seconds=freshness_seconds,
            missing_bar_rate=missing_bar_rate,
            nbbo_success_rate=nbbo_success_rate,
            rl_backfill_lag_hours=rl_backfill_lag_hours,
            calendar_session_minutes=bounds.session_minutes,
            is_early_close_session=bounds.early_close,
            reasons=tuple(reasons),
        )

    def evaluate_research_pipeline_health(
        self, *, reference_ts: datetime, rl_backfill_lag_hours: float | None = None
    ) -> dict[str, Any]:
        """S2b — research/ML pipeline health, MONITORED + ALERTED but it NEVER gates the order path.
        Currently the shadow RL reward backfill lag. Returns {healthy, alert, lag_hours, reason};
        the caller alerts on `alert` (a genuine multi-day-dead backfill is a real finding) without
        ever touching the degradation manager or the live SLO verdict."""
        lag = float(
            rl_backfill_lag_hours
            if rl_backfill_lag_hours is not None
            else query_rl_backfill_lag_hours(self.db_path, reference_ts=reference_ts)
        )
        # alert at the (former) HARD threshold — a >30h dead backfill is worth a page, off the
        # order path entirely.
        alert = lag > RL_BACKFILL_LAG_HARD_HOURS
        healthy = lag <= RL_BACKFILL_LAG_SOFT_HOURS
        return {
            "healthy": healthy,
            "alert": alert,
            "rl_backfill_lag_hours": lag,
            "reason": "rl_backfill_lag" if not healthy else "",
        }


def compute_missing_bar_rate(
    bar_timestamps: Sequence[datetime],
    *,
    bar_minutes: int,
    calendar: MarketSessionCalendar,
    lookback_bars: int = MISSING_BAR_LOOKBACK,
    asset_class: str = "stock",
) -> float:
    if bar_minutes <= 0 or not bar_timestamps:
        return 0.0

    ordered = sorted(_coerce_bar_timestamps(bar_timestamps))[-lookback_bars:]
    if len(ordered) < 2:
        return 0.0

    if asset_class == "crypto":
        expected_interval = timedelta(minutes=bar_minutes)
        max_gap = expected_interval * GAP_INTERVAL_MULT
        gaps = 0
        expected = len(ordered)
        for prev, curr in zip(ordered, ordered[1:]):
            delta = curr - prev
            if delta > max_gap:
                gaps += int(delta / expected_interval) - 1
        return gaps / max(expected + gaps, 1)

    expected_interval = timedelta(minutes=bar_minutes)
    max_gap = expected_interval * GAP_INTERVAL_MULT
    gaps = 0
    expected = len(ordered)

    for prev, curr in zip(ordered, ordered[1:]):
        if not _both_in_same_rth_span(prev, curr, calendar):
            continue
        delta = curr - prev
        if delta > max_gap:
            gaps += int(delta / expected_interval) - 1

    session_date = ordered[-1].astimezone(ET).date()
    session_expected = calendar.expected_bars_for_session(session_date, bar_minutes)
    if session_expected > 0 and calendar.is_early_close(session_date):
        observed_today = sum(
            1
            for ts in ordered
            if ts.astimezone(ET).date() == session_date and calendar.is_within_rth(ts)
        )
        shortfall = max(0, session_expected - observed_today)
        gaps = max(gaps, shortfall)

    return gaps / max(expected + gaps, 1)


def query_rl_backfill_lag_hours(
    db_path: Path = RESEARCH_VAULT_PATH,
    *,
    reference_ts: datetime,
) -> float:
    reference = _coerce_ts(reference_ts)
    max_lag = 0.0
    with sqlite3.connect(db_path) as conn:
        try:
            rows = conn.execute(
                """
                SELECT timestamp, realized_reward_24h
                FROM shadow_rl_ledger
                WHERE realized_reward_24h IS NULL
                ORDER BY timestamp ASC
                LIMIT 200
                """
            ).fetchall()
        except sqlite3.Error:
            return 0.0

    for ts_raw, _ in rows:
        origin = _coerce_ts(ts_raw)
        age_hours = (reference - origin).total_seconds() / 3600.0
        if age_hours < RL_BACKFILL_MIN_AGE_HOURS:
            continue
        max_lag = max(max_lag, age_hours - RL_BACKFILL_MIN_AGE_HOURS)
    return max_lag


def _calendar_adjusted_freshness_limit(
    freshness_seconds: float,
    *,
    bar_minutes: int,
    calendar: MarketSessionCalendar,
    exchange_reference: datetime,
    asset_class: str,
) -> float:
    if asset_class == "crypto":
        return freshness_seconds
    if calendar.is_within_rth(exchange_reference):
        return freshness_seconds
    bounds = calendar.session_bounds(exchange_reference.astimezone(ET).date())
    if bounds.early_close and not bounds.trading_day:
        return freshness_seconds
    return min(freshness_seconds, bar_minutes * 60.0)


def _both_in_same_rth_span(
    left: datetime,
    right: datetime,
    calendar: MarketSessionCalendar,
) -> bool:
    if left.date() != right.date():
        return False
    return calendar.is_within_rth(left) and calendar.is_within_rth(right)


def _coerce_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_bar_timestamps(value: Any) -> list[datetime]:
    if value is None:
        return []
    if isinstance(value, datetime):
        return [value]
    timestamps: list[datetime] = []
    for item in value:
        if isinstance(item, datetime):
            timestamps.append(_coerce_ts(item))
        elif isinstance(item, Mapping) and item.get("timestamp") is not None:
            timestamps.append(_coerce_ts(item["timestamp"]))
        else:
            timestamps.append(_coerce_ts(item))
    return timestamps


def _severity_rank(severity: IntegritySeverity) -> int:
    order = {
        IntegritySeverity.OK: 0,
        IntegritySeverity.WARNING: 1,
        IntegritySeverity.SOFT_BREACH: 2,
        IntegritySeverity.HARD_BREACH: 3,
    }
    return order[severity]


def _max_severity(
    current: IntegritySeverity,
    candidate: IntegritySeverity,
) -> IntegritySeverity:
    return current if _severity_rank(current) >= _severity_rank(candidate) else candidate
