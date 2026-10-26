"""
Control-plane supervisor — systemd-style lifecycle, runbooks, and alert dispatch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping

from src.persistence import db as persistence

log = logging.getLogger(__name__)

JOB_PRE_OPEN = "pre_open_readiness"
JOB_POST_CLOSE = "post_close_reconciliation"
JOB_WEEKEND_TUNER = "weekend_parameter_tuner"
JOB_WEEKLY_POLICY = "weekly_policy_brain"

RunbookWorker = Callable[[], Awaitable[dict[str, Any]]]
PayloadValidator = Callable[[dict[str, Any]], tuple[bool, str]]


class SupervisorState(str, Enum):
    INACTIVE = "inactive"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    FAILED = "failed"


class CronLifecycleStage(str, Enum):
    IDLE = "idle"
    SCHEDULED = "scheduled"
    DISPATCHED = "dispatched"
    EXECUTING = "executing"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    ALERTED = "alerted"


class RunbookStep(str, Enum):
    PRE_OPEN_HYDRATION = "PRE_OPEN_HYDRATION"
    POST_CLOSE_RECONCILIATION = "POST_CLOSE_RECONCILIATION"
    WEEKEND_PARAMETER_TUNER = "WEEKEND_PARAMETER_TUNER"
    WEEKLY_POLICY_SWAP = "WEEKLY_POLICY_SWAP"


RUNBOOK_JOB_ID: dict[RunbookStep, str] = {
    RunbookStep.PRE_OPEN_HYDRATION: JOB_PRE_OPEN,
    RunbookStep.POST_CLOSE_RECONCILIATION: JOB_POST_CLOSE,
    RunbookStep.WEEKEND_PARAMETER_TUNER: JOB_WEEKEND_TUNER,
    RunbookStep.WEEKLY_POLICY_SWAP: JOB_WEEKLY_POLICY,
}

RUNBOOK_RESTORE_ON_FAILURE: dict[RunbookStep, bool] = {
    RunbookStep.PRE_OPEN_HYDRATION: True,
    RunbookStep.POST_CLOSE_RECONCILIATION: False,
    RunbookStep.WEEKEND_PARAMETER_TUNER: True,
    RunbookStep.WEEKLY_POLICY_SWAP: False,
}


@dataclass(frozen=True)
class AlertDispatchResult:
    delivered: bool
    channel: str
    status_code: int | None
    reason: str


@dataclass(frozen=True)
class RunbookExecutionResult:
    step: RunbookStep
    job_id: str
    stage: CronLifecycleStage
    success: bool
    validation_passed: bool
    payload: dict[str, Any]
    error: str | None
    hooks_executed: tuple[str, ...]


@dataclass
class RunbookContext:
    step: RunbookStep
    job_id: str
    supervisor_state: SupervisorState
    lifecycle_stage: CronLifecycleStage = CronLifecycleStage.IDLE
    started_at: str | None = None
    hooks_executed: list[str] = field(default_factory=list)


@dataclass
class ControlPlaneSupervisor:
    """Operational supervisor harness with explicit state transitions."""

    vault_path: Any
    workers: dict[RunbookStep, RunbookWorker] = field(default_factory=dict)
    validators: dict[RunbookStep, PayloadValidator] = field(default_factory=dict)
    pre_hooks: dict[RunbookStep, tuple[Callable[[RunbookContext], None], ...]] = field(
        default_factory=dict
    )
    post_hooks: dict[RunbookStep, tuple[Callable[[RunbookContext, dict[str, Any]], None], ...]] = field(
        default_factory=dict
    )
    restore_on_failure: Callable[[], bool] | None = None
    on_block_consumption: Callable[[str], None] | None = None
    on_release_consumption: Callable[[], None] | None = None
    pre_flight_engine: Any | None = None
    _state: SupervisorState = SupervisorState.INACTIVE
    _active_runbook: RunbookStep | None = None
    _lifecycle_stage: CronLifecycleStage = CronLifecycleStage.IDLE
    _pre_flight_result: Any | None = None

    @property
    def state(self) -> SupervisorState:
        return self._state

    @property
    def lifecycle_stage(self) -> CronLifecycleStage:
        return self._lifecycle_stage

    @property
    def pre_flight_result(self) -> Any | None:
        return self._pre_flight_result

    async def execute_pre_flight_broker_reconciliation(self) -> Any | None:
        """
        Reconcile broker positions and open orders against local runtime snapshots.

        Returns None when no pre-flight engine is configured.
        """
        if self.pre_flight_engine is None:
            return None
        result = await self.pre_flight_engine.execute()
        self._pre_flight_result = result
        event_payload = {
            "success": result.success,
            "recovered": result.recovered,
            "latched_soft_degrade": result.latched_soft_degrade,
            "delta_count": len(result.deltas),
            "journal_event_id": result.journal_event_id,
            "failure_reason": result.failure_reason,
            **result.metadata,
        }
        persistence.log_system_event(
            event_type="STATE_RECON_RECOVERY" if result.recovered else "PRE_FLIGHT_RECON",
            severity="critical" if result.latched_soft_degrade else "info",
            message=json.dumps(event_payload, separators=(",", ":")),
        )
        if result.latched_soft_degrade:
            if self.on_block_consumption:
                self.on_block_consumption(result.failure_reason)
            log.critical(
                "pre_flight_recon_latched reason=%s",
                result.failure_reason,
            )
        return result

    def transition(self, target: SupervisorState, *, reason: str = "") -> None:
        prior = self._state
        allowed = _ALLOWED_SUPERVISOR_TRANSITIONS.get(prior, frozenset())
        if target != prior and target not in allowed:
            raise RuntimeError(
                f"illegal supervisor transition {prior.value} -> {target.value}: {reason}"
            )
        self._state = target
        log.info(
            "supervisor_transition from=%s to=%s reason=%s",
            prior.value,
            target.value,
            reason,
        )

    async def start(self, *, run_pre_flight: bool = True) -> bool:
        self.transition(SupervisorState.STARTING, reason="daemon_boot")
        if run_pre_flight and self.pre_flight_engine is not None:
            result = await self.execute_pre_flight_broker_reconciliation()
            if result is not None and result.latched_soft_degrade:
                self.transition(SupervisorState.FAILED, reason=result.failure_reason)
                return False
        self.transition(SupervisorState.RUNNING, reason="daemon_ready")
        return True

    async def stop(self) -> None:
        self.transition(SupervisorState.STOPPING, reason="daemon_shutdown")
        self.transition(SupervisorState.INACTIVE, reason="daemon_stopped")

    def escalate_liquidation_race_failure(
        self,
        *,
        strategy_id: str,
        symbol: str,
        remaining_open_orders: int,
        timeout_seconds: float,
    ) -> None:
        reason = (
            f"liquidation_race_failure:{symbol.upper()}:"
            f"{remaining_open_orders}_open_after_{timeout_seconds:.1f}s"
        )
        if self.on_block_consumption:
            self.on_block_consumption(reason)
        if self._state == SupervisorState.RUNNING:
            self.transition(SupervisorState.DEGRADED, reason=reason)
        log.critical(
            "liquidation_race_failure strategy_id=%s symbol=%s remaining=%s",
            strategy_id,
            symbol.upper(),
            remaining_open_orders,
        )

    async def execute_runbook(self, step: RunbookStep) -> RunbookExecutionResult:
        job_id = RUNBOOK_JOB_ID[step]
        ctx = RunbookContext(
            step=step,
            job_id=job_id,
            supervisor_state=self._state,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        self._active_runbook = step
        self._set_stage(CronLifecycleStage.SCHEDULED)

        if self._state not in (SupervisorState.RUNNING, SupervisorState.DEGRADED):
            error = f"supervisor not runnable state={self._state.value}"
            return await self._fail_runbook(ctx, error, payload={})

        worker = self.workers.get(step)
        validator = self.validators.get(step)
        if worker is None or validator is None:
            error = f"missing worker or validator for {step.value}"
            return await self._fail_runbook(ctx, error, payload={})

        try:
            self._run_structural_hooks(self.pre_hooks.get(step, ()), ctx)
            self._set_stage(CronLifecycleStage.DISPATCHED)
            self._set_stage(CronLifecycleStage.EXECUTING)
            payload = await worker()
            self._run_structural_hooks_post(
                self.post_hooks.get(step, ()), ctx, payload
            )
            self._set_stage(CronLifecycleStage.VALIDATING)
            ok, reason = validator(payload)
            if not ok:
                restored = False
                if RUNBOOK_RESTORE_ON_FAILURE.get(step, False) and self.restore_on_failure:
                    restored = self.restore_on_failure()
                    payload["lkg_restored"] = restored
                    if restored:
                        # LATCH FIX (INCIDENT 2026-08-07): restore_on_failure already re-stamped a
                        # clean-baseline champion fresh THIS cycle. Historically the run still returned
                        # FAILED and the live-consumption gate latched until the NEXT daily readiness run
                        # -- a ~24h freeze (1437 blocked cycles, 08-07T12:30Z -> 08-08T12:31Z). Re-run
                        # the worker ONCE and re-validate so we RELEASE in-cycle when the heal actually
                        # cleared the failure. Bounded: exactly one restore + one re-validate, no loop.
                        #   Guard 1: reached ONLY when restore returned True (a valid known-good state
                        #     existed); restore==False (genuinely-bad / no LKG) falls straight through to
                        #     the block below.
                        #   Guard 2 (clean-baseline-only) lives in restore_on_failure -- a REAL tuned
                        #     stale champion is never re-stamped, so this re-validate still fails and the
                        #     gate stays blocked (a stale tuned champion needs a re-tune, not a re-stamp).
                        #   Guard 3: a pass falls through to the normal success path, which RELEASES via
                        #     on_release_consumption -- that clears the in-memory _consumption_blocked
                        #     latch; a bare DB flip would not, leaving the running process gated.
                        # Scope: only the validator-FAILURE path retries. A worker EXCEPTION still blocks
                        # (the except branch is unchanged) -- a raised worker is a genuine error.
                        payload = await worker()
                        payload["lkg_restored"] = True
                        payload["revalidated_after_restore"] = True
                        ok, reason = validator(payload)
                if not ok:
                    if self.on_block_consumption:
                        self.on_block_consumption(reason)
                    self.transition(SupervisorState.DEGRADED, reason=reason)
                    self._record_job(
                        job_id,
                        status="validation_failed",
                        validation_passed=False,
                        error=reason,
                        payload=payload,
                    )
                    await self._alert_failure(step, reason, payload)
                    self._set_stage(CronLifecycleStage.ALERTED)
                    return RunbookExecutionResult(
                        step=step,
                        job_id=job_id,
                        stage=self._lifecycle_stage,
                        success=False,
                        validation_passed=False,
                        payload=payload,
                        error=reason,
                        hooks_executed=tuple(ctx.hooks_executed),
                    )
                # else: the post-restore re-validate PASSED -> fall through to the success/release path.

            if self.on_release_consumption:
                self.on_release_consumption()
            if self._state == SupervisorState.DEGRADED:
                self.transition(SupervisorState.RUNNING, reason="runbook_recovered")
            self._record_job(
                job_id,
                status="success",
                validation_passed=True,
                payload=payload,
                success=True,
            )
            self._set_stage(CronLifecycleStage.COMPLETED)
            return RunbookExecutionResult(
                step=step,
                job_id=job_id,
                stage=self._lifecycle_stage,
                success=True,
                validation_passed=True,
                payload=payload,
                error=None,
                hooks_executed=tuple(ctx.hooks_executed),
            )
        except Exception as exc:
            restored = False
            if RUNBOOK_RESTORE_ON_FAILURE.get(step, False) and self.restore_on_failure:
                restored = self.restore_on_failure()
            payload = {"lkg_restored": restored}
            if self.on_block_consumption:
                self.on_block_consumption(str(exc))
            self.transition(SupervisorState.DEGRADED, reason=str(exc))
            self._record_job(
                job_id,
                status="error",
                validation_passed=False,
                error=str(exc),
                payload=payload,
            )
            await self._alert_failure(step, str(exc), payload)
            self._set_stage(CronLifecycleStage.FAILED)
            return RunbookExecutionResult(
                step=step,
                job_id=job_id,
                stage=self._lifecycle_stage,
                success=False,
                validation_passed=False,
                payload=payload,
                error=str(exc),
                hooks_executed=tuple(ctx.hooks_executed),
            )
        finally:
            self._active_runbook = None
            if self._lifecycle_stage not in (
                CronLifecycleStage.FAILED,
                CronLifecycleStage.ALERTED,
            ):
                self._set_stage(CronLifecycleStage.IDLE)

    def _set_stage(self, stage: CronLifecycleStage) -> None:
        self._lifecycle_stage = stage
        log.info(
            "runbook_lifecycle step=%s stage=%s",
            self._active_runbook.value if self._active_runbook else "none",
            stage.value,
        )

    async def _fail_runbook(
        self,
        ctx: RunbookContext,
        error: str,
        *,
        payload: dict[str, Any],
    ) -> RunbookExecutionResult:
        if self.on_block_consumption:
            self.on_block_consumption(error)
        self.transition(SupervisorState.DEGRADED, reason=error)
        self._record_job(
            ctx.job_id,
            status="error",
            validation_passed=False,
            error=error,
            payload=payload,
        )
        await self._alert_failure(ctx.step, error, payload)
        self._set_stage(CronLifecycleStage.ALERTED)
        return RunbookExecutionResult(
            step=ctx.step,
            job_id=ctx.job_id,
            stage=self._lifecycle_stage,
            success=False,
            validation_passed=False,
            payload=payload,
            error=error,
            hooks_executed=tuple(ctx.hooks_executed),
        )

    async def _alert_failure(
        self,
        step: RunbookStep,
        error: str,
        payload: Mapping[str, Any],
    ) -> None:
        await dispatch_system_alert(
            step.value,
            {
                "error": error,
                "job_id": RUNBOOK_JOB_ID[step],
                "supervisor_state": self._state.value,
                "lifecycle_stage": self._lifecycle_stage.value,
                "payload": dict(payload),
            },
        )

    def _run_structural_hooks(
        self,
        hooks: tuple[Callable[[RunbookContext], None], ...],
        ctx: RunbookContext,
    ) -> None:
        for hook in hooks:
            hook(ctx)
            ctx.hooks_executed.append(hook.__name__)

    def _run_structural_hooks_post(
        self,
        hooks: tuple[Callable[[RunbookContext, dict[str, Any]], None], ...],
        ctx: RunbookContext,
        payload: dict[str, Any],
    ) -> None:
        for hook in hooks:
            hook(ctx, payload)
            ctx.hooks_executed.append(hook.__name__)

    def _record_job(
        self,
        job_id: str,
        *,
        status: str,
        validation_passed: bool,
        error: str | None = None,
        payload: dict[str, Any] | None = None,
        success: bool = False,
    ) -> None:
        persistence.upsert_maintenance_job_status(
            job_id=job_id,
            status=status,
            validation_passed=validation_passed,
            error=error,
            payload=payload,
            success=success,
            db_path=self.vault_path,
        )


_ALLOWED_SUPERVISOR_TRANSITIONS: dict[SupervisorState, frozenset[SupervisorState]] = {
    SupervisorState.INACTIVE: frozenset({SupervisorState.STARTING}),
    SupervisorState.STARTING: frozenset({SupervisorState.RUNNING, SupervisorState.FAILED}),
    SupervisorState.RUNNING: frozenset(
        {SupervisorState.DEGRADED, SupervisorState.STOPPING, SupervisorState.FAILED}
    ),
    SupervisorState.DEGRADED: frozenset(
        {SupervisorState.RUNNING, SupervisorState.STOPPING, SupervisorState.FAILED}
    ),
    SupervisorState.STOPPING: frozenset({SupervisorState.INACTIVE, SupervisorState.FAILED}),
    SupervisorState.FAILED: frozenset({SupervisorState.INACTIVE, SupervisorState.STARTING}),
}


async def dispatch_system_alert(
    job_name: str,
    error_details: Mapping[str, Any],
) -> AlertDispatchResult:
    """Route maintenance/control-plane failures through dispatch_critical_page."""
    from src.control.alerts import IncidentType, dispatch_critical_page

    page = await dispatch_critical_page(
        IncidentType.MAINTENANCE_JOB_FAILED,
        f"Maintenance scheduler job failed: {job_name}",
        {"job_name": job_name, **dict(error_details)},
        cooldown_seconds=0.0,
    )
    return AlertDispatchResult(
        delivered=page.delivered,
        channel=page.channel,
        status_code=page.status_code,
        reason=page.reason,
    )


def hook_assert_vault_present(ctx: RunbookContext) -> None:
    from pathlib import Path

    vault_path = Path(getattr(ctx, "vault_path", "")) if hasattr(ctx, "vault_path") else None
    if vault_path is not None and not vault_path.exists():
        raise RuntimeError("research vault missing")


def hook_record_runbook_start(ctx: RunbookContext) -> None:
    log.info(
        "runbook_start step=%s job_id=%s supervisor=%s",
        ctx.step.value,
        ctx.job_id,
        ctx.supervisor_state.value,
    )


def hook_record_runbook_complete(ctx: RunbookContext, payload: dict[str, Any]) -> None:
    log.info(
        "runbook_complete step=%s job_id=%s keys=%s",
        ctx.step.value,
        ctx.job_id,
        sorted(payload.keys()),
    )
