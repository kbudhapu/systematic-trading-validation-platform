"""
Dual-buffer shadow matrix — tape-correction guard for live vs REST bar parity.

Active loop stream: websocket + execution-window snapshots (NumPy ring buffers).
Shadow matrix: staggered REST reconciliation for locked historical blocks.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable

import numpy as np
import structlog

from src.core.market_calendar import MarketSessionCalendar
from src.core.rolling_window import RollingWindow
from src.engine.cold_start_gate import (
    ColdStartGateEngine,
    ColdStartGateState,
    ColdStartPolicy,
    StreamHealthSnapshot,
    parse_cold_start_policy,
    resolve_cold_start_policy,
)
from src.ingestor.alpaca import TIMEFRAME_MINUTES, AlpacaDataIngestor
from src.models import Bar

log = structlog.get_logger()

SHADOW_POST_CLOSE_DELAY_SECONDS = 30.0
SHADOW_REFRESH_POLL_SECONDS = 1.0
# AE2.4 ingester watchdog: how long (as a MULTIPLE of the bar period -- T4 clause 3, never absolute)
# a LIVE leg may go with zero bars promoted into its window before it is declared a dead ingester.
INGESTER_STALL_MULTIPLE = 2.0
INGESTER_WATCHDOG_CHECK_SECONDS = 30.0
LOCKED_BLOCK_COUNT = 5
ACTIVE_RING_CAPACITY = 32
PRICE_FEATURE_TOLERANCE = 1e-4
VOLUME_FEATURE_TOLERANCE = 0.01


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp_ns(value: datetime) -> int:
    return int(_coerce_utc(value).timestamp() * 1_000_000_000)


@dataclass(frozen=True)
class BarFeatureDelta:
    timestamp: str
    field: str
    active_value: float
    shadow_value: float
    delta: float


class TapeStatus(str, Enum):
    """Per-leg tape synchronization state for cold-start and divergence gating."""

    UNKNOWN = "TAPE_STATUS_UNKNOWN"
    VERIFIED = "TAPE_STATUS_VERIFIED"
    DIVERGENT = "TAPE_STATUS_DIVERGENT"


@dataclass(frozen=True)
class MatrixAlignmentVerdict:
    aligned: bool
    compared_bars: int
    mismatches: tuple[BarFeatureDelta, ...] = ()
    latch_engaged: bool = False
    latch_cleared: bool = False
    skip_reason: str | None = None


@dataclass
class _MatrixRing:
    capacity: int
    timestamps: np.ndarray = field(init=False)
    highs: np.ndarray = field(init=False)
    lows: np.ndarray = field(init=False)
    closes: np.ndarray = field(init=False)
    volumes: np.ndarray = field(init=False)
    count: int = 0
    head: int = 0

    def __post_init__(self) -> None:
        cap = max(int(self.capacity), LOCKED_BLOCK_COUNT)
        self.capacity = cap
        self.timestamps = np.zeros(cap, dtype=np.int64)
        self.highs = np.zeros(cap, dtype=np.float64)
        self.lows = np.zeros(cap, dtype=np.float64)
        self.closes = np.zeros(cap, dtype=np.float64)
        self.volumes = np.zeros(cap, dtype=np.float64)

    def clear(self) -> None:
        self.count = 0
        self.head = 0

    def upsert_bar(self, bar: Bar) -> None:
        ts_ns = _timestamp_ns(bar.timestamp)
        if self.count > 0:
            last_idx = (self.head - 1) % self.capacity
            if int(self.timestamps[last_idx]) == ts_ns:
                self.highs[last_idx] = float(bar.high)
                self.lows[last_idx] = float(bar.low)
                self.closes[last_idx] = float(bar.close)
                self.volumes[last_idx] = float(bar.volume)
                return

        idx = self.head
        self.timestamps[idx] = ts_ns
        self.highs[idx] = float(bar.high)
        self.lows[idx] = float(bar.low)
        self.closes[idx] = float(bar.close)
        self.volumes[idx] = float(bar.volume)
        self.head = (idx + 1) % self.capacity
        self.count = min(self.count + 1, self.capacity)

    def replace_bars(self, bars: list[Bar]) -> None:
        self.clear()
        for bar in bars:
            self.upsert_bar(bar)

    def apply_price_offset(self, offset: float) -> bool:
        if offset <= 0.0 or self.count == 0:
            return False
        active = self.count if self.count < self.capacity else self.capacity
        if self.count < self.capacity:
            self.highs[:active] += offset
            self.lows[:active] += offset
            self.closes[:active] += offset
        else:
            self.highs += offset
            self.lows += offset
            self.closes += offset
        return True

    def tail(self, n: int) -> tuple[np.ndarray, ...]:
        take = min(max(int(n), 0), self.count)
        if take == 0:
            empty = np.array([], dtype=np.float64)
            empty_i = np.array([], dtype=np.int64)
            return empty_i, empty, empty, empty, empty
        if self.count < self.capacity:
            start = max(self.count - take, 0)
            sl = slice(start, self.count)
            return (
                self.timestamps[sl].copy(),
                self.highs[sl].copy(),
                self.lows[sl].copy(),
                self.closes[sl].copy(),
                self.volumes[sl].copy(),
            )
        idx = np.arange(self.count - take, self.count)
        positions = (self.head + idx) % self.capacity
        order = np.argsort(self.timestamps[positions])
        positions = positions[order]
        return (
            self.timestamps[positions].copy(),
            self.highs[positions].copy(),
            self.lows[positions].copy(),
            self.closes[positions].copy(),
            self.volumes[positions].copy(),
        )


def _most_recent_bar_close(now: datetime, bar_minutes: int) -> datetime:
    """Return the UTC open timestamp of the most recently closed bar period."""
    now = _coerce_utc(now).replace(second=0, microsecond=0)
    total_min = now.hour * 60 + now.minute
    if total_min % bar_minutes == 0:
        close_min = total_min
    else:
        close_min = ((total_min // bar_minutes) + 1) * bar_minutes
    day_offset = close_min // (24 * 60)
    close_min = close_min % (24 * 60)
    close_dt = now.replace(hour=close_min // 60, minute=close_min % 60)
    if day_offset:
        close_dt = close_dt + timedelta(days=day_offset)
    return close_dt


def _shadow_refresh_due(
    now: datetime,
    *,
    bar_minutes: int,
    last_refreshed_close: datetime | None,
) -> tuple[bool, datetime]:
    close_dt = _most_recent_bar_close(now, bar_minutes)
    refresh_at = close_dt + timedelta(seconds=SHADOW_POST_CLOSE_DELAY_SECONDS)
    if _coerce_utc(now) < refresh_at:
        return False, close_dt
    if last_refreshed_close is not None and last_refreshed_close >= close_dt:
        return False, close_dt
    return True, close_dt


@dataclass
class _SymbolMatrixState:
    symbol: str
    timeframe: str
    asset_class: str
    active: _MatrixRing = field(default_factory=lambda: _MatrixRing(ACTIVE_RING_CAPACITY))
    shadow: _MatrixRing = field(default_factory=lambda: _MatrixRing(LOCKED_BLOCK_COUNT + 4))
    last_shadow_refresh_monotonic: float = 0.0
    last_shadow_refresh_bar_close: datetime | None = None
    strategy_ids: set[str] = field(default_factory=set)


class DualBufferDataCoordinator:
    """
    Cross-references streaming OHLCV rings against REST-reconciled shadow blocks.
    """

    def __init__(self, ingestor: AlpacaDataIngestor | None = None) -> None:
        self._ingestor = ingestor
        self._lock = threading.RLock()
        self._matrices: dict[tuple[str, str], _SymbolMatrixState] = {}
        self._leg_index: dict[str, tuple[str, str]] = {}
        self._leg_windows: dict[str, RollingWindow] = {}
        self._divergent_latches: dict[str, bool] = {}
        self._tape_status_by_strategy: dict[str, TapeStatus] = {}
        self._cold_start_gate_cleared: bool = False
        self._background_shadow_verification_completed: bool = False
        self._cold_start_gate = ColdStartGateEngine()
        self._leg_cold_start_policies: dict[str, ColdStartPolicy] = {}
        self._stream_bar_listener: Callable[[str, str], None] | None = None
        self._stop_event = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        # AE2.4 ingester watchdog: monotonic time each leg window last received a NEW (appended) bar.
        # The system detects STALE BARS but not a DEAD INGESTER; a lagging window on a 24/7 instrument
        # went ten hours unpaged. This lets us detect zero promotions -> page + auto-recover.
        self._last_promotion_mono: dict[tuple[str, str], float] = {}
        self._watchdog_calendar = MarketSessionCalendar()
        self._last_watchdog_check_mono = 0.0
        self._ingester_stalled_keys: set[tuple[str, str]] = set()

    def initialize_cold_start_state(self) -> None:
        with self._lock:
            strategy_ids = list(self._leg_index.keys())
            for strategy_id in strategy_ids:
                self._tape_status_by_strategy[strategy_id] = TapeStatus.UNKNOWN
                self._divergent_latches[strategy_id] = False
            self._cold_start_gate_cleared = False
            self._background_shadow_verification_completed = False
        self._cold_start_gate.begin_cold_start(
            strategy_ids,
            policies={
                strategy_id: self._leg_cold_start_policies.get(
                    strategy_id,
                    ColdStartPolicy.FAIL_SAFE_CLOSE,
                )
                for strategy_id in strategy_ids
            },
            symbols={
                strategy_id: key[0]
                for strategy_id, key in self._leg_index.items()
                if strategy_id in strategy_ids
            },
        )
        log.info(
            "dual_buffer_cold_start_initialized",
            leg_count=len(strategy_ids),
        )

    def cold_start_gate_state(self, strategy_id: str) -> ColdStartGateState:
        return self._cold_start_gate.state(strategy_id)

    def cold_start_entry_scale(self, strategy_id: str) -> float:
        if self.is_divergent_latch(strategy_id):
            return 0.0
        symbol = self._leg_index.get(strategy_id, ("", ""))[0]
        return self._cold_start_gate.cold_start_entry_scale(
            strategy_id,
            symbol=symbol or None,
        )

    def blocks_all_entry_signals(self, strategy_id: str) -> bool:
        if self.is_divergent_latch(strategy_id):
            return True
        return self._cold_start_gate.blocks_all_entries(strategy_id)

    def tick_cold_start_gates(self, *, readiness_ok: bool = True) -> None:
        # R4 (INCIDENT-20260722): `readiness_ok=False` (pre_open_readiness failed) SUSPENDS the
        # cold-start time-bound clock -- it cannot progress a leg while the readiness gate is unmet.
        with self._lock:
            leg_index = dict(self._leg_index)
            background_verified = self._background_shadow_verification_completed
        for strategy_id, key in leg_index.items():
            symbol, timeframe = key
            stream_health = self._stream_health_for_key(key)
            with self._lock:
                divergent = bool(self._divergent_latches.get(strategy_id, False))
            verdict = self.verify_matrix_alignment(
                symbol,
                timeframe,
                strategy_id=None,
            )
            self._cold_start_gate.tick_time_bound(
                strategy_id,
                symbol=symbol,
                timeframe=timeframe,
                stream_health=stream_health,
                matrix_aligned=verdict.aligned and verdict.skip_reason is None,
                background_verified=background_verified,
                readiness_ok=readiness_ok,
            )
        self._sync_cold_start_gate_cleared()

    def _stream_health_for_key(self, key: tuple[str, str]) -> StreamHealthSnapshot:
        # H2: freshness must be measured on the CLOSED signal bar the strategy actually
        # decides on — NOT the forming websocket bar in the active matrix. The oscillating
        # 70s->930s->70s lag was never staleness; it was how far into the current bar we
        # were. Read the leg RollingWindow (closed-only); fall back to the active matrix only
        # while the leg window is still warming (no closed bars yet).
        with self._lock:
            leg_window = None
            for strategy_id, leg_key in self._leg_index.items():
                if leg_key == key:
                    candidate = self._leg_windows.get(strategy_id)
                    if candidate is not None and len(candidate) > 0:
                        leg_window = candidate
                        break
            if leg_window is not None:
                closed_bars = leg_window.chronological_bars()
                latest_bar = closed_bars[-1]
                prior_bar = closed_bars[-2] if len(closed_bars) > 1 else None
                return StreamHealthSnapshot(
                    latest_bar_timestamp=latest_bar.timestamp,
                    prior_bar_timestamp=prior_bar.timestamp if prior_bar is not None else None,
                    latest_volume=float(latest_bar.volume),
                    prior_volume=float(prior_bar.volume) if prior_bar is not None else 0.0,
                    bar_count=len(leg_window),
                )
            state = self._matrices.get(key)
            if state is None:
                return StreamHealthSnapshot()
            active = state.active
            bar_count = active.count
            if bar_count == 0:
                return StreamHealthSnapshot()
            take = min(bar_count, 2)
            ts, _, _, _, volumes = active.tail(take)
        if ts.size == 0:
            return StreamHealthSnapshot(bar_count=bar_count)
        latest_ts = datetime.fromtimestamp(int(ts[-1]) / 1_000_000_000, tz=timezone.utc)
        prior_ts = (
            datetime.fromtimestamp(int(ts[-2]) / 1_000_000_000, tz=timezone.utc)
            if ts.size > 1
            else None
        )
        latest_vol = float(volumes[-1]) if volumes.size else 0.0
        prior_vol = float(volumes[-2]) if volumes.size > 1 else 0.0
        return StreamHealthSnapshot(
            latest_bar_timestamp=latest_ts,
            prior_bar_timestamp=prior_ts,
            latest_volume=latest_vol,
            prior_volume=prior_vol,
            bar_count=bar_count,
        )

    def tape_status(self, strategy_id: str) -> TapeStatus:
        with self._lock:
            return self._tape_status_by_strategy.get(strategy_id, TapeStatus.UNKNOWN)

    def is_cold_start_gate_cleared(self) -> bool:
        with self._lock:
            return self._cold_start_gate_cleared

    def blocks_entry_signals(self, strategy_id: str) -> bool:
        if self.is_divergent_latch(strategy_id):
            return True
        symbol = self._leg_index.get(strategy_id, ("", ""))[0]
        if self._cold_start_gate.blocks_entry_signals(strategy_id, symbol=symbol or None):
            return True
        gate_state = self._cold_start_gate.state(strategy_id)
        if gate_state == ColdStartGateState.STABILIZED_VERIFIED:
            with self._lock:
                tape = self._tape_status_by_strategy.get(strategy_id, TapeStatus.UNKNOWN)
            return tape == TapeStatus.DIVERGENT
        return False

    def set_stream_bar_listener(
        self,
        listener: Callable[[str, str], None] | None,
    ) -> None:
        """Optional callback invoked when a websocket bar closes (new timestamp)."""
        self._stream_bar_listener = listener

    def configure_ingestor(self, ingestor: AlpacaDataIngestor) -> None:
        with self._lock:
            self._ingestor = ingestor

    def start(self) -> None:
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            return
        self._stop_event.clear()
        self._refresh_thread = threading.Thread(
            target=self._shadow_refresh_loop,
            name="dual-buffer-shadow-refresh",
            daemon=True,
        )
        self._refresh_thread.start()
        log.info("dual_buffer_shadow_refresh_started")

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        self._stop_event.set()
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=max(timeout_seconds, 0.1))
            self._refresh_thread = None
        log.info("dual_buffer_shadow_refresh_stopped")

    def register_leg(
        self,
        strategy_id: str,
        symbol: str,
        timeframe: str,
        *,
        asset_class: str = "stock",
        window: RollingWindow | None = None,
        strategy_params: dict | None = None,
        cold_start_policy: ColdStartPolicy | str | None = None,
    ) -> None:
        key = (symbol.upper(), timeframe)
        resolved_policy = (
            parse_cold_start_policy(cold_start_policy)
            if cold_start_policy is not None
            else resolve_cold_start_policy(strategy_params, symbol=symbol)
        )
        with self._lock:
            state = self._matrices.get(key)
            if state is None:
                state = _SymbolMatrixState(
                    symbol=symbol.upper(),
                    timeframe=timeframe,
                    asset_class=asset_class,
                )
                self._matrices[key] = state
            state.strategy_ids.add(strategy_id)
            state.asset_class = asset_class
            self._leg_index[strategy_id] = key
            if window is not None:
                self._leg_windows[strategy_id] = window
            if strategy_id not in self._divergent_latches:
                self._divergent_latches[strategy_id] = False
            if (
                not self._cold_start_gate_cleared
                or strategy_id not in self._tape_status_by_strategy
            ):
                self._tape_status_by_strategy[strategy_id] = TapeStatus.UNKNOWN
            self._leg_cold_start_policies[strategy_id] = resolved_policy
            self._cold_start_gate.set_policy(strategy_id, resolved_policy)
            self._cold_start_gate.set_symbol(strategy_id, symbol)

    def unregister_leg(self, strategy_id: str) -> None:
        with self._lock:
            key = self._leg_index.pop(strategy_id, None)
            self._divergent_latches.pop(strategy_id, None)
            self._tape_status_by_strategy.pop(strategy_id, None)
            self._leg_windows.pop(strategy_id, None)
            self._leg_cold_start_policies.pop(strategy_id, None)
            if key is None:
                return
            state = self._matrices.get(key)
            if state is None:
                return
            state.strategy_ids.discard(strategy_id)
            if not state.strategy_ids:
                self._matrices.pop(key, None)

    def ingest_stream_bar(self, bar: Bar, timeframe: str) -> str:
        """Feed websocket bars into the active matrix and primary leg RollingWindows."""
        key = (bar.symbol.upper(), timeframe)
        stream_outcome = "ignored"
        listener: Callable[[str, str], None] | None = None
        with self._lock:
            state = self._matrices.get(key)
            if state is None:
                state = _SymbolMatrixState(
                    symbol=bar.symbol.upper(),
                    timeframe=timeframe,
                    asset_class="stock",
                )
                self._matrices[key] = state
            state.active.upsert_bar(bar)
            targets = [
                window
                for strategy_id, window in self._leg_windows.items()
                if self._leg_index.get(strategy_id) == key
            ]
            listener = self._stream_bar_listener

        for window in targets:
            # H1a (parity fix): a live websocket bar is the CURRENTLY-FORMING bar for its
            # period (Alpaca streams sub-period 1-min bars for a 15Min leg). It must NEVER be
            # appended to the signal window — that is exactly what made `window.latest()` a
            # forming bar and broke bar-completeness parity. It goes to the forming SLOT only;
            # the CLOSED signal bars come from the REST-authoritative shadow reconcile.
            outcome = window.set_forming(bar)
            if outcome == "set" and stream_outcome == "ignored":
                stream_outcome = "forming"

        if stream_outcome == "forming" and listener is not None:
            try:
                listener(bar.symbol.upper(), timeframe)
            except Exception as exc:
                log.warning(
                    "stream_bar_listener_failed",
                    symbol=bar.symbol,
                    timeframe=timeframe,
                    error=str(exc),
                )
        return stream_outcome

    def sync_active_from_window(
        self,
        symbol: str,
        timeframe: str,
        window: RollingWindow,
    ) -> None:
        """Mirror the execution rolling window into the active streaming matrix."""
        bars = window.chronological_bars()
        if not bars:
            return
        key = (symbol.upper(), timeframe)
        with self._lock:
            state = self._matrices.get(key)
            if state is None:
                return
            state.active.clear()
            for bar in bars:
                state.active.upsert_bar(
                    Bar(
                        timestamp=bar.timestamp,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                        symbol=symbol.upper(),
                    )
                )

    def apply_corporate_action_offset(
        self,
        symbol: str,
        timeframe: str,
        offset: float,
    ) -> bool:
        """Apply ex-dividend / split adjustment to active and shadow matrices atomically."""
        if offset <= 0.0:
            return False
        key = (symbol.upper(), timeframe)
        with self._lock:
            state = self._matrices.get(key)
            if state is None:
                return False
            active_applied = state.active.apply_price_offset(offset)
            shadow_applied = state.shadow.apply_price_offset(offset)
            return active_applied or shadow_applied

    def is_divergent_latch(self, strategy_id: str) -> bool:
        with self._lock:
            return bool(self._divergent_latches.get(strategy_id, False))

    def any_tape_latch_active(self) -> bool:
        with self._lock:
            if not self._cold_start_gate_cleared:
                return True
            return any(self._divergent_latches.values())

    def shadow_matrix_lag_seconds(self) -> float | None:
        """Seconds since the most recent bar-aligned REST shadow verification pass."""
        now = time.monotonic()
        with self._lock:
            lags: list[float] = []
            for state in self._matrices.values():
                if state.last_shadow_refresh_monotonic <= 0.0:
                    continue
                lags.append(max(now - state.last_shadow_refresh_monotonic, 0.0))
        if not lags:
            return None
        return float(max(lags))

    def telemetry_snapshot(self) -> dict[str, Any]:
        with self._lock:
            tape_status_by_strategy = {
                strategy_id: status.value
                for strategy_id, status in self._tape_status_by_strategy.items()
            }
            unknown_strategy_ids = sorted(
                strategy_id
                for strategy_id, status in self._tape_status_by_strategy.items()
                if status == TapeStatus.UNKNOWN
            )
            divergent_strategy_ids = sorted(
                strategy_id
                for strategy_id, active in self._divergent_latches.items()
                if active
            )
            cold_start_gate_cleared = self._cold_start_gate_cleared
            background_verified = self._background_shadow_verification_completed
            gate_telemetry = self._cold_start_gate.telemetry_snapshot()
        return {
            "shadow_matrix_lag_seconds": self.shadow_matrix_lag_seconds(),
            "tape_latch_active": self.any_tape_latch_active(),
            "cold_start_gate_cleared": cold_start_gate_cleared,
            "background_shadow_verification_completed": background_verified,
            "tape_status_by_strategy": tape_status_by_strategy,
            "unknown_strategy_ids": unknown_strategy_ids,
            "divergent_strategy_ids": divergent_strategy_ids,
            **gate_telemetry,
        }

    def verify_matrix_alignment(
        self,
        symbol: str,
        timeframe: str,
        *,
        strategy_id: str | None = None,
        lookback_window: int = LOCKED_BLOCK_COUNT,
    ) -> MatrixAlignmentVerdict:
        key = (symbol.upper(), timeframe)
        with self._lock:
            state = self._matrices.get(key)
            if state is None:
                return MatrixAlignmentVerdict(
                    aligned=True,
                    compared_bars=0,
                    skip_reason="matrix_not_registered",
                )
            if state.shadow.count < lookback_window:
                return MatrixAlignmentVerdict(
                    aligned=True,
                    compared_bars=state.shadow.count,
                    skip_reason="shadow_warming",
                )
            if state.active.count < lookback_window:
                return MatrixAlignmentVerdict(
                    aligned=True,
                    compared_bars=state.active.count,
                    skip_reason="active_warming",
                )

            active_ts, a_high, a_low, a_close, a_vol = state.active.tail(lookback_window)
            shadow_ts, s_high, s_low, s_close, s_vol = state.shadow.tail(lookback_window)
            mismatches = _compare_feature_matrices(
                active_ts,
                a_high,
                a_low,
                a_close,
                a_vol,
                shadow_ts,
                s_high,
                s_low,
                s_close,
                s_vol,
            )
            compared = min(len(active_ts), len(shadow_ts))
            latch_engaged = False
            latch_cleared = False

            if mismatches:
                if strategy_id is not None and not self._divergent_latches.get(
                    strategy_id, False
                ):
                    self._divergent_latches[strategy_id] = True
                    latch_engaged = True
                verdict = MatrixAlignmentVerdict(
                    aligned=False,
                    compared_bars=compared,
                    mismatches=mismatches,
                    latch_engaged=latch_engaged,
                )
                if strategy_id is not None:
                    self._apply_verdict_tape_status(strategy_id, verdict)
                return verdict

            if strategy_id is not None and self._divergent_latches.get(strategy_id, False):
                self._divergent_latches[strategy_id] = False
                latch_cleared = True
            verdict = MatrixAlignmentVerdict(
                aligned=True,
                compared_bars=compared,
                latch_cleared=latch_cleared,
            )
            if strategy_id is not None:
                self._apply_verdict_tape_status(strategy_id, verdict)
            return verdict

    def _apply_verdict_tape_status(
        self,
        strategy_id: str,
        verdict: MatrixAlignmentVerdict,
    ) -> None:
        with self._lock:
            key = self._leg_index.get(strategy_id)
            background_verified = self._background_shadow_verification_completed
            divergent = bool(self._divergent_latches.get(strategy_id, False))
            if verdict.skip_reason in {
                "shadow_warming",
                "active_warming",
                "matrix_not_registered",
            }:
                if not self._cold_start_gate_cleared:
                    self._tape_status_by_strategy[strategy_id] = TapeStatus.UNKNOWN
                return
            if verdict.aligned:
                self._tape_status_by_strategy[strategy_id] = TapeStatus.VERIFIED
            else:
                self._tape_status_by_strategy[strategy_id] = TapeStatus.DIVERGENT
        if key is None:
            return
        symbol, timeframe = key
        stream_health = self._stream_health_for_key(key)
        self._cold_start_gate.evaluate_verification(
            strategy_id,
            symbol=symbol,
            timeframe=timeframe,
            matrix_aligned=verdict.aligned,
            matrix_skip_reason=verdict.skip_reason,
            stream_health=stream_health,
            background_verified=background_verified,
            divergent=divergent,
        )
        self._sync_cold_start_gate_cleared()

    def _sync_cold_start_gate_cleared(self) -> None:
        with self._lock:
            if not self._background_shadow_verification_completed:
                return
            if not self._leg_index:
                return
            if not self._cold_start_gate.operational_gate_cleared():
                return
            self._cold_start_gate_cleared = True
        log.info(
            "dual_buffer_cold_start_gate_cleared",
            leg_count=len(self._leg_index),
            fully_verified=self._cold_start_gate.fully_verified(),
        )

    def _maybe_clear_cold_start_gate(self) -> None:
        self._sync_cold_start_gate_cleared()

    def _apply_tape_verification_for_state(
        self,
        state: _SymbolMatrixState,
        *,
        from_background: bool,
    ) -> None:
        if from_background:
            with self._lock:
                self._background_shadow_verification_completed = True
        with self._lock:
            strategy_ids = list(state.strategy_ids)
        for strategy_id in strategy_ids:
            self.verify_matrix_alignment(
                state.symbol,
                state.timeframe,
                strategy_id=strategy_id,
            )

    def _shadow_refresh_loop(self) -> None:
        last_tick = time.monotonic()
        while not self._stop_event.wait(SHADOW_REFRESH_POLL_SECONDS):
            tick = time.monotonic()
            actual_elapsed = tick - last_tick
            last_tick = tick
            started = tick
            try:
                self._refresh_due_shadow_buffers()
            except Exception as exc:
                log.warning("dual_buffer_shadow_refresh_failed", error=str(exc))
            iteration_duration = time.monotonic() - started
            # AE2.1: the shadow-refresh THREAD must wake on time. If actual_elapsed >> the intended
            # 1s period, the thread is being STARVED (page-fault / scheduling under memory pressure)
            # and the leg windows fall behind by exactly that drift -- the "~26% rate deficit". This
            # is the ONE measurement that decides event-loop/thread starvation vs a bug inside
            # reconcile. Logged only when slow, so a healthy 1s tick does not flood the journal.
            drift = actual_elapsed - SHADOW_REFRESH_POLL_SECONDS
            if drift > SHADOW_REFRESH_POLL_SECONDS or iteration_duration > SHADOW_REFRESH_POLL_SECONDS:
                log.warning(
                    "shadow_refresh_loop_slow",
                    intended_period_s=SHADOW_REFRESH_POLL_SECONDS,
                    actual_elapsed_s=round(actual_elapsed, 3),
                    drift_s=round(drift, 3),
                    iteration_duration_s=round(iteration_duration, 3),
                )
            if tick - self._last_watchdog_check_mono >= INGESTER_WATCHDOG_CHECK_SECONDS:
                self._last_watchdog_check_mono = tick
                try:
                    self._check_ingestion_watchdog()
                except Exception as exc:
                    log.warning("ingester_watchdog_failed", error=str(exc))

    def _check_ingestion_watchdog(self) -> None:
        """AE2.4 — a heartbeat on the INGESTION PIPELINE itself. For each LIVE leg, if zero bars have
        been promoted into its window for > N (period-relative), the ingester is DEAD, not merely
        stale: log/page AND force a shadow re-fetch so bar_freshness_critical can actually clear (its
        exit was behind a dead producer). DORMANT IS NOT DEAD (T4 c4): an equity leg out of session
        receives none CORRECTLY and is exempt -- via the SAME calendar, never a second one."""
        now_mono = time.monotonic()
        with self._lock:
            states = list(self._matrices.values())
            ingestor = self._ingestor
        now_utc = datetime.now(timezone.utc)
        for state in states:
            key = (state.symbol, state.timeframe)
            if state.asset_class != "crypto" and not self._watchdog_calendar.is_within_rth(now_utc):
                self._ingester_stalled_keys.discard(key)  # dormant, not dead
                continue
            last = self._last_promotion_mono.get(key)
            if last is None:
                self._last_promotion_mono[key] = now_mono   # still bootstrapping; seed, don't fire
                continue
            bar_period_s = TIMEFRAME_MINUTES.get(state.timeframe, 15) * 60.0
            threshold_s = INGESTER_STALL_MULTIPLE * bar_period_s
            stall_s = now_mono - last
            if stall_s <= threshold_s:
                continue
            self._ingester_stalled_keys.add(key)
            log.warning(
                "ingester_stall_detected",
                symbol=state.symbol,
                timeframe=state.timeframe,
                asset_class=state.asset_class,
                stall_seconds=round(stall_s, 1),
                threshold_seconds=round(threshold_s, 1),
                action="force_shadow_refetch",
            )
            if ingestor is not None:
                try:
                    self._refresh_shadow_for_state(state, ingestor)  # open the dead door
                except Exception as exc:
                    log.warning(
                        "ingester_watchdog_recovery_failed", symbol=state.symbol, error=str(exc)
                    )

    def _refresh_due_shadow_buffers(self) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            targets = list(self._matrices.values())
            ingestor = self._ingestor
        if ingestor is None:
            return
        for state in targets:
            bar_minutes = TIMEFRAME_MINUTES.get(state.timeframe, 15)
            due, close_dt = _shadow_refresh_due(
                now,
                bar_minutes=bar_minutes,
                last_refreshed_close=state.last_shadow_refresh_bar_close,
            )
            if not due:
                continue
            try:
                self._refresh_shadow_for_state(
                    state,
                    ingestor,
                    bar_close=close_dt,
                    from_background=True,
                )
            except Exception as exc:
                log.warning(
                    "dual_buffer_shadow_symbol_refresh_failed",
                    symbol=state.symbol,
                    timeframe=state.timeframe,
                    error=str(exc),
                )

    def refresh_all_shadow_buffers(self) -> None:
        with self._lock:
            targets = list(self._matrices.values())
            ingestor = self._ingestor
        if ingestor is None:
            return
        for state in targets:
            try:
                self._refresh_shadow_for_state(state, ingestor)
            except Exception as exc:
                log.warning(
                    "dual_buffer_shadow_symbol_refresh_failed",
                    symbol=state.symbol,
                    timeframe=state.timeframe,
                    error=str(exc),
                )

    def _refresh_shadow_for_state(
        self,
        state: _SymbolMatrixState,
        ingestor: AlpacaDataIngestor,
        *,
        bar_close: datetime | None = None,
        from_background: bool = False,
    ) -> None:
        bar_minutes = TIMEFRAME_MINUTES.get(state.timeframe, 15)
        end = bar_close or _most_recent_bar_close(datetime.now(timezone.utc), bar_minutes)
        end = _coerce_utc(end)
        start = end - timedelta(minutes=bar_minutes * (LOCKED_BLOCK_COUNT + 4))
        bars = ingestor._fetch_bars_sync(
            state.symbol,
            state.timeframe,
            start,
            end,
            state.asset_class,
        )
        if not bars:
            return
        locked = bars[-LOCKED_BLOCK_COUNT:]
        with self._lock:
            key = (state.symbol, state.timeframe)
            current = self._matrices.get(key)
            if current is None:
                return
            current.shadow.replace_bars(locked)
            current.last_shadow_refresh_monotonic = time.monotonic()
            current.last_shadow_refresh_bar_close = end
        reconciled = self.reconcile_streaming_windows(
            state.symbol,
            state.timeframe,
            authority_bars=locked,
        )
        self._apply_tape_verification_for_state(state, from_background=from_background)
        log.debug(
            "dual_buffer_shadow_refreshed",
            symbol=state.symbol,
            timeframe=state.timeframe,
            bars=len(locked),
            bar_close=end.isoformat(),
            reconciled_bars=reconciled,
        )

    def reconcile_streaming_windows(
        self,
        symbol: str,
        timeframe: str,
        *,
        authority_bars: list[Bar] | None = None,
    ) -> int:
        """
        Apply REST-authoritative shadow bars onto websocket-fed leg windows.

        Overwrites anomalies and back-fills missing points inside the active
        streaming arrays without replacing the websocket-primary ingest path.
        """
        key = (symbol.upper(), timeframe)
        bars = authority_bars
        if bars is None:
            bars = self._shadow_bars_as_list(key)
        if not bars:
            return 0

        with self._lock:
            state = self._matrices.get(key)
            targets = [
                window
                for strategy_id, window in self._leg_windows.items()
                if self._leg_index.get(strategy_id) == key
            ]

        applied = 0
        if state is not None:
            with self._lock:
                for bar in bars:
                    state.active.upsert_bar(bar)

        shadow_newest = bars[-1].timestamp if bars else None
        bar_period_s = TIMEFRAME_MINUTES.get(timeframe, 15) * 60.0
        key_appended = 0
        for window in targets:
            last_before = window.last_timestamp
            appended = updated = ignored = 0
            for bar in bars:
                outcome = window.upsert_bar(bar)
                if outcome == "appended":
                    appended += 1
                elif outcome == "updated":
                    updated += 1
                else:
                    ignored += 1
            applied += appended + updated
            key_appended += appended
            last_after = window.last_timestamp
            lag_bars = None
            if shadow_newest is not None and last_after is not None and bar_period_s > 0:
                lag_bars = round((shadow_newest - last_after).total_seconds() / bar_period_s, 2)
            # AE2.3: report the TRUTH, not the target close. The old `bar_close=end` log said nothing
            # for ten hours because it echoed the REQUEST, not the DATA. lag_bars > 1 means the window
            # is behind the fresh shadow -- visible immediately.
            log.info(
                "reconcile_window_truth",
                symbol=symbol,
                timeframe=timeframe,
                window_last_before=last_before.isoformat() if last_before else None,
                shadow_newest=shadow_newest.isoformat() if shadow_newest else None,
                bars_offered=len(bars),
                bars_appended=appended,
                bars_updated=updated,
                bars_ignored=ignored,
                window_last_after=last_after.isoformat() if last_after else None,
                lag_bars=lag_bars,
            )
        if key_appended > 0:
            # AE2.4: a NEW bar reached the window -> the ingester for this leg is alive.
            self._last_promotion_mono[key] = time.monotonic()
            self._ingester_stalled_keys.discard(key)
        return applied

    def _shadow_bars_as_list(self, key: tuple[str, str]) -> list[Bar]:
        with self._lock:
            state = self._matrices.get(key)
            if state is None or state.shadow.count == 0:
                return []
            ts, highs, lows, closes, volumes = state.shadow.tail(state.shadow.count)
            symbol = state.symbol
        bars: list[Bar] = []
        for idx in range(len(ts)):
            bars.append(
                Bar(
                    timestamp=datetime.fromtimestamp(
                        int(ts[idx]) / 1_000_000_000,
                        tz=timezone.utc,
                    ),
                    open=float(closes[idx]),
                    high=float(highs[idx]),
                    low=float(lows[idx]),
                    close=float(closes[idx]),
                    volume=float(volumes[idx]),
                    symbol=symbol,
                )
            )
        return bars

    def refresh_shadow_now(self, symbol: str, timeframe: str) -> None:
        """Force an immediate shadow refresh (used during tests and boot)."""
        key = (symbol.upper(), timeframe)
        with self._lock:
            state = self._matrices.get(key)
            ingestor = self._ingestor
        if state is None or ingestor is None:
            return
        self._refresh_shadow_for_state(state, ingestor)


def _price_close(a: float, b: float) -> bool:
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= max(PRICE_FEATURE_TOLERANCE, PRICE_FEATURE_TOLERANCE * scale)


def _volume_close(a: float, b: float) -> bool:
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= max(VOLUME_FEATURE_TOLERANCE, VOLUME_FEATURE_TOLERANCE * scale)


def _compare_feature_matrices(
    active_ts: np.ndarray,
    a_high: np.ndarray,
    a_low: np.ndarray,
    a_close: np.ndarray,
    a_vol: np.ndarray,
    shadow_ts: np.ndarray,
    s_high: np.ndarray,
    s_low: np.ndarray,
    s_close: np.ndarray,
    s_vol: np.ndarray,
) -> tuple[BarFeatureDelta, ...]:
    shadow_index = {int(ts): idx for idx, ts in enumerate(shadow_ts)}
    deltas: list[BarFeatureDelta] = []

    for idx, ts in enumerate(active_ts):
        shadow_idx = shadow_index.get(int(ts))
        if shadow_idx is None:
            continue
        ts_label = datetime.fromtimestamp(int(ts) / 1_000_000_000, tz=timezone.utc).isoformat()
        checks = (
            ("high", float(a_high[idx]), float(s_high[shadow_idx])),
            ("low", float(a_low[idx]), float(s_low[shadow_idx])),
            ("close", float(a_close[idx]), float(s_close[shadow_idx])),
            ("volume", float(a_vol[idx]), float(s_vol[shadow_idx])),
        )
        for field_name, active_value, shadow_value in checks:
            close_fn = _volume_close if field_name == "volume" else _price_close
            if not close_fn(active_value, shadow_value):
                deltas.append(
                    BarFeatureDelta(
                        timestamp=ts_label,
                        field=field_name,
                        active_value=active_value,
                        shadow_value=shadow_value,
                        delta=active_value - shadow_value,
                    )
                )
    return tuple(deltas)


_coordinator: DualBufferDataCoordinator | None = None
_coordinator_lock = threading.Lock()


def get_dual_buffer_coordinator() -> DualBufferDataCoordinator:
    global _coordinator
    with _coordinator_lock:
        if _coordinator is None:
            _coordinator = DualBufferDataCoordinator()
        return _coordinator


def get_dual_buffer_telemetry() -> dict[str, Any]:
    """Return shadow-matrix lag and tape latch state for dashboard snapshots."""
    return get_dual_buffer_coordinator().telemetry_snapshot()


def configure_dual_buffer_coordinator(
    ingestor: AlpacaDataIngestor,
) -> DualBufferDataCoordinator:
    global _coordinator
    with _coordinator_lock:
        if _coordinator is None:
            _coordinator = DualBufferDataCoordinator(ingestor)
        else:
            _coordinator.configure_ingestor(ingestor)
        return _coordinator
