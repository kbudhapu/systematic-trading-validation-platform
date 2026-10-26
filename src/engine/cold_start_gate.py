"""
Time-bounded cold-start verification state machine for tape parity gating.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import structlog

from src.engine.bar_freshness import bar_staleness_seconds, stale_after_seconds
from src.ingestor.alpaca import TIMEFRAME_MINUTES

log = structlog.get_logger()

DEFAULT_MAX_COLD_START_WAIT_SECONDS = 120.0
DEFAULT_MAX_BAR_AGE_SECONDS = 65.0
HEURISTIC_ENTRY_SCALE = 0.25
BYPASS_ENTRY_SCALE = 0.15
# Fraction of max_bar_age_for_timeframe at which a staleness WARNING is emitted.
# At 0.5, a 15Min-bar leg warns at ~452s lag; a 4Hour-bar leg warns at ~7202s lag.
# The warning fires ONCE per staleness event (reset on freshness recovery) so it
# is not repeated every cycle during a multi-hour 4Hour-bar inter-bar interval.
STALE_FEED_WARNING_FRACTION = 0.5


class ColdStartPolicy(str, Enum):
    HIGH_AVAILABILITY = "HIGH_AVAILABILITY"
    FAIL_SAFE_CLOSE = "FAIL_SAFE_CLOSE"


DEFAULT_COLD_START_POLICY = ColdStartPolicy.FAIL_SAFE_CLOSE


class ColdStartGateState(str, Enum):
    PENDING = "PENDING"
    PENDING_BLOCKED = "PENDING_BLOCKED"
    HEURISTIC_PARITY = "HEURISTIC_PARITY"
    STABILIZED_VERIFIED = "STABILIZED_VERIFIED"
    STALE_REVERIFY = "STALE_REVERIFY"
    BYPASS_OVERRIDE = "BYPASS_OVERRIDE"


@dataclass(frozen=True)
class ColdStartGateConfig:
    max_cold_start_wait_seconds: float = DEFAULT_MAX_COLD_START_WAIT_SECONDS
    max_bar_age_seconds: float = DEFAULT_MAX_BAR_AGE_SECONDS
    heuristic_entry_scale: float = HEURISTIC_ENTRY_SCALE
    bypass_entry_scale: float = BYPASS_ENTRY_SCALE


@dataclass(frozen=True)
class StreamHealthSnapshot:
    latest_bar_timestamp: datetime | None = None
    prior_bar_timestamp: datetime | None = None
    latest_volume: float = 0.0
    prior_volume: float = 0.0
    bar_count: int = 0


@dataclass(frozen=True)
class ColdStartGateTransition:
    strategy_id: str
    prior_state: ColdStartGateState
    new_state: ColdStartGateState
    reason: str
    wall_clock_lag_seconds: float | None = None
    elapsed_seconds: float | None = None
    matrix_aligned: bool = False
    stream_intact: bool = False


def parse_cold_start_policy(raw: Any) -> ColdStartPolicy:
    if isinstance(raw, ColdStartPolicy):
        return raw
    text = str(raw or "").strip().upper()
    if text == ColdStartPolicy.HIGH_AVAILABILITY.value:
        return ColdStartPolicy.HIGH_AVAILABILITY
    return ColdStartPolicy.FAIL_SAFE_CLOSE


def resolve_cold_start_policy(
    strategy_params: dict | None,
    *,
    symbol: str,
) -> ColdStartPolicy:
    params = strategy_params or {}
    if "cold_start_policy" in params:
        return parse_cold_start_policy(params["cold_start_policy"])
    from src.config.research_validation import load_research_validation_index

    record = load_research_validation_index().get(str(symbol).upper())
    if record is not None and "cold_start_policy" in record.constraints:
        return parse_cold_start_policy(record.constraints["cold_start_policy"])
    return DEFAULT_COLD_START_POLICY


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def bar_period_seconds_for(timeframe: str) -> float:
    """The bar interval in seconds (900 for 15Min, 3600 for 1H, ...)."""
    return float(TIMEFRAME_MINUTES.get(timeframe, 15)) * 60.0


def max_bar_age_for_timeframe(timeframe: str, config: ColdStartGateConfig) -> float:
    """T1 — the staleness-since-close threshold, PERIOD-RELATIVE: one bar period + the post-close
    delivery latency (config.max_bar_age_seconds, default 65s). `max_bar_age_seconds` is a DELIVERY
    property (how late a bar may arrive after its close), independent of the bar period; the period is
    added structurally by stale_after_seconds. The old flat 65s was period-blind: a 1Hour leg's latest
    CLOSED bar is naturally up to 3600s old within its own hour, so a bare 65s threshold flagged it
    stale for ~59 of every 60 minutes (the same absolute-vs-period bug as the SLO bar_freshness and
    the soak heartbeat). A bar is stale only when the NEXT bar is OVERDUE."""
    return stale_after_seconds(bar_period_seconds_for(timeframe), config.max_bar_age_seconds)


def stream_is_structurally_intact(snapshot: StreamHealthSnapshot) -> bool:
    if snapshot.bar_count < 2:
        return False
    if snapshot.latest_bar_timestamp is None or snapshot.prior_bar_timestamp is None:
        return False
    latest = _coerce_utc(snapshot.latest_bar_timestamp)
    prior = _coerce_utc(snapshot.prior_bar_timestamp)
    if latest <= prior:
        return False
    if snapshot.latest_volume <= 0.0 and snapshot.prior_volume <= 0.0:
        return False
    return True


def wall_clock_freshness_ok(
    latest_bar_timestamp: datetime | None,
    *,
    bar_period_seconds: float,
    now: datetime | None = None,
    max_age_seconds: float,
) -> tuple[bool, float | None]:
    """Freshness = staleness-since-CLOSE (bar_freshness.bar_staleness_seconds) vs the post-close
    delivery-latency threshold. G2/G3: the ONE shared measurement -- age from close, not open."""
    if latest_bar_timestamp is None:
        return False, None
    reference = _coerce_utc(now or datetime.now(timezone.utc))
    lag = bar_staleness_seconds(latest_bar_timestamp, bar_period_seconds, reference)
    return lag <= max(max_age_seconds, 0.0), lag


class ColdStartGateEngine:
    """Per-leg cold-start verification with policy-driven fail-safe transitions."""

    def __init__(self, config: ColdStartGateConfig | None = None) -> None:
        self._config = config or ColdStartGateConfig()
        self._state_by_strategy: dict[str, ColdStartGateState] = {}
        self._policy_by_strategy: dict[str, ColdStartPolicy] = {}
        self._symbol_by_strategy: dict[str, str] = {}
        self._started_at_monotonic: float | None = None
        # R4 (INCIDENT-20260722, option i): the time-bound progression clock PAUSES while
        # pre_open_readiness is failed -- it must not accumulate toward HEURISTIC_PARITY /
        # BYPASS_OVERRIDE against a gate that has not passed. Elapsed = wall - started - paused.
        self._paused_accumulated_seconds: float = 0.0
        self._paused_since_monotonic: float | None = None
        self._transition_log: dict[str, ColdStartGateTransition] = {}
        self._policy_suppression_logged: set[tuple[str, str]] = set()
        # Tracks which strategy_ids have already had a stale-feed early warning
        # emitted for the current staleness event.  Reset when freshness recovers.
        self._stale_feed_warned: set[str] = set()

    def begin_cold_start(
        self,
        strategy_ids: list[str],
        *,
        policies: dict[str, ColdStartPolicy | str] | None = None,
        symbols: dict[str, str] | None = None,
    ) -> None:
        self._started_at_monotonic = time.monotonic()
        self._transition_log.clear()
        self._policy_suppression_logged.clear()
        self._stale_feed_warned.clear()
        policy_map = policies or {}
        symbol_map = symbols or {}
        for strategy_id in strategy_ids:
            self._state_by_strategy[strategy_id] = ColdStartGateState.PENDING
            self._policy_by_strategy[strategy_id] = parse_cold_start_policy(
                policy_map.get(strategy_id, DEFAULT_COLD_START_POLICY)
            )
            if strategy_id in symbol_map:
                self._symbol_by_strategy[strategy_id] = symbol_map[strategy_id].upper()
        log.info(
            "cold_start_gate_initialized",
            leg_count=len(strategy_ids),
            max_wait_seconds=self._config.max_cold_start_wait_seconds,
            policies={
                strategy_id: self._policy_by_strategy[strategy_id].value
                for strategy_id in strategy_ids
            },
        )

    def set_policy(self, strategy_id: str, policy: ColdStartPolicy | str) -> None:
        self._policy_by_strategy[strategy_id] = parse_cold_start_policy(policy)

    def set_symbol(self, strategy_id: str, symbol: str) -> None:
        self._symbol_by_strategy[strategy_id] = symbol.upper()

    def policy(self, strategy_id: str) -> ColdStartPolicy:
        return self._policy_by_strategy.get(strategy_id, DEFAULT_COLD_START_POLICY)

    def state(self, strategy_id: str) -> ColdStartGateState:
        return self._state_by_strategy.get(strategy_id, ColdStartGateState.PENDING)

    def elapsed_seconds(self) -> float:
        if self._started_at_monotonic is None:
            return 0.0
        paused = self._paused_accumulated_seconds
        if self._paused_since_monotonic is not None:      # currently paused: add the open span
            paused += max(time.monotonic() - self._paused_since_monotonic, 0.0)
        return max(time.monotonic() - self._started_at_monotonic - paused, 0.0)

    def pause_time_bound_progression(self) -> None:
        """R4: pause the time-bound clock (readiness failed). Idempotent."""
        if self._paused_since_monotonic is None:
            self._paused_since_monotonic = time.monotonic()

    def resume_time_bound_progression(self) -> None:
        """R4: resume the time-bound clock from its paused value (readiness passed). Idempotent."""
        if self._paused_since_monotonic is not None:
            self._paused_accumulated_seconds += max(
                time.monotonic() - self._paused_since_monotonic, 0.0)
            self._paused_since_monotonic = None

    def set_readiness(self, readiness_ok: bool) -> None:
        """R4: drive the pause/resume from the pre_open_readiness gate state."""
        if readiness_ok:
            self.resume_time_bound_progression()
        else:
            self.pause_time_bound_progression()

    def operational_gate_cleared(self) -> bool:
        if not self._state_by_strategy:
            return False
        blocked_states = {
            ColdStartGateState.PENDING,
            ColdStartGateState.PENDING_BLOCKED,
        }
        return all(
            state not in blocked_states for state in self._state_by_strategy.values()
        )

    def fully_verified(self) -> bool:
        if not self._state_by_strategy:
            return False
        return all(
            state == ColdStartGateState.STABILIZED_VERIFIED
            for state in self._state_by_strategy.values()
        )

    def blocks_all_entries(self, strategy_id: str) -> bool:
        return self.state(strategy_id) in {
            ColdStartGateState.PENDING,
            ColdStartGateState.PENDING_BLOCKED,
            ColdStartGateState.STALE_REVERIFY,
        }

    def blocks_entry_signals(self, strategy_id: str, *, symbol: str | None = None) -> bool:
        state = self.state(strategy_id)
        if state in {
            ColdStartGateState.PENDING,
            ColdStartGateState.PENDING_BLOCKED,
            ColdStartGateState.STALE_REVERIFY,
        }:
            return True
        if state in {
            ColdStartGateState.HEURISTIC_PARITY,
            ColdStartGateState.BYPASS_OVERRIDE,
        }:
            if self.policy(strategy_id) == ColdStartPolicy.FAIL_SAFE_CLOSE:
                self._emit_entry_suppressed_by_policy(strategy_id, state, symbol=symbol)
                return True
            return False
        return state != ColdStartGateState.STABILIZED_VERIFIED

    def cold_start_entry_scale(self, strategy_id: str, *, symbol: str | None = None) -> float:
        state = self.state(strategy_id)
        policy = self.policy(strategy_id)
        if state == ColdStartGateState.STABILIZED_VERIFIED:
            return 1.0
        if state == ColdStartGateState.STALE_REVERIFY:
            return 0.0
        if state in {
            ColdStartGateState.HEURISTIC_PARITY,
            ColdStartGateState.BYPASS_OVERRIDE,
        }:
            if policy == ColdStartPolicy.FAIL_SAFE_CLOSE:
                self._emit_entry_suppressed_by_policy(strategy_id, state, symbol=symbol)
                return 0.0
            if state == ColdStartGateState.HEURISTIC_PARITY:
                return float(self._config.heuristic_entry_scale)
            return float(self._config.bypass_entry_scale)
        return 0.0

    def entry_sizing_scale(self, strategy_id: str, *, symbol: str | None = None) -> float:
        return self.cold_start_entry_scale(strategy_id, symbol=symbol)

    def allows_conservative_entries(self, strategy_id: str, *, symbol: str | None = None) -> bool:
        return self.cold_start_entry_scale(strategy_id, symbol=symbol) > 0.0

    def last_transition(self, strategy_id: str) -> ColdStartGateTransition | None:
        return self._transition_log.get(strategy_id)

    def evaluate_verification(
        self,
        strategy_id: str,
        *,
        symbol: str,
        timeframe: str,
        matrix_aligned: bool,
        matrix_skip_reason: str | None,
        stream_health: StreamHealthSnapshot,
        background_verified: bool,
        divergent: bool,
    ) -> ColdStartGateTransition | None:
        if divergent:
            return None
        max_age = max_bar_age_for_timeframe(timeframe, self._config)
        fresh, lag = wall_clock_freshness_ok(
            stream_health.latest_bar_timestamp,
            bar_period_seconds=bar_period_seconds_for(timeframe),
            max_age_seconds=max_age,
        )
        intact = stream_is_structurally_intact(stream_health)
        aligned = matrix_aligned and matrix_skip_reason is None
        current = self.state(strategy_id)

        if (
            aligned
            and background_verified
            and fresh
            and intact
            and current != ColdStartGateState.STABILIZED_VERIFIED
        ):
            return self._transition(
                strategy_id,
                ColdStartGateState.STABILIZED_VERIFIED,
                reason="matrix_parity_and_wall_clock_fresh",
                wall_clock_lag_seconds=lag,
                matrix_aligned=True,
                stream_intact=intact,
                symbol=symbol,
                timeframe=timeframe,
            )

        if aligned and not fresh:
            log.warning(
                "cold_start_gate_freshness_rejected",
                strategy_id=strategy_id,
                symbol=symbol,
                timeframe=timeframe,
                wall_clock_lag_seconds=lag,
                max_bar_age_seconds=max_age,
                matrix_aligned=True,
            )
            return None

        self._evaluate_time_bound(
            strategy_id,
            symbol=symbol,
            timeframe=timeframe,
            stream_health=stream_health,
            matrix_aligned=aligned,
            background_verified=background_verified,
            fresh=fresh,
            intact=intact,
            wall_clock_lag_seconds=lag,
        )
        return self.last_transition(strategy_id)

    def tick_time_bound(
        self,
        strategy_id: str,
        *,
        symbol: str,
        timeframe: str,
        stream_health: StreamHealthSnapshot,
        matrix_aligned: bool,
        background_verified: bool,
        readiness_ok: bool = True,
    ) -> ColdStartGateTransition | None:
        # R4 (option i): while pre_open_readiness is FAILED, the time-bound progression clock is
        # SUSPENDED -- it must never advance a leg to HEURISTIC_PARITY / BYPASS_OVERRIDE against a
        # readiness gate that has not passed (precedence: readiness gate > cold-start policy).
        self.set_readiness(readiness_ok)
        if not readiness_ok:
            return None
        max_age = max_bar_age_for_timeframe(timeframe, self._config)
        fresh, lag = wall_clock_freshness_ok(
            stream_health.latest_bar_timestamp,
            bar_period_seconds=bar_period_seconds_for(timeframe),
            max_age_seconds=max_age,
        )
        intact = stream_is_structurally_intact(stream_health)
        return self._evaluate_time_bound(
            strategy_id,
            symbol=symbol,
            timeframe=timeframe,
            stream_health=stream_health,
            matrix_aligned=matrix_aligned,
            background_verified=background_verified,
            fresh=fresh,
            intact=intact,
            wall_clock_lag_seconds=lag,
        )

    def _evaluate_time_bound(
        self,
        strategy_id: str,
        *,
        symbol: str,
        timeframe: str,
        stream_health: StreamHealthSnapshot,
        matrix_aligned: bool,
        background_verified: bool,
        fresh: bool,
        intact: bool,
        wall_clock_lag_seconds: float | None,
    ) -> ColdStartGateTransition | None:
        current = self.state(strategy_id)

        # BYPASS_OVERRIDE is a deliberate operator override — never demote it.
        if current == ColdStartGateState.BYPASS_OVERRIDE:
            return None

        # Early-warning: emit ONCE when lag first crosses 50% of max_bar_age but
        # before the gate demotes.  Resets automatically on freshness recovery.
        self._check_stale_feed_early_warning(
            strategy_id,
            symbol=symbol,
            timeframe=timeframe,
            lag=wall_clock_lag_seconds,
            max_age_seconds=max_bar_age_for_timeframe(timeframe, self._config),
            fresh=fresh,
        )

        # A fully-verified leg must be re-protected if the feed goes stale.
        if current == ColdStartGateState.STABILIZED_VERIFIED:
            if not fresh:
                return self._transition(
                    strategy_id,
                    ColdStartGateState.STALE_REVERIFY,
                    reason="stabilized_verified_feed_stale",
                    wall_clock_lag_seconds=wall_clock_lag_seconds,
                    matrix_aligned=matrix_aligned,
                    stream_intact=intact,
                    symbol=symbol,
                    timeframe=timeframe,
                )
            return None  # still fresh — nothing to do

        # A demoted leg recovers directly to STABILIZED_VERIFIED (not HEURISTIC_PARITY)
        # once the feed is fresh again and the active/shadow matrices are consistent.
        if current == ColdStartGateState.STALE_REVERIFY:
            if fresh and matrix_aligned:
                return self._transition(
                    strategy_id,
                    ColdStartGateState.STABILIZED_VERIFIED,
                    reason="stale_reverify_feed_recovered",
                    wall_clock_lag_seconds=wall_clock_lag_seconds,
                    matrix_aligned=True,
                    stream_intact=intact,
                    symbol=symbol,
                    timeframe=timeframe,
                )
            return None  # still stale — remain blocked
        elapsed = self.elapsed_seconds()
        if elapsed < self._config.max_cold_start_wait_seconds:
            return None

        if matrix_aligned and background_verified and fresh and intact:
            return self._transition(
                strategy_id,
                ColdStartGateState.STABILIZED_VERIFIED,
                reason="time_bound_stabilized_verified",
                wall_clock_lag_seconds=wall_clock_lag_seconds,
                matrix_aligned=True,
                stream_intact=intact,
                symbol=symbol,
                timeframe=timeframe,
            )

        policy = self.policy(strategy_id)
        if policy == ColdStartPolicy.FAIL_SAFE_CLOSE:
            if current == ColdStartGateState.PENDING:
                return self._transition(
                    strategy_id,
                    ColdStartGateState.PENDING_BLOCKED,
                    reason="fail_safe_timeout_without_matrix_parity",
                    wall_clock_lag_seconds=wall_clock_lag_seconds,
                    matrix_aligned=matrix_aligned,
                    stream_intact=intact,
                    symbol=symbol,
                    timeframe=timeframe,
                )
            return None

        if intact and fresh and current == ColdStartGateState.PENDING:
            return self._transition(
                strategy_id,
                ColdStartGateState.HEURISTIC_PARITY,
                reason="time_bound_heuristic_parity",
                wall_clock_lag_seconds=wall_clock_lag_seconds,
                matrix_aligned=matrix_aligned,
                stream_intact=True,
                symbol=symbol,
                timeframe=timeframe,
            )

        if current != ColdStartGateState.BYPASS_OVERRIDE:
            transition = self._transition(
                strategy_id,
                ColdStartGateState.BYPASS_OVERRIDE,
                reason="time_bound_system_bypass",
                wall_clock_lag_seconds=wall_clock_lag_seconds,
                matrix_aligned=matrix_aligned,
                stream_intact=intact,
                symbol=symbol,
                timeframe=timeframe,
            )
            log.critical(
                "cold_start_gate: SYSTEM_BYPASS_ENGAGED",
                cold_start_gate_event="SYSTEM_BYPASS_ENGAGED",
                strategy_id=strategy_id,
                symbol=symbol,
                timeframe=timeframe,
                elapsed_seconds=elapsed,
                wall_clock_lag_seconds=wall_clock_lag_seconds,
                matrix_aligned=matrix_aligned,
                stream_intact=intact,
                fresh=fresh,
            )
            return transition
        return None

    def _check_stale_feed_early_warning(
        self,
        strategy_id: str,
        *,
        symbol: str,
        timeframe: str,
        lag: float | None,
        max_age_seconds: float,
        fresh: bool,
    ) -> None:
        """Emit a one-shot warning when lag first exceeds STALE_FEED_WARNING_FRACTION of
        max_age but before the gate actually blocks.  Resets when freshness is restored."""
        if lag is None:
            return
        warning_threshold = max_age_seconds * STALE_FEED_WARNING_FRACTION
        if fresh and lag > warning_threshold:
            if strategy_id not in self._stale_feed_warned:
                self._stale_feed_warned.add(strategy_id)
                log.warning(
                    "cold_start_gate_stale_feed_early_warning",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    lag_seconds=lag,
                    warning_threshold_seconds=warning_threshold,
                    demotion_threshold_seconds=max_age_seconds,
                )
        elif not fresh or lag <= warning_threshold:
            # Freshness recovered or lag dropped back below threshold — reset so a
            # new staleness event will fire the warning again.
            self._stale_feed_warned.discard(strategy_id)

    def _emit_entry_suppressed_by_policy(
        self,
        strategy_id: str,
        state: ColdStartGateState,
        *,
        symbol: str | None,
    ) -> None:
        resolved_symbol = (symbol or self._symbol_by_strategy.get(strategy_id) or "").upper()
        cache_key = (strategy_id, state.value)
        if cache_key in self._policy_suppression_logged:
            return
        self._policy_suppression_logged.add(cache_key)
        assigned_policy = self.policy(strategy_id).value
        log.warning(
            "cold_start_gate: ENTRY_SUPPRESSED_BY_POLICY",
            symbol=resolved_symbol or None,
            strategy_id=strategy_id,
            active_gate_state=state.value,
            assigned_policy=assigned_policy,
        )

    def _transition(
        self,
        strategy_id: str,
        new_state: ColdStartGateState,
        *,
        reason: str,
        symbol: str,
        timeframe: str,
        wall_clock_lag_seconds: float | None,
        matrix_aligned: bool,
        stream_intact: bool,
    ) -> ColdStartGateTransition:
        prior = self.state(strategy_id)
        if prior == new_state:
            return ColdStartGateTransition(
                strategy_id=strategy_id,
                prior_state=prior,
                new_state=new_state,
                reason=reason,
                wall_clock_lag_seconds=wall_clock_lag_seconds,
                elapsed_seconds=self.elapsed_seconds(),
                matrix_aligned=matrix_aligned,
                stream_intact=stream_intact,
            )
        self._state_by_strategy[strategy_id] = new_state
        transition = ColdStartGateTransition(
            strategy_id=strategy_id,
            prior_state=prior,
            new_state=new_state,
            reason=reason,
            wall_clock_lag_seconds=wall_clock_lag_seconds,
            elapsed_seconds=self.elapsed_seconds(),
            matrix_aligned=matrix_aligned,
            stream_intact=stream_intact,
        )
        self._transition_log[strategy_id] = transition
        log.info(
            "cold_start_gate_state_transition",
            strategy_id=strategy_id,
            symbol=symbol,
            timeframe=timeframe,
            prior_state=prior.value,
            new_state=new_state.value,
            reason=reason,
            elapsed_seconds=transition.elapsed_seconds,
            wall_clock_lag_seconds=wall_clock_lag_seconds,
            matrix_aligned=matrix_aligned,
            stream_intact=stream_intact,
            assigned_policy=self.policy(strategy_id).value,
            entry_sizing_scale=self.cold_start_entry_scale(strategy_id, symbol=symbol),
        )
        return transition

    def telemetry_snapshot(self) -> dict[str, Any]:
        return {
            "cold_start_gate_states": {
                strategy_id: state.value
                for strategy_id, state in self._state_by_strategy.items()
            },
            "cold_start_policies": {
                strategy_id: policy.value
                for strategy_id, policy in self._policy_by_strategy.items()
            },
            "cold_start_elapsed_seconds": self.elapsed_seconds(),
            "cold_start_operational_gate_cleared": self.operational_gate_cleared(),
            "cold_start_fully_verified": self.fully_verified(),
            "max_cold_start_wait_seconds": self._config.max_cold_start_wait_seconds,
        }
