"""
Authoritative market-data websocket feed health registry.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

FEED_RECONNECT_DEGRADED_SECONDS = 300.0

_KNOWN_STREAM_TYPES = ("stock", "crypto")


class FeedStreamMode(str, Enum):
    HEALTHY = "HEALTHY"
    RECONNECTING = "RECONNECTING"
    DEGRADED_FEED = "DEGRADED_FEED"


@dataclass
class FeedStreamHealthSnapshot:
    mode: FeedStreamMode
    disconnected_seconds: float
    last_bar_monotonic: float | None


class FeedStreamHealthRegistry:
    """Thread-safe feed connectivity state tracked per stream type."""

    __slots__ = ("_lock", "_per_stream")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # state per stream type: (mode, disconnect_since_monotonic, last_bar_monotonic)
        self._per_stream: dict[str, list] = {
            st: [FeedStreamMode.HEALTHY, None, None] for st in _KNOWN_STREAM_TYPES
        }

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def note_stream_bar(self, stream_type: str) -> None:
        """Mark a bar received on the given stream ('stock' or 'crypto')."""
        now = time.monotonic()
        with self._lock:
            entry = self._per_stream.setdefault(
                stream_type, [FeedStreamMode.HEALTHY, None, None]
            )
            entry[0] = FeedStreamMode.HEALTHY
            entry[1] = None  # clear disconnect timer
            entry[2] = now

    def note_disconnect(self, stream_type: str | None = None) -> None:
        """Mark disconnect. None → affects all known stream types (supervisor-level)."""
        now = time.monotonic()
        keys = _KNOWN_STREAM_TYPES if stream_type is None else (stream_type,)
        with self._lock:
            for key in keys:
                entry = self._per_stream.setdefault(
                    key, [FeedStreamMode.HEALTHY, None, None]
                )
                if entry[1] is None:
                    entry[1] = now
                entry[0] = FeedStreamMode.RECONNECTING
                self._refresh_locked(entry)

    def note_reconnect_attempt(self, stream_type: str | None = None) -> None:
        """Mark reconnect attempt. None → affects all known stream types."""
        now = time.monotonic()
        keys = _KNOWN_STREAM_TYPES if stream_type is None else (stream_type,)
        with self._lock:
            for key in keys:
                entry = self._per_stream.setdefault(
                    key, [FeedStreamMode.HEALTHY, None, None]
                )
                if entry[1] is None:
                    entry[1] = now
                entry[0] = FeedStreamMode.RECONNECTING
                self._refresh_locked(entry)

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def is_degraded_feed(self, stream_type: str | None = None) -> bool:
        """True if the named stream (or any stream if None) is DEGRADED_FEED."""
        return self.mode(stream_type) == FeedStreamMode.DEGRADED_FEED

    def mode(self, stream_type: str | None = None) -> FeedStreamMode:
        """Return mode for the named stream, or worst-case across all streams."""
        keys = _KNOWN_STREAM_TYPES if stream_type is None else (stream_type,)
        with self._lock:
            worst = FeedStreamMode.HEALTHY
            for key in keys:
                entry = self._per_stream.get(key, [FeedStreamMode.HEALTHY, None, None])
                self._refresh_locked(entry)
                m = entry[0]
                if m == FeedStreamMode.DEGRADED_FEED:
                    return FeedStreamMode.DEGRADED_FEED
                if m == FeedStreamMode.RECONNECTING:
                    worst = FeedStreamMode.RECONNECTING
            return worst

    def snapshot(self, stream_type: str | None = None) -> FeedStreamHealthSnapshot:
        """Snapshot for the named stream, or worst-case across all streams."""
        keys = _KNOWN_STREAM_TYPES if stream_type is None else (stream_type,)
        now = time.monotonic()
        with self._lock:
            worst_mode = FeedStreamMode.HEALTHY
            max_disconnected = 0.0
            earliest_bar: float | None = None
            for key in keys:
                entry = self._per_stream.get(key, [FeedStreamMode.HEALTHY, None, None])
                self._refresh_locked(entry)
                m, disc_since, last_bar = entry
                if m == FeedStreamMode.DEGRADED_FEED:
                    worst_mode = FeedStreamMode.DEGRADED_FEED
                elif m == FeedStreamMode.RECONNECTING and worst_mode != FeedStreamMode.DEGRADED_FEED:
                    worst_mode = FeedStreamMode.RECONNECTING
                if disc_since is not None:
                    max_disconnected = max(max_disconnected, max(0.0, now - disc_since))
                if last_bar is not None:
                    earliest_bar = last_bar if earliest_bar is None else min(earliest_bar, last_bar)
            return FeedStreamHealthSnapshot(
                mode=worst_mode,
                disconnected_seconds=max_disconnected,
                last_bar_monotonic=earliest_bar,
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _refresh_locked(entry: list) -> None:
        """Re-evaluate RECONNECTING → DEGRADED_FEED transition in-place."""
        disc_since = entry[1]
        if disc_since is None:
            if entry[0] != FeedStreamMode.HEALTHY:
                entry[0] = FeedStreamMode.HEALTHY
            return
        elapsed = time.monotonic() - disc_since
        if elapsed >= FEED_RECONNECT_DEGRADED_SECONDS:
            entry[0] = FeedStreamMode.DEGRADED_FEED
        else:
            entry[0] = FeedStreamMode.RECONNECTING


_registry: FeedStreamHealthRegistry | None = None
_registry_lock = threading.Lock()


def get_feed_stream_health_registry() -> FeedStreamHealthRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = FeedStreamHealthRegistry()
        return _registry


def build_feed_health_payload() -> dict[str, dict]:
    """Snapshot each tracked stream's health as JSON-able facts for the persisted operational-state
    store, so an EXTERNAL process can verify the feed is live (future deploy-runner G2). WRITE-ONLY
    telemetry -- no engine decision consumes it (dormant). `last_bar_utc` is derived from the
    in-process monotonic clock, so this MUST run in the engine process (it does, once per cycle).
    'stock' covers the QQQ/SPY feed, 'crypto' covers BTC -- the granularity the registry tracks."""
    reg = get_feed_stream_health_registry()
    now_mono = time.monotonic()
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    payload: dict[str, dict] = {}
    for stream in _KNOWN_STREAM_TYPES:
        snap = reg.snapshot(stream)
        if snap.last_bar_monotonic is not None:
            staleness = max(0.0, now_mono - snap.last_bar_monotonic)
            last_bar_utc = (now_utc - timedelta(seconds=staleness)).isoformat()
        else:
            staleness = None
            last_bar_utc = None
        payload[stream] = {
            "stream": stream,
            "mode": snap.mode.value,
            "last_bar_utc": last_bar_utc,               # wall-clock time of the most recent bar
            "bar_staleness_seconds": staleness,          # seconds since that bar (None if none yet)
            "disconnected_seconds": snap.disconnected_seconds,  # reconnect-gap (0 if healthy)
            "last_write_utc": now_iso,                   # when THIS row was written (writer-alive proof)
        }
    return payload
