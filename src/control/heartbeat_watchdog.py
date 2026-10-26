"""Heartbeat watchdog + notification plug point (garage G1.5).

The main loop writes a heartbeat every cycle (persistence/heartbeat_store, via the
existing AsyncDBWriter). An INDEPENDENT async watchdog checks the latest beat's
age; if it is stale beyond the threshold WHILE THE MARKET IS OPEN, it forces the
block-new-entries safe state via the existing risk overlay (RiskEscalationLevel
.ENTRY_GATE_HALT) and fires a notification.

Notification ships as a Notifier PROTOCOL + a complete LogNotifier (structured log
+ durable alerts table). EmailNotifier (src/alerts/email_dispatcher.py, P3.1) now
implements the same protocol and is composed with LogNotifier via CompositeNotifier;
safety actions never depend on any notification channel.

Market-calendar aware: while the market is closed the loop is expected to be idle,
so a stale heartbeat does NOT trip (no false alarms overnight/weekends).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

import structlog

from src.engine.engine_preemption import RiskEscalationEngine, RiskEscalationLevel
from src.core.market_calendar import MarketSessionCalendar
from src.persistence.alert_store import persist_alert
from src.persistence.heartbeat_store import read_latest_heartbeat, read_latest_heartbeat_status

log = structlog.get_logger()


@runtime_checkable
class Notifier(Protocol):
    """Notification sink. The plug point for LogNotifier now, EmailNotifier later."""

    def notify(self, alert: dict) -> None: ...


class LogNotifier:
    """Complete notifier: emits a structured log line AND appends to the durable
    operator_alerts table. No email (operator decision)."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = db_path

    def notify(self, alert: dict) -> None:
        log.critical("operator_alert", **{k: v for k, v in alert.items() if k != "detail"},
                     detail=alert.get("detail", {}))
        persist_alert(alert, self._db_path)


@dataclass
class WatchdogResult:
    tripped: bool
    age_seconds: float | None
    market_open: bool
    reason: str = ""


class HeartbeatWatchdog:
    """Independent liveness watchdog for the main loop."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        escalation: RiskEscalationEngine,
        notifier: Notifier,
        stale_threshold_seconds: float = 120.0,
        component: str = "main_loop",
        calendar: MarketSessionCalendar | None = None,
        started_at: datetime | None = None,
        environment: str = "paper",
        auto_deescalation_enabled: bool = True,
        deescalation_fresh_streak: int = 10,
    ) -> None:
        self._db_path = db_path
        self._escalation = escalation
        self._notifier = notifier
        self._threshold = float(stale_threshold_seconds)
        self._component = component
        self._calendar = calendar or MarketSessionCalendar()
        # R2 (INCIDENT-20260722 FINDING-7): bounded auto-de-escalation of a watchdog-commanded
        # ENTRY_GATE_HALT after a sustained fresh streak. OFF in live regardless of the flag
        # (live keeps latch-until-human until the unfreeze review extends it).
        self._allow_deesc = bool(auto_deescalation_enabled) and str(environment).lower() != "live"
        self._deesc_streak_required = int(deescalation_fresh_streak)
        self._fresh_streak = 0
        # FINDING-4 (INCIDENT-20260722): the heartbeat is persisted and survives a process restart,
        # so a fresh process would otherwise page "stale: <age of the PRIOR process's heartbeat>".
        # Start the paging clock from process start (grace == threshold) and log the inherited age once.
        self._started_at = started_at or datetime.now(timezone.utc)
        self._logged_prior_age = False

    def check(self, now: datetime | None = None) -> WatchdogResult:
        """One watchdog evaluation. Trips block-new-entries + notifies iff the
        heartbeat is stale beyond the threshold AND the market is open."""
        now = now or datetime.now(timezone.utc)
        market_open = self._calendar.is_within_rth(now)
        latest = read_latest_heartbeat(self._component, self._db_path)

        if not market_open:
            # market closed: the loop is idle by design -> never a false alarm
            return WatchdogResult(False, None, market_open=False, reason="market_closed")

        if latest is None:
            age = None
        else:
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=timezone.utc)
            age = (now - latest).total_seconds()

        # FINDING-4: log the inherited (prior-process) heartbeat age ONCE as INFO context.
        if not self._logged_prior_age:
            self._logged_prior_age = True
            log.info("heartbeat_watchdog_start", component=self._component,
                     prior_heartbeat_age_seconds=age, grace_seconds=self._threshold)

        # FINDING-4: grace window. For the first `threshold` seconds after THIS process started, do not
        # page/escalate on a stale heartbeat -- it is the predecessor's, and a healthy new process will
        # have emitted its own by the time the grace elapses. A genuinely dead new process still trips
        # after the grace. Guarded `0 <= elapsed` so an injected past `now` (tests) never grace-passes.
        elapsed_since_start = (now - self._started_at).total_seconds()
        if 0.0 <= elapsed_since_start < self._threshold:
            return WatchdogResult(False, age, market_open=True, reason="startup_grace")

        if age is None or age > self._threshold:
            self._fresh_streak = 0                       # R2: any stale reading resets the streak
            self._escalation.transition(RiskEscalationLevel.ENTRY_GATE_HALT,
                                        commanded_by="heartbeat_watchdog")
            self._notifier.notify({
                "kind": "heartbeat_stale", "severity": "critical",
                # F3: name the ACTUAL component. The old template hardcoded "main-loop" for every
                # component, so a soak trip read "main-loop heartbeat stale (soak)" -- self-contradictory.
                "message": f"{self._component} heartbeat stale",
                "detail": {"age_seconds": age, "threshold_seconds": self._threshold,
                           "component": self._component},
            })
            return WatchdogResult(True, age, market_open=True, reason="stale_heartbeat")

        # R3 (INCIDENT-20260722): the beat is FRESH. Read the status payload (written by the DECOUPLED
        # heartbeat emitter) to distinguish HALTED-BY-DESIGN from a DEAD ENGINE. Status-less beats
        # (legacy write_heartbeat_sync) carry no payload -> fall through to the healthy path unchanged.
        status = read_latest_heartbeat_status(self._component, self._db_path)
        status_dict = status[1] if status else {}
        cycle_age = None
        lct_raw = status_dict.get("last_cycle_ts")
        if lct_raw:
            try:
                lct = datetime.fromisoformat(str(lct_raw))
                if lct.tzinfo is None:
                    lct = lct.replace(tzinfo=timezone.utc)
                cycle_age = (now - lct).total_seconds()
            except (ValueError, TypeError):
                cycle_age = None

        def _resolve() -> None:
            # F5: tell the notifier the condition is clear so a ThrottledNotifier sends ONE "recovered".
            resolve = getattr(self._notifier, "resolve", None)
            if callable(resolve):
                try:
                    resolve(component=self._component, kind="heartbeat_stale")
                except Exception:
                    log.warning("heartbeat_resolve_notify_failed", component=self._component, exc_info=True)

        # (a) DEAD ENGINE: the emitter is alive (fresh beat) but the CONSUMPTION loop's last_cycle_ts is
        # stale -- a heartbeat that can't see a dead engine is worse than none. Trip at CRITICAL.
        if cycle_age is not None and cycle_age > self._threshold:
            self._fresh_streak = 0                       # R2: a trip is not a healthy reading
            self._escalation.transition(RiskEscalationLevel.ENTRY_GATE_HALT,
                                        commanded_by="heartbeat_watchdog")
            self._notifier.notify({
                "kind": "heartbeat_stale", "severity": "critical",
                "message": f"{self._component} consumption stalled (heartbeat task alive)",
                "detail": {"cycle_age_seconds": cycle_age, "threshold_seconds": self._threshold,
                           "component": self._component}})
            return WatchdogResult(True, age, market_open=True, reason="engine_cycle_stale")

        # (b) HALTED-BY-DESIGN: fresh beat + current cycle, but the gate is BLOCKED. Alive, not dead --
        # a WARNING with the reason inline, NOT a heartbeat_stale CRITICAL, and NO escalation. A gated
        # (halted-by-design) check is NOT a clean healthy reading, so it does not advance the R2
        # de-escalation streak -- auto-recovery only accrues on sustained TRULY-healthy checks.
        if str(status_dict.get("gate", "")).upper() == "BLOCKED":
            self._fresh_streak = 0
            _resolve()
            self._notifier.notify({
                "kind": "gate_blocked", "severity": "warning",
                "message": f"{self._component} halted by design ({status_dict.get('gate_reason', '')})",
                "detail": {"gate_reason": status_dict.get("gate_reason", ""),
                           "escalation_level": status_dict.get("escalation_level"),
                           "component": self._component}})
            return WatchdogResult(False, age, market_open=True, reason="gate_blocked")

        # (c) Healthy (gate OPEN + cycle current). F5 resolve + R2 (FINDING-7) bounded auto-de-escalation:
        # a fresh reading advances the streak; at the required length de-escalate ONLY a watchdog-commanded
        # ENTRY_GATE_HALT with no portfolio-liquidation / shutdown semantics (never operator/kill /
        # STRATEGY_LIQUIDATE / GLOBAL_FLATTEN). Disabled entirely in live.
        _resolve()
        self._fresh_streak += 1
        snapshot_fn = getattr(self._escalation, "snapshot", None)   # NullEscalation has none -> skip
        if self._allow_deesc and callable(snapshot_fn) and self._fresh_streak >= self._deesc_streak_required:
            snap = snapshot_fn()
            if (snap.level == RiskEscalationLevel.ENTRY_GATE_HALT
                    and snap.commanded_by == "heartbeat_watchdog"
                    and not snap.portfolio_liquidation_commanded
                    and not snap.engine_shutdown_latched):
                self._escalation.transition(RiskEscalationLevel.NOMINAL,
                                            commanded_by="heartbeat_watchdog_recovery")
                log.critical("heartbeat_watchdog_deescalation",
                             prior_level=RiskEscalationLevel.ENTRY_GATE_HALT.value,
                             new_level=RiskEscalationLevel.NOMINAL.value,
                             fresh_streak=self._fresh_streak, component=self._component,
                             commanded_by="heartbeat_watchdog_recovery")
                self._fresh_streak = 0
        return WatchdogResult(False, age, market_open=True, reason="healthy")

    async def run(self, *, poll_interval_seconds: float = 30.0, iterations: int | None = None):
        """Async watchdog loop (independent task). ``iterations`` bounds it for
        tests; None runs until cancelled."""
        import asyncio
        count = 0
        while iterations is None or count < iterations:
            self.check()
            count += 1
            if iterations is not None and count >= iterations:
                break
            await asyncio.sleep(poll_interval_seconds)
