"""
On-call incident paging and dead-man heartbeat monitoring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from src.config import DB_PATH
from src.persistence import db as persistence

log = logging.getLogger(__name__)

HEARTBEAT_STATE_KEY = "last_successful_cycle_at"
DEFAULT_PAGE_COOLDOWN_SECONDS = 300.0
DEAD_MAN_CYCLE_MULTIPLIER = 2


class IncidentType(str, Enum):
    PRE_FLIGHT_RECON_LOCK = "PRE_FLIGHT_RECON_LOCK"
    DEGRADED_FEED_SUSTAINED = "DEGRADED_FEED_SUSTAINED"
    WAL_BACKLOG_CRITICAL = "WAL_BACKLOG_CRITICAL"
    HARD_CRITICAL_DEGRADE = "HARD_CRITICAL_DEGRADE"
    MAINTENANCE_JOB_FAILED = "MAINTENANCE_JOB_FAILED"
    DEAD_MAN_HEARTBEAT = "DEAD_MAN_HEARTBEAT"
    BOOT_BLOCKING_ERROR = "BOOT_BLOCKING_ERROR"


@dataclass(frozen=True)
class PageDispatchResult:
    delivered: bool
    channel: str
    status_code: int | None
    reason: str
    incident_type: str


_recent_pages: dict[str, float] = {}
_recent_pages_lock = threading.Lock()


def _blocking_webhook_post(
    request: urllib.request.Request,
    incident_key: str,
) -> PageDispatchResult:
    """Synchronous HTTP POST executed inside a thread executor to avoid blocking the event loop."""
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            status = int(response.status)
        return PageDispatchResult(
            delivered=200 <= status < 300,
            channel="webhook",
            status_code=status,
            reason="delivered" if 200 <= status < 300 else "non_2xx",
            incident_type=incident_key,
        )
    except urllib.error.HTTPError as exc:
        log.error("critical_page_http_error incident=%s code=%s", incident_key, exc.code)
        return PageDispatchResult(
            delivered=False,
            channel="webhook",
            status_code=int(exc.code),
            reason=str(exc.reason),
            incident_type=incident_key,
        )
    except urllib.error.URLError as exc:
        log.error("critical_page_url_error incident=%s error=%s", incident_key, exc.reason)
        return PageDispatchResult(
            delivered=False,
            channel="webhook",
            status_code=None,
            reason=f"url_error:{exc.reason}",
            incident_type=incident_key,
        )
    except TimeoutError as exc:
        log.error("critical_page_timeout incident=%s error=%s", incident_key, exc)
        return PageDispatchResult(
            delivered=False,
            channel="webhook",
            status_code=None,
            reason="timeout",
            incident_type=incident_key,
        )
    except OSError as exc:
        log.error("critical_page_connection_error incident=%s error=%s", incident_key, exc)
        return PageDispatchResult(
            delivered=False,
            channel="webhook",
            status_code=None,
            reason=f"connection_error:{exc}",
            incident_type=incident_key,
        )
    except Exception as exc:
        log.error("critical_page_failed incident=%s error=%s", incident_key, exc)
        return PageDispatchResult(
            delivered=False,
            channel="webhook",
            status_code=None,
            reason=str(exc),
            incident_type=incident_key,
        )


async def dispatch_critical_page(
    incident_type: str | IncidentType,
    message: str,
    metadata: Mapping[str, Any] | None = None,
    *,
    cooldown_seconds: float = DEFAULT_PAGE_COOLDOWN_SECONDS,
) -> PageDispatchResult:
    """
    Dispatch a critical incident page via webhook (PagerDuty/Opsgenie-compatible JSON).

    The blocking network I/O is offloaded to a thread executor so this coroutine
    never stalls the async trading loop regardless of network latency or timeouts.
    """
    incident_key = (
        incident_type.value
        if isinstance(incident_type, IncidentType)
        else str(incident_type)
    )
    now = time.monotonic()
    with _recent_pages_lock:
        last_sent = _recent_pages.get(incident_key, 0.0)
        if now - last_sent < max(float(cooldown_seconds), 0.0):
            return PageDispatchResult(
                delivered=False,
                channel="suppressed",
                status_code=None,
                reason="cooldown_active",
                incident_type=incident_key,
            )
        _recent_pages[incident_key] = now

    payload = {
        "incident_type": incident_key,
        "severity": "critical",
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": dict(metadata or {}),
        "source": "trading_bot_alerts",
    }
    persistence.log_system_event(
        "CRITICAL_PAGE",
        json.dumps(payload, separators=(",", ":")),
        severity="critical",
    )

    webhook_url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not webhook_url:
        log.critical("CRITICAL_PAGE %s: %s metadata=%s", incident_key, message, metadata)
        return PageDispatchResult(
            delivered=False,
            channel="log_only",
            status_code=None,
            reason="webhook_not_configured",
            incident_type=incident_key,
        )

    headers = {"Content-Type": "application/json"}
    token = os.getenv("ALERT_WEBHOOK_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        _blocking_webhook_post,
        request,
        incident_key,
    )


def record_successful_cycle_heartbeat(
    *,
    db_path: Any = DB_PATH,
    supabase_sync: Any | None = None,
    environment: str = "paper",
) -> str:
    """Persist local heartbeat timestamp and optionally ping Supabase."""
    now = datetime.now(timezone.utc)
    iso_ts = now.isoformat()
    # ADDITIVE, write-only telemetry (dormant): persist per-stream feed-health facts alongside the
    # heartbeat, in the same operational-state write, so a future external process can verify the
    # feed is live (deploy-runner G2). Fail-safe: never let this break the heartbeat.
    feed_health: dict | None = None
    try:
        from src.ingestor.feed_stream_health import build_feed_health_payload

        feed_health = build_feed_health_payload()
    except Exception as exc:  # pragma: no cover - telemetry must never break the cycle heartbeat
        log.warning("feed_health_payload_failed error=%s", exc)
    persistence.set_engine_heartbeat_timestamp(iso_ts, db_path=db_path, feed_health=feed_health)
    if supabase_sync is not None:
        try:
            supabase_sync.sync_heartbeat_ping(environment=environment, timestamp=iso_ts)
        except Exception as exc:
            log.warning("heartbeat_supabase_ping_failed error=%s", exc)
    return iso_ts


def heartbeat_age_seconds(*, db_path: Any = DB_PATH) -> float | None:
    last = persistence.get_engine_heartbeat_timestamp(db_path=db_path)
    if last is None:
        return None
    try:
        parsed = datetime.fromisoformat(last)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max((datetime.now(timezone.utc) - parsed).total_seconds(), 0.0)


class SystemHeartbeatMonitor:
    """
    Background dead-man switch: pages if no clean cycle completes within 2× cycle interval.
    """

    def __init__(
        self,
        *,
        cycle_interval_seconds: float,
        db_path: Any = DB_PATH,
        supabase_sync: Any | None = None,
        environment: str = "paper",
        check_interval_seconds: float = 30.0,
        none_stale_after_seconds: float | None = None,
    ) -> None:
        self.cycle_interval_seconds = max(float(cycle_interval_seconds), 1.0)
        self.db_path = db_path
        self.supabase_sync = supabase_sync
        self.environment = environment
        self.check_interval_seconds = max(float(check_interval_seconds), 1.0)
        # Grace period before treating a never-written heartbeat as stale.
        # Defaults to 3 full cycles so a fresh deploy doesn't page immediately.
        self._none_stale_after_seconds = (
            float(none_stale_after_seconds)
            if none_stale_after_seconds is not None
            else 3.0 * self.cycle_interval_seconds
        )
        self._started_at = time.monotonic()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="system-heartbeat-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def record_successful_cycle(self) -> str:
        return record_successful_cycle_heartbeat(
            db_path=self.db_path,
            supabase_sync=self.supabase_sync,
            environment=self.environment,
        )

    def evaluate_dead_man_once(self) -> bool:
        """Run a single dead-man evaluation (used by monitor loop and tests)."""
        if not self.is_dead_man_stale():
            return False
        age = heartbeat_age_seconds(db_path=self.db_path)
        threshold = self.cycle_interval_seconds * DEAD_MAN_CYCLE_MULTIPLIER
        age_desc = f"{age:.0f}s" if age is not None else "never written"
        asyncio.run(
            dispatch_critical_page(
                IncidentType.DEAD_MAN_HEARTBEAT,
                f"Engine heartbeat stale: {age_desc} (threshold {threshold:.0f}s)",
                {
                    "heartbeat_age_seconds": age,
                    "threshold_seconds": threshold,
                    "environment": self.environment,
                },
            )
        )
        return True

    def is_dead_man_stale(self) -> bool:
        """True when the heartbeat is stale or has never been written past the grace period."""
        age = heartbeat_age_seconds(db_path=self.db_path)
        if age is None:
            # No heartbeat written yet. Only stale once we're past the startup grace period.
            return time.monotonic() - self._started_at > self._none_stale_after_seconds
        threshold = self.cycle_interval_seconds * DEAD_MAN_CYCLE_MULTIPLIER
        return age > threshold

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self.check_interval_seconds):
            self.evaluate_dead_man_once()
