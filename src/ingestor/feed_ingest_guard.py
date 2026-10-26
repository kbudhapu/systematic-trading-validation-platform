"""
Feed sequence validation and ingest arrival-latency circuit breaker.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from src.engine.bar_freshness import bar_staleness_seconds
from src.ingestor.alpaca import TIMEFRAME_MINUTES

# Arrival-lag breach is CADENCE-RELATIVE (registry SLO-001): lag is judged as a fraction
# of the bar period, calibrated to the equity-15Min baseline (15s / 900s = 1.667%), so a
# benign longer-cadence poll lag (e.g. a 1Hour bar ~60s late = 1.667%) is not treated like
# a streamed-feed lag. INGEST_ARRIVAL_LAG_CEILING_SECONDS is retained as the reference point.
INGEST_ARRIVAL_LAG_CEILING_SECONDS = 15.0
INGEST_ARRIVAL_LAG_CEILING_FRACTION = 15.0 / 900.0
INGEST_CIRCUIT_BREAKER_STREAK = 3

# W1 — the real-time WS delivers 1-MINUTE bars stamped at their OPEN, regardless of the leg's
# aggregation timeframe (a 1Hour leg is aggregated downstream from these minute bars). Arrival lag —
# SLO-001's "delay between a bar's SCHEDULED availability and its ACTUAL arrival" — must be measured
# against the WS bar's OWN close (open + this cadence), NOT the leg period. Measuring against the leg
# period (or from the open) made the benign 60s WS cadence read as a full period of "lag" — a 60s
# sawtooth that breached the 60s ceiling every tick and tripped the circuit breaker forever.
STREAM_BAR_PERIOD_SECONDS = 60.0


def bar_period_seconds(timeframe: str) -> float:
    """Nominal bar period in seconds for a timeframe (15Min->900, 1Hour->3600, ...)."""
    return float(TIMEFRAME_MINUTES.get(timeframe, 15)) * 60.0


@dataclass(frozen=True)
class BarAcceptance:
    accepted: bool
    reason: str


@dataclass(frozen=True)
class IngestGuardSnapshot:
    sequence_misalignment_events: int
    consecutive_lag_breaches: int
    ingest_circuit_breaker_tripped: bool
    last_arrival_lag_seconds: float


class FeedSequenceGuard:
    """Per-symbol intraday sequence and duplicate bar rejection."""

    __slots__ = ("_last_seq", "_last_ts", "_lock", "_misalignment_events")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ts: dict[tuple[str, str], datetime] = {}
        self._last_seq: dict[tuple[str, str], int] = {}
        # per-(symbol, timeframe) misalignment counter — avoids cross-symbol bleed
        self._misalignment_events: dict[tuple[str, str], int] = {}

    def evaluate(
        self,
        symbol: str,
        timeframe: str,
        bar_timestamp: datetime,
        *,
        vendor_sequence_id: int | None = None,
    ) -> BarAcceptance:
        key = (symbol.upper(), timeframe)
        normalized_ts = bar_timestamp.astimezone(timezone.utc)
        sequence_id = (
            int(vendor_sequence_id)
            if vendor_sequence_id is not None
            else int(normalized_ts.timestamp() * 1000.0)
        )
        with self._lock:
            prior_ts = self._last_ts.get(key)
            if prior_ts is not None and normalized_ts <= prior_ts:
                self._misalignment_events[key] = self._misalignment_events.get(key, 0) + 1
                return BarAcceptance(False, "out_of_order_or_duplicate_timestamp")
            prior_seq = self._last_seq.get(key)
            if prior_seq is not None and sequence_id <= prior_seq:
                self._misalignment_events[key] = self._misalignment_events.get(key, 0) + 1
                return BarAcceptance(False, "sequence_misalignment")
            self._last_ts[key] = normalized_ts
            self._last_seq[key] = sequence_id
            return BarAcceptance(True, "accepted")

    def misalignment_count(
        self,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> int:
        """Return misalignment count for a specific symbol/timeframe, or total across all."""
        with self._lock:
            if symbol is not None and timeframe is not None:
                key = (symbol.upper(), timeframe)
                return self._misalignment_events.get(key, 0)
            return sum(self._misalignment_events.values())


class IngestCircuitBreaker:
    """Trips when vendor bar arrival latency breaches ceiling repeatedly.

    State is tracked per (symbol, timeframe) to prevent a slow symbol's late
    bars from tripping the circuit for unrelated symbols.
    """

    __slots__ = (
        "_consecutive_breaches",
        "_last_lag_seconds",
        "_lock",
        "_tripped",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive_breaches: dict[tuple[str, str], int] = {}
        self._last_lag_seconds: dict[tuple[str, str], float] = {}
        self._tripped: dict[tuple[str, str], bool] = {}

    def note_bar_arrival(
        self,
        bar_timestamp: datetime,
        symbol: str,
        timeframe: str,
    ) -> None:
        arrival = datetime.now(timezone.utc)
        # W1: arrival lag = how long AFTER the WS bar's close it arrived (SLO-001's delivery
        # property), via the ONE shared T1 helper. The WS bar closes at open + STREAM_BAR_PERIOD,
        # NOT open + leg_period; measuring from the open counted the whole 1-min WS cadence as lag.
        lag = bar_staleness_seconds(bar_timestamp, STREAM_BAR_PERIOD_SECONDS, arrival)
        key = (symbol.upper(), timeframe)
        with self._lock:
            self._last_lag_seconds[key] = lag
            ceiling = INGEST_ARRIVAL_LAG_CEILING_FRACTION * bar_period_seconds(timeframe)
            if lag > ceiling:
                self._consecutive_breaches[key] = self._consecutive_breaches.get(key, 0) + 1
            else:
                self._consecutive_breaches[key] = 0
            if self._consecutive_breaches.get(key, 0) >= INGEST_CIRCUIT_BREAKER_STREAK:
                self._tripped[key] = True

    def is_tripped(
        self,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> bool:
        """True if the named symbol/timeframe is tripped, or any symbol if both None."""
        with self._lock:
            if symbol is not None and timeframe is not None:
                return self._tripped.get((symbol.upper(), timeframe), False)
            return any(self._tripped.values())

    def snapshot(
        self,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> IngestGuardSnapshot:
        with self._lock:
            if symbol is not None and timeframe is not None:
                key = (symbol.upper(), timeframe)
                return IngestGuardSnapshot(
                    sequence_misalignment_events=0,
                    consecutive_lag_breaches=self._consecutive_breaches.get(key, 0),
                    ingest_circuit_breaker_tripped=self._tripped.get(key, False),
                    last_arrival_lag_seconds=self._last_lag_seconds.get(key, 0.0),
                )
            # aggregate: worst-case consecutive breaches and tripped across all symbols
            return IngestGuardSnapshot(
                sequence_misalignment_events=0,
                consecutive_lag_breaches=max(self._consecutive_breaches.values(), default=0),
                ingest_circuit_breaker_tripped=any(self._tripped.values()),
                last_arrival_lag_seconds=max(self._last_lag_seconds.values(), default=0.0),
            )


_sequence_guard: FeedSequenceGuard | None = None
_circuit_breaker: IngestCircuitBreaker | None = None
_guard_lock = threading.Lock()


def get_feed_sequence_guard() -> FeedSequenceGuard:
    global _sequence_guard
    with _guard_lock:
        if _sequence_guard is None:
            _sequence_guard = FeedSequenceGuard()
        return _sequence_guard


def get_ingest_circuit_breaker() -> IngestCircuitBreaker:
    global _circuit_breaker
    with _guard_lock:
        if _circuit_breaker is None:
            _circuit_breaker = IngestCircuitBreaker()
        return _circuit_breaker


def ingest_guard_snapshot(
    symbol: str | None = None,
    timeframe: str | None = None,
) -> IngestGuardSnapshot:
    guard = get_feed_sequence_guard()
    breaker = get_ingest_circuit_breaker()
    base = breaker.snapshot(symbol, timeframe)
    return IngestGuardSnapshot(
        sequence_misalignment_events=guard.misalignment_count(symbol, timeframe),
        consecutive_lag_breaches=base.consecutive_lag_breaches,
        ingest_circuit_breaker_tripped=base.ingest_circuit_breaker_tripped,
        last_arrival_lag_seconds=base.last_arrival_lag_seconds,
    )
