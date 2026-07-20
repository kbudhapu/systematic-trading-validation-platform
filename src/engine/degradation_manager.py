"""
Operational degradation manager — soft entry blocks and hard safety flatten modes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from src.config import DB_PATH
from src.engine.slo_monitor import DataIntegrityVerdict, IntegritySeverity
from src.models import SignalAction
from src.persistence.governance_state_store import (
    load_governance_state,
    persist_governance_state,
)

SOFT_RECOVERY_STREAK = 3
# R2.3a — HARD_CRITICAL_DEGRADE recovery. A safety state MUST have a proven exit, evaluated on
# the SAME signal/tick as engagement. HARD engages the instant the SLO verdict is a HARD_BREACH;
# it now recovers when the SLO verdict is OK for HARD_RECOVERY_STREAK consecutive evaluations.
# 6 (= 2x SOFT's 3): HARD force-flattens and ignores every signal, so it must re-prove health
# for materially longer than SOFT before stepping DOWN — and it steps down to SOFT (not straight
# to NORMAL) so health is re-proven a second time (a further SOFT_RECOVERY_STREAK) before any
# entry is allowed. Total OK verdicts HARD -> NORMAL = HARD_RECOVERY_STREAK + SOFT_RECOVERY_STREAK.
HARD_RECOVERY_STREAK = 6


class OperationalMode(str, Enum):
    NORMAL = "NORMAL"
    SOFT_DEGRADE = "SOFT_DEGRADE"
    RECON_DEGRADED_MANAGE = "RECON_DEGRADED_MANAGE"
    HARD_CRITICAL_DEGRADE = "HARD_CRITICAL_DEGRADE"


@dataclass(frozen=True)
class DegradationState:
    mode: OperationalMode
    block_new_entries: bool
    ignore_model_signals: bool
    force_emergency_flatten: bool
    bypass_standard_timers: bool
    allow_exit_tracking: bool
    reason: str
    activated_at: str


@dataclass
class DegradationManager:
    """Central operational state transitions driven by SLO integrity breaches."""

    db_path: Path = DB_PATH
    # R2.4a: a ThrottledNotifier-like sink (.notify(alert) + .resolve(component, kind)) so entering
    # HARD_CRITICAL_DEGRADE pages the phone IMMEDIATELY and clearing it sends exactly one RECOVERED
    # message. Optional; when absent we fall back to the webhook page + durable log. A six-day
    # silent latch is the failure mode this whole component now guards against.
    notifier: Any = field(default=None, repr=False)
    _mode: OperationalMode = OperationalMode.NORMAL
    _reason: str = ""
    _activated_at: str | None = None
    _recovery_streak: int = 0
    _flatten_latched: bool = False
    _hydrated: bool = field(default=False, repr=False)

    _DEGRADE_COMPONENT = "degradation_manager"
    _DEGRADE_KIND = "hard_critical_degrade"

    def hydrate_from_vault(self) -> DegradationState:
        if self._hydrated:
            return self.current_state()
        persisted = load_governance_state(self.db_path)
        if persisted is not None:
            try:
                self._mode = OperationalMode(persisted.degradation_mode)
            except ValueError:
                self._mode = OperationalMode.NORMAL
            self._reason = persisted.trigger_reason
            self._activated_at = persisted.entered_at
            self._flatten_latched = persisted.flatten_latched
            # R2.3b (ruling i): rehydrate the MODE (a genuine degrade must survive a restart —
            # else restarting silently overrides the safety system) but RESET the recovery streak
            # to zero. Health must be RE-EARNED after a restart; do not inherit stale credit.
            self._recovery_streak = 0
        self._hydrated = True
        return self.current_state()

    def current_state(self) -> DegradationState:
        return self._build_state(self._mode, self._reason)

    def evaluate_from_slo(self, verdict: DataIntegrityVerdict) -> DegradationState:
        if verdict.severity == IntegritySeverity.HARD_BREACH:
            return self.apply_hard_critical_degrade(
                "|".join(verdict.reasons) or "hard_slo_breach"
            )

        # R2.3a — HARD_CRITICAL recovery, driven by the SAME signal (the SLO verdict) on the SAME
        # tick as engagement. This branch is checked BEFORE the SOFT_BREACH handler so that a
        # still-degraded (SOFT) verdict resets the streak rather than sneaking a downgrade. A HARD
        # leg that recovers steps DOWN to SOFT (re-prove health again) — it does not jump to NORMAL.
        # (Replaces the old `return self.current_state()` dead-end that made HARD unrecoverable and
        # latched the engine for six days.)
        if self._mode == OperationalMode.HARD_CRITICAL_DEGRADE:
            # S1: HARD must be RELEASED BY THE SAME SIGNAL CLASS THAT ENGAGED IT (R2.7). HARD is
            # engaged only by a HARD_BREACH (handled at the top of this method, which resets the
            # streak). A SOFT verdict here means the genuine live-data criticals (nbbo/ingest/
            # freshness) have cleared but some lower-severity condition remains — it must NOT reset
            # HARD's recovery, or a persistent SOFT signal would trap the engine in force-flatten
            # forever. Only a fully-OK verdict earns streak credit; a SOFT verdict holds progress
            # steady; a HARD verdict (top of method) resets it.
            if verdict.severity == IntegritySeverity.OK:
                self._recovery_streak += 1
            if self._recovery_streak >= HARD_RECOVERY_STREAK:
                return self._downgrade_hard_to_soft("hard_critical_recovered_to_soft")
            self._persist_state()
            return self.current_state()

        # V1: SOFT_DEGRADE recovery -- apply T4 clause (1) to SOFT exactly as S1 did for HARD. Checked
        # BEFORE the SOFT_BREACH engage handler so a SOFT verdict while ALREADY in SOFT does NOT reset
        # the streak. The old code reset on ANY non-OK (via apply_soft_degrade below), so a PERSISTENT
        # SOFT condition reset the recovery on the very condition that engaged it -> SOFT permanent by
        # construction, and SOFT sets block_new_entries=True -> the soft trap. A state is reset ONLY by
        # a breach AT OR ABOVE its own severity: HARD escalates (top of method), a SOFT verdict HOLDS
        # the streak, an OK verdict (all SOFT reasons cleared) accrues it toward NORMAL.
        if self._mode == OperationalMode.SOFT_DEGRADE:
            if verdict.severity == IntegritySeverity.OK:
                self._recovery_streak += 1
            if self._recovery_streak >= SOFT_RECOVERY_STREAK:
                return self.reset_to_normal("slo_recovered")
            self._persist_state()
            return self.current_state()

        if verdict.severity == IntegritySeverity.SOFT_BREACH:
            return self.apply_soft_degrade("|".join(verdict.reasons) or "soft_slo_breach")

        if self._mode == OperationalMode.RECON_DEGRADED_MANAGE:
            return self.current_state()

        self._recovery_streak = 0
        self._persist_state()
        return self.current_state()

    def apply_soft_degrade(self, reason: str) -> DegradationState:
        if self._mode == OperationalMode.HARD_CRITICAL_DEGRADE:
            return self.current_state()
        # V1: only a FRESH engage (from NORMAL) resets the recovery streak. RE-ASSERTING SOFT (e.g. a
        # persistent degraded feed every cycle, via the orchestrator's feed-quality path) must NOT
        # reset it, or the streak can never accrue and SOFT becomes permanent -- the soft trap.
        entering = self._mode != OperationalMode.SOFT_DEGRADE
        self._mode = OperationalMode.SOFT_DEGRADE
        self._reason = reason
        self._flatten_latched = False
        if entering:
            self._activated_at = datetime.now(timezone.utc).isoformat()
            self._recovery_streak = 0
        self._persist_state()
        return self._build_state(self._mode, reason)

    def apply_recon_degraded_manage(self, reason: str) -> DegradationState:
        if self._mode == OperationalMode.HARD_CRITICAL_DEGRADE:
            return self.current_state()
        self._mode = OperationalMode.RECON_DEGRADED_MANAGE
        self._reason = reason
        self._activated_at = datetime.now(timezone.utc).isoformat()
        self._recovery_streak = 0
        self._flatten_latched = False
        self._persist_state()
        return self._build_state(self._mode, reason)

    def apply_hard_critical_degrade(self, reason: str) -> DegradationState:
        entering = self._mode != OperationalMode.HARD_CRITICAL_DEGRADE
        self._mode = OperationalMode.HARD_CRITICAL_DEGRADE
        self._reason = reason
        if entering:
            # AE3.3: persist the TRUE entry time ONCE, on the transition. Re-asserting HARD every
            # cycle (a persistent breach) must NOT re-stamp entered_at, or the alert and the DB both
            # report age ~0 while it has actually been hours -- the "just entered" lie.
            self._activated_at = datetime.now(timezone.utc).isoformat()
        self._recovery_streak = 0
        self._flatten_latched = True
        self._persist_state()
        if entering:
            self._dispatch_degradation_entered(reason)
        return self._build_state(self._mode, reason)

    def _dispatch_degradation_entered(self, reason: str) -> None:
        """R2.4a — page URGENT immediately on entering HARD_CRITICAL_DEGRADE. Prefers the injected
        ThrottledNotifier (dedup + backoff, reaches the phone); always also fires the webhook page +
        durable log so the incident is recorded even if ntfy is unconfigured."""
        if self.notifier is not None:
            try:
                self.notifier.notify({
                    "kind": self._DEGRADE_KIND,
                    "severity": "urgent",
                    "message": f"Engine entered HARD_CRITICAL_DEGRADE: {reason}",
                    "detail": {"component": self._DEGRADE_COMPONENT, "reason": reason,
                               "mode": self._mode.value},
                })
            except Exception as exc:  # a page must never raise into the trading loop
                import logging as _logging
                _logging.getLogger(__name__).warning("degrade_notify_failed: %s", exc)
        import asyncio
        from src.control.alerts import IncidentType, dispatch_critical_page
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                dispatch_critical_page(
                    IncidentType.HARD_CRITICAL_DEGRADE,
                    f"Engine entered HARD_CRITICAL_DEGRADE: {reason}",
                    {"reason": reason, "mode": self._mode.value},
                )
            )
        except RuntimeError:
            import logging as _logging
            _logging.getLogger(__name__).critical(
                "HARD_CRITICAL_DEGRADE (no loop, webhook suppressed): %s", reason
            )

    def _dispatch_degradation_recovered(self, reason: str) -> None:
        """R2.4a / AE3.2 — a POSITIVE 'recovered' message on EVERY downgrade (HARD->SOFT and
        SOFT->NORMAL), unconditionally. Entry-only alerting is the July-7 position: without a recovery
        message the operator cannot tell 'still broken' from 'fixed hours ago' except by SSH. resolve()
        re-arms the ThrottledNotifier (next occurrence pages immediately); notify() is the actual
        recovered push."""
        if self.notifier is not None:
            try:
                self.notifier.resolve(component=self._DEGRADE_COMPONENT, kind=self._DEGRADE_KIND)
                self.notifier.notify({
                    "kind": self._DEGRADE_KIND + "_recovered",
                    "severity": "info",
                    "message": f"Engine degradation recovered -> {self._mode.value}: {reason}",
                    "detail": {"component": self._DEGRADE_COMPONENT, "reason": reason,
                               "mode": self._mode.value},
                })
            except Exception as exc:  # a page must never raise into the trading loop
                import logging as _logging
                _logging.getLogger(__name__).warning("degrade_recovered_notify_failed: %s", exc)
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "degradation_recovered -> %s (%s)", self._mode.value, reason
        )

    def _downgrade_hard_to_soft(self, reason: str) -> DegradationState:
        """R2.3a — staged recovery: HARD_CRITICAL -> SOFT_DEGRADE once the SLO verdict has been OK
        for HARD_RECOVERY_STREAK consecutive evaluations. SOFT still blocks new entries and stops
        force-flattening, so the engine re-proves health (a further SOFT_RECOVERY_STREAK) before
        returning to NORMAL and trading again."""
        self._mode = OperationalMode.SOFT_DEGRADE
        self._reason = reason
        self._activated_at = datetime.now(timezone.utc).isoformat()
        self._recovery_streak = 0
        self._flatten_latched = False
        self._persist_state()
        self._dispatch_degradation_recovered(reason)
        return self._build_state(self._mode, reason)

    def reset_to_normal(self, reason: str = "manual_reset") -> DegradationState:
        was_degraded = self._mode != OperationalMode.NORMAL
        self._mode = OperationalMode.NORMAL
        self._reason = reason
        self._activated_at = None
        self._recovery_streak = 0
        self._flatten_latched = False
        self._persist_state()
        if was_degraded:
            # AE3.2: SOFT -> NORMAL is a recovery too. Announce it, or the operator only ever hears
            # the engine break and never hears it heal.
            self._dispatch_degradation_recovered(reason)
        return self._build_state(self._mode, reason)

    def should_block_entry_action(self, action: SignalAction | None) -> bool:
        state = self.current_state()
        if not state.block_new_entries or action is None:
            return False
        return action in (SignalAction.LONG, SignalAction.SHORT)

    def should_ignore_model_signal(self) -> bool:
        return self.current_state().ignore_model_signals

    def consume_flatten_request(self) -> bool:
        if not self._flatten_latched:
            return False
        self._flatten_latched = False
        self._persist_state()
        return True

    def override_routing_params(self, params: dict[str, Any]) -> dict[str, Any]:
        state = self.current_state()
        if state.mode == OperationalMode.NORMAL:
            return params
        adjusted = dict(params)
        adjusted["operational_mode"] = state.mode.value
        adjusted["degradation_reason"] = state.reason
        if state.bypass_standard_timers:
            adjusted["bypass_standard_timers"] = True
            adjusted["poll_interval_seconds"] = 0
        if state.block_new_entries:
            adjusted["block_new_entries"] = True
        return adjusted

    def _persist_state(self) -> None:
        persist_governance_state(
            degradation_mode=self._mode.value,
            trigger_reason=self._reason,
            entered_at=self._activated_at,
            flatten_latched=self._flatten_latched,
            recovery_streak=self._recovery_streak,
            db_path=self.db_path,
        )
        self._hydrated = True

    def _build_state(self, mode: OperationalMode, reason: str) -> DegradationState:
        if mode == OperationalMode.SOFT_DEGRADE:
            return DegradationState(
                mode=mode,
                block_new_entries=True,
                ignore_model_signals=False,
                force_emergency_flatten=False,
                bypass_standard_timers=False,
                allow_exit_tracking=True,
                reason=reason,
                activated_at=self._activated_at or datetime.now(timezone.utc).isoformat(),
            )
        if mode == OperationalMode.RECON_DEGRADED_MANAGE:
            return DegradationState(
                mode=mode,
                block_new_entries=True,
                ignore_model_signals=False,
                force_emergency_flatten=False,
                bypass_standard_timers=False,
                allow_exit_tracking=True,
                reason=reason,
                activated_at=self._activated_at or datetime.now(timezone.utc).isoformat(),
            )
        if mode == OperationalMode.HARD_CRITICAL_DEGRADE:
            return DegradationState(
                mode=mode,
                block_new_entries=True,
                ignore_model_signals=True,
                force_emergency_flatten=True,
                bypass_standard_timers=True,
                allow_exit_tracking=False,
                reason=reason,
                activated_at=self._activated_at or datetime.now(timezone.utc).isoformat(),
            )
        return DegradationState(
            mode=OperationalMode.NORMAL,
            block_new_entries=False,
            ignore_model_signals=False,
            force_emergency_flatten=False,
            bypass_standard_timers=False,
            allow_exit_tracking=True,
            reason=reason,
            activated_at=self._activated_at or "",
        )
