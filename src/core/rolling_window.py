"""
Live rolling OHLCV window for real-time execution.

Data layer (per .cursorrules):
  - collections.deque  → stores Bar objects (live tracking window)
  - numpy ring buffers → incremental views for Numba @njit indicators (rule 3)
  - Polars             → NOT used here; only in ingestor.fetch_historical()
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import replace
from datetime import datetime

import numpy as np
import structlog

from src.models import Bar

log = structlog.get_logger()

# AE2.2: rate-limit the rejection LOG LINE (never the check) so a flood cannot drown the journal,
# but a silent discard on the live data path is never allowed again.
_REJECT_LOG_MIN_INTERVAL_S = 30.0


class RollingWindow:
    """
    Deque-backed rolling window with incremental numpy ring buffers.

    Appending is O(1). Array views for indicators are built from pre-allocated
    buffers — no list comprehension or per-tick allocation in the hot path.
    """

    __slots__ = (
        "_bars",
        "_maxlen",
        "_last_ts",
        "_count",
        "_head",
        "_closes",
        "_highs",
        "_lows",
        "_opens",
        "_volumes",
        "_applied_corporate_offset",
        "_forming",
        "label",
        "_last_reject_log_mono",
    )

    def __init__(self, maxlen: int, *, label: str = "") -> None:
        if maxlen <= 0:
            raise ValueError("maxlen must be positive")
        self._maxlen = maxlen
        # AE2.2: an optional "SYMBOL:TIMEFRAME" tag so a rejection log names the window it happened in.
        self.label = label
        self._last_reject_log_mono = 0.0
        self._bars: deque[Bar] = deque(maxlen=maxlen)
        self._last_ts: datetime | None = None
        self._count = 0
        self._head = 0
        self._closes = np.zeros(maxlen, dtype=np.float64)
        self._highs = np.zeros(maxlen, dtype=np.float64)
        self._lows = np.zeros(maxlen, dtype=np.float64)
        self._opens = np.zeros(maxlen, dtype=np.float64)
        self._volumes = np.zeros(maxlen, dtype=np.float64)
        self._applied_corporate_offset = 0.0
        # H1a (parity fix): the CURRENTLY-FORMING (in-progress, not-yet-closed) bar is held
        # HERE, entirely OUTSIDE the deque and ring buffers, so it can NEVER reach the signal
        # path. `latest()` / `*_array()` see only CLOSED bars; a price reference that
        # legitimately needs the live price reads `forming_bar()` / `latest_including_forming()`.
        # This makes the open/forming-vs-closed mistake unrepresentable, not merely avoided.
        self._forming: Bar | None = None

    @property
    def corporate_action_offset_applied(self) -> float:
        return self._applied_corporate_offset

    def apply_corporate_action_offset(self, offset: float) -> bool:
        if offset <= 0.0 or self._count == 0 or self._applied_corporate_offset > 0.0:
            return False
        self._applied_corporate_offset = float(offset)
        active = self._count if self._count < self._maxlen else self._maxlen
        if self._count < self._maxlen:
            self._opens[:active] += offset
            self._highs[:active] += offset
            self._lows[:active] += offset
            self._closes[:active] += offset
        else:
            self._opens += offset
            self._highs += offset
            self._lows += offset
            self._closes += offset
        rebuilt: deque[Bar] = deque(maxlen=self._maxlen)
        for bar in self._bars:
            rebuilt.append(
                replace(
                    bar,
                    open=bar.open + offset,
                    high=bar.high + offset,
                    low=bar.low + offset,
                    close=bar.close + offset,
                )
            )
        self._bars = rebuilt
        return True

    def __len__(self) -> int:
        """Number of bars currently held in the window."""
        return self._count

    def clear(self) -> None:
        """Reset to empty state. Used before seeding from REST history when streaming
        bars are already present and would otherwise cause upsert_bar to ignore all
        historical bars as stale (their timestamps predate the streaming _last_ts)."""
        self._bars.clear()
        self._last_ts = None
        self._count = 0
        self._head = 0
        self._forming = None

    @property
    def last_timestamp(self) -> datetime | None:
        """Timestamp of the most recently appended bar, if any."""
        return self._last_ts

    def _note_rejection(self, bar: Bar, reason: str) -> None:
        """AE2.2: a bar was DISCARDED. Never silent — log it (rate-limited per window). A component
        dropping data on the live path and telling no one is the disease that hid the 10h freeze."""
        now = time.monotonic()
        if now - self._last_reject_log_mono < _REJECT_LOG_MIN_INTERVAL_S:
            return
        self._last_reject_log_mono = now
        log.warning(
            "rolling_window_bar_rejected",
            window=self.label or "unlabelled",
            rejected_ts=bar.timestamp.isoformat() if getattr(bar, "timestamp", None) else None,
            window_last_ts=self._last_ts.isoformat() if self._last_ts else None,
            reason=reason,
        )

    def append(self, bar: Bar) -> bool:
        """
        Append a bar in chronological order.

        Returns False if the bar is a duplicate or out-of-order timestamp.
        """
        if self._last_ts is not None and bar.timestamp <= self._last_ts:
            self._note_rejection(
                bar, "duplicate" if bar.timestamp == self._last_ts else "stale_or_out_of_order"
            )
            return False

        self._write_bar_slot(bar)
        self._bars.append(bar)
        self._last_ts = bar.timestamp
        return True

    def upsert_bar(self, bar: Bar) -> str:
        """
        WebSocket-primary ingest path: revise the forming bar or append a new close.

        Returns ``appended`` when a new timestamp is added, ``updated`` when the
        latest bar is revised in place, or ``ignored`` for stale/out-of-order data.
        """
        if self._count == 0:
            self._write_bar_slot(bar)
            self._bars.append(bar)
            self._last_ts = bar.timestamp
            return "appended"

        latest = self._bars[-1]
        if bar.timestamp == latest.timestamp:
            self._rewrite_latest(bar)
            return "updated"

        if bar.timestamp > latest.timestamp:
            return "appended" if self.append(bar) else "ignored"
        self._note_rejection(bar, "out_of_order_before_latest")
        return "ignored"

    def _write_bar_slot(self, bar: Bar) -> None:
        idx = self._head
        self._opens[idx] = bar.open
        self._highs[idx] = bar.high
        self._lows[idx] = bar.low
        self._closes[idx] = bar.close
        self._volumes[idx] = bar.volume
        self._head = (idx + 1) % self._maxlen
        self._count = min(self._count + 1, self._maxlen)

    def _rewrite_latest(self, bar: Bar) -> None:
        if self._count == 0:
            return
        latest_idx = (self._head - 1) % self._maxlen
        self._opens[latest_idx] = bar.open
        self._highs[latest_idx] = bar.high
        self._lows[latest_idx] = bar.low
        self._closes[latest_idx] = bar.close
        self._volumes[latest_idx] = bar.volume
        self._bars[-1] = bar
        self._last_ts = bar.timestamp

    def is_ready(self, min_bars: int) -> bool:
        """True when the window holds at least `min_bars` observations."""
        return self._count >= min_bars

    def latest(self) -> Bar | None:
        """Most recent CLOSED Bar, or None if no closed bars. NEVER the forming bar —
        this is the bar the signal path decides on, identically to the backtest."""
        return self._bars[-1] if self._bars else None

    def set_forming(self, bar: Bar) -> str:
        """H1a: record the currently-forming (in-progress) bar OUTSIDE the closed window.

        This is the ONLY ingest path for sub-period / not-yet-closed bars (e.g. the live
        websocket's intra-period ticks). It writes NOTHING to the deque or ring buffers, so
        the forming bar can never contaminate `latest()` / `*_array()`. A definitively CLOSED
        bar still enters via `append` / `upsert_bar`.

        Returns ``set`` when the forming bar is (re)recorded, or ``ignored`` when the bar is
        older than the latest closed bar or older than the current forming bar (out-of-order)."""
        if self._last_ts is not None and bar.timestamp <= self._last_ts:
            # already covered by a closed bar — not a forming bar for the current period
            return "ignored"
        if self._forming is not None and bar.timestamp < self._forming.timestamp:
            return "ignored"
        self._forming = bar
        return "set"

    def forming_bar(self) -> Bar | None:
        """The currently-forming (in-progress) bar, if any. For price references that
        legitimately need the live price — NEVER for features/indicators/signals."""
        if self._forming is None:
            return None
        if self._last_ts is not None and self._forming.timestamp <= self._last_ts:
            # the forming period has since closed and been appended; drop the stale slot
            return None
        return self._forming

    def latest_including_forming(self) -> Bar | None:
        """The forming bar if one is live, else the latest CLOSED bar. Price-reference use only."""
        return self.forming_bar() or self.latest()

    def chronological_bars(self) -> list[Bar]:
        """Bars in chronological order for parity checks and diagnostics."""
        return list(self._bars)

    def _ordered(self, buf: np.ndarray) -> np.ndarray:
        """Return buffer contents in chronological order (may be a view)."""
        if self._count < self._maxlen:
            return buf[: self._count]
        return np.concatenate((buf[self._head :], buf[: self._head]))

    def closes_array(self) -> np.ndarray:
        """Chronological close prices as a float64 array for Numba indicators."""
        return self._ordered(self._closes)

    def highs_array(self) -> np.ndarray:
        """Chronological high prices as a float64 array for Numba indicators."""
        return self._ordered(self._highs)

    def lows_array(self) -> np.ndarray:
        """Chronological low prices as a float64 array for Numba indicators."""
        return self._ordered(self._lows)

    def opens_array(self) -> np.ndarray:
        """Chronological open prices as a float64 array."""
        return self._ordered(self._opens)

    def volumes_array(self) -> np.ndarray:
        """Chronological volumes as a float64 array."""
        return self._ordered(self._volumes)

    def tail_closes(self, period: int) -> np.ndarray:
        """
        Last `period` close prices only — avoids copying the full window
        when indicators need a single rolling statistic on the live path.
        """
        full = self._ordered(self._closes)
        if len(full) <= period:
            return full
        return full[-period:]
