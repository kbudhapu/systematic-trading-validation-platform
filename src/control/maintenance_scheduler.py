"""
Centralized maintenance job scheduler for offline research and pre/post-market ops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.config import DATA_DIR, ROOT
from src.control.config_watcher import ConfigWatcher
from src.engine.tuner_limits import TUNER_SUBPROCESS_TIMEOUT_SECONDS
from src.engine.control_plane import (
    ControlPlaneSupervisor,
    RunbookStep,
    hook_record_runbook_complete,
    hook_record_runbook_start,
)
from src.engine.governance import HumanOverrideRegistry
from src.persistence import db as persistence

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
RESEARCH_VAULT_PATH = DATA_DIR / "research_vault.db"
TUNER_REPORT_PATH = DATA_DIR / "adaptive_tuner_report.json"
ALLOCATION_SNAPSHOT_PATH = DATA_DIR / "allocation_matrix_snapshot.json"
VAULT_BACKUP_DIR = DATA_DIR / "backups"
CHAMPION_STALENESS_MAX_DAYS = 14
DAEMON_POLL_SECONDS = 30.0

JOB_WEEKEND_TUNER = "weekend_parameter_tuner"
JOB_WEEKLY_POLICY = "weekly_policy_brain"
JOB_PRE_OPEN = "pre_open_readiness"
JOB_POST_CLOSE = "post_close_reconciliation"
JOB_VAULT_BACKUP = "vault_backup"

TUNER_SUCCESS_STATUSES = frozenset(
    {"promoted", "incumbent_retained", "maintained_fallback", "dry_run_promoted"}
)
TUNER_FAILURE_STATUSES = frozenset({"error", "data_corruption"})

STRATEGY_YAML: dict[str, Path] = {
    "QQQ": ROOT / "config" / "strategies" / "mean_reversion_qqq.yaml",
    "SPY": ROOT / "config" / "strategies" / "mean_reversion_spy.yaml",
}


@dataclass(frozen=True)
class ScheduledMaintenanceJob:
    job_id: str
    hour: int
    minute: int
    weekdays: frozenset[int] | None
    handler_name: str


@dataclass(frozen=True)
class LiveConsumptionGate:
    allowed: bool
    reason: str
    using_last_known_good: bool


WEEKEND_TUNER_JOB = ScheduledMaintenanceJob(
    job_id=JOB_WEEKEND_TUNER,
    hour=6,
    minute=0,
    weekdays=frozenset({6}),
    handler_name="run_weekend_parameter_tuner",
)
WEEKLY_POLICY_JOB = ScheduledMaintenanceJob(
    job_id=JOB_WEEKLY_POLICY,
    hour=22,
    minute=0,
    weekdays=frozenset({5}),
    handler_name="run_weekly_policy_brain",
)
PRE_OPEN_JOB = ScheduledMaintenanceJob(
    job_id=JOB_PRE_OPEN,
    hour=8,
    minute=30,
    weekdays=None,
    handler_name="run_pre_open_readiness",
)
POST_CLOSE_JOB = ScheduledMaintenanceJob(
    job_id=JOB_POST_CLOSE,
    hour=16,
    minute=30,
    weekdays=None,
    handler_name="run_post_close_reconciliation",
)

VAULT_BACKUP_JOB = ScheduledMaintenanceJob(
    job_id=JOB_VAULT_BACKUP,
    hour=3,                       # off-hours (03:00 ET)
    minute=0,
    weekdays=None,
    handler_name="run_vault_backup",
)

DEFAULT_JOBS: tuple[ScheduledMaintenanceJob, ...] = (
    WEEKEND_TUNER_JOB,
    WEEKLY_POLICY_JOB,
    PRE_OPEN_JOB,
    POST_CLOSE_JOB,
    VAULT_BACKUP_JOB,
)

# R3: the durable maintenance daemon runs OPERATIONAL jobs only. It EXCLUDES:
#   - weekend_parameter_tuner + weekly_policy_brain -- doctrine-illegal parameter
#     mutators (PSD L4 / LLD §3); gated by RESEARCH_HALT, and left out of the
#     daemon's job set as defence-in-depth (STANDING maintenance-automation item).
#   - vault_backup -- deduped: the standalone `MbappeDailyBackup` scheduled task
#     (P2) owns off-hours vault backups, independent of daemon health.
# So the daemon carries pre_open_readiness (clears the live-consumption gate) and
# post_close_reconciliation.
OPERATIONAL_JOBS: tuple[ScheduledMaintenanceJob, ...] = (
    PRE_OPEN_JOB,
    POST_CLOSE_JOB,
)


def _active_mean_reversion_symbols() -> list[str]:
    """Symbols of the enabled mean-reversion legs, for the pre-open champion/
    inventory checks. Config-derived (replaces the removed
    `scripts.adaptive_parameter_tuner.active_mr_symbols`); an empty result falls
    back to the auto-seed defaults in the caller."""
    try:
        from src.config import load_config

        return [
            s.symbol.upper()
            for s in load_config().strategies
            if s.enabled and "mean_reversion" in s.module
        ]
    except Exception:  # pragma: no cover - defensive; caller falls back to defaults
        return []


class MaintenanceScheduler:
    """Cron-style daemon for institutional maintenance windows."""

    def __init__(
        self,
        config_watcher: ConfigWatcher | None = None,
        vault_path: Path = RESEARCH_VAULT_PATH,
        jobs: tuple[ScheduledMaintenanceJob, ...] = DEFAULT_JOBS,
    ) -> None:
        self.config_watcher = config_watcher or ConfigWatcher()
        self.vault_path = vault_path
        self.jobs = jobs
        self._last_fired: dict[str, str] = {}
        self._consumption_blocked = False
        self._consumption_block_reason = ""
        self.override_registry = HumanOverrideRegistry(db_path=vault_path)
        self.supervisor = ControlPlaneSupervisor(
            vault_path=vault_path,
            workers={
                RunbookStep.PRE_OPEN_HYDRATION: self._invoke_pre_open_readiness,
                RunbookStep.POST_CLOSE_RECONCILIATION: self._invoke_post_close_reconciliation,
                RunbookStep.WEEKEND_PARAMETER_TUNER: self._invoke_weekend_tuner,
                RunbookStep.WEEKLY_POLICY_SWAP: self._invoke_weekly_policy_brain,
            },
            validators={
                RunbookStep.PRE_OPEN_HYDRATION: _validate_pre_open_payload,
                RunbookStep.POST_CLOSE_RECONCILIATION: _validate_post_close_payload,
                RunbookStep.WEEKEND_PARAMETER_TUNER: _validate_tuner_payload,
                RunbookStep.WEEKLY_POLICY_SWAP: _validate_policy_payload,
            },
            pre_hooks={
                RunbookStep.PRE_OPEN_HYDRATION: (hook_record_runbook_start,),
                RunbookStep.POST_CLOSE_RECONCILIATION: (hook_record_runbook_start,),
                RunbookStep.WEEKEND_PARAMETER_TUNER: (hook_record_runbook_start,),
                RunbookStep.WEEKLY_POLICY_SWAP: (hook_record_runbook_start,),
            },
            post_hooks={
                RunbookStep.PRE_OPEN_HYDRATION: (hook_record_runbook_complete,),
                RunbookStep.POST_CLOSE_RECONCILIATION: (hook_record_runbook_complete,),
                RunbookStep.WEEKEND_PARAMETER_TUNER: (hook_record_runbook_complete,),
                RunbookStep.WEEKLY_POLICY_SWAP: (hook_record_runbook_complete,),
            },
            restore_on_failure=self._restore_last_known_good_configs,
            on_block_consumption=self._block_live_consumption,
            on_release_consumption=self._release_live_consumption,
        )
        # E7/R7: explicit job-handler registry (replaces `getattr(self, handler_name)`
        # reflection dispatch, F9). Same job ids / same handlers; an unregistered
        # handler now fails loudly at construction-adjacent lookup, not at fire time.
        self._job_handlers: dict[str, Callable[[], Awaitable[None]]] = {
            "run_weekend_parameter_tuner": self.run_weekend_parameter_tuner,
            "run_weekly_policy_brain": self.run_weekly_policy_brain,
            "run_vault_backup": self.run_vault_backup,
            "run_pre_open_readiness": self.run_pre_open_readiness,
            "run_post_close_reconciliation": self.run_post_close_reconciliation,
        }
        persistence.init_db()

    def _handler_for(self, job: ScheduledMaintenanceJob) -> Callable[[], Awaitable[None]]:
        try:
            return self._job_handlers[job.handler_name]
        except KeyError:
            raise ValueError(
                f"no registered handler {job.handler_name!r} for job {job.job_id!r}"
            ) from None

    def live_consumption_gate(self) -> LiveConsumptionGate:
        if self._consumption_blocked:
            return LiveConsumptionGate(
                allowed=False,
                reason=self._consumption_block_reason,
                using_last_known_good=False,
            )
        critical_jobs = (JOB_WEEKEND_TUNER, JOB_PRE_OPEN)
        for job_id in critical_jobs:
            row = persistence.get_maintenance_job_status(job_id, self.vault_path)
            if row is None:
                continue
            if int(row.get("validation_passed", 0)) == 0:
                # FINDING-2 (INCIDENT-20260722): surface the ACTUAL failure reason the daemon already
                # persisted (`last_error`, e.g. "stale champions: QQQ,SPY") at the block site the
                # operator reads first — not just the generic "validation not passed".
                detail = str(row.get("last_error") or "").strip()
                reason = f"{job_id} validation not passed"
                if detail:
                    reason = f"{reason}: {detail}"
                return LiveConsumptionGate(
                    allowed=False,
                    reason=reason,
                    using_last_known_good=False,
                )
        return LiveConsumptionGate(
            allowed=True,
            reason="",
            using_last_known_good=False,
        )

    def assert_live_consumption_allowed(self) -> bool:
        gate = self.live_consumption_gate()
        if gate.allowed:
            return True
        log.critical(
            "MAINTENANCE GATE BLOCKED live consumption: %s",
            gate.reason,
        )
        return False

    def assert_readiness_lkg_present(self) -> None:
        """R5 (INCIDENT-20260722 FINDING-6): every enabled readiness leg MUST have a populated LKG
        champion entry -- an empty fallback is never again a silent else-branch discovered mid-incident.
        Self-heals the common case (baseline-seedable legs), then raises a LOUD config error for any
        leg that still has no LKG (e.g. no baseline params -- a genuine config problem to fix)."""
        symbols = [s.upper() for s in
                   (_active_mean_reversion_symbols() or list(persistence.DEFAULT_AUTO_SEED_SYMBOLS))]
        try:
            # auto-seed re-stamps a MISSING champion to FRESH baseline (promoted_at=now).
            persistence.auto_seed_baseline_champions_if_needed(self.vault_path, tuple(symbols))
            # R5 SEED-CONDITION FIX (INCIDENT-20260722): snapshot the LKG ONLY from currently-valid,
            # NON-STALE champion state -- never write a stale champion as 'known good'. A stale-but-
            # present champion is not re-seeded above, so it is EXCLUDED here; if it also has no prior
            # (valid) LKG entry, the assertion below fails LOUD and no seed is written. This keeps the
            # self-heal from reintroducing the silent-else-branch on stale/invalid state.
            staleness = audit_champion_staleness(self.vault_path, symbols)
            fresh = [s for s in symbols if not staleness.get(s, {}).get("stale", True)]
            persistence.snapshot_champions_to_lkg(fresh, self.vault_path)
        except Exception as exc:
            log.warning("startup_lkg_self_heal_failed error=%s", exc)
        have = persistence.champion_lkg_symbols(self.vault_path)
        missing = [s for s in symbols if s not in have]
        if missing:
            raise RuntimeError(
                f"MAINTENANCE STARTUP CONFIG ERROR: readiness legs missing an LKG champion entry: "
                f"{missing}. Seed them via upsert_baseline_regime_champion before starting the daemon "
                f"(the LKG fallback must be real -- INCIDENT-20260722 R5).")
        log.info("maintenance_startup_lkg_ok symbols=%s", symbols)

    async def run_daemon(self, poll_seconds: float = DAEMON_POLL_SECONDS) -> None:
        log.info("maintenance_scheduler_daemon_start poll_seconds=%.1f", poll_seconds)
        self.assert_readiness_lkg_present()          # R5: fail loud if the LKG fallback is empty
        await self.supervisor.start()
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("maintenance_scheduler_tick_failed")
            await asyncio.sleep(poll_seconds)

    async def tick(self) -> None:
        now_et = datetime.now(ET)
        for job in self.jobs:
            if not self._should_fire(job, now_et):
                continue
            handler = self._handler_for(job)
            await handler()
            self._last_fired[job.job_id] = now_et.date().isoformat()

    async def run_job(self, job_id: str) -> None:
        job = next((item for item in self.jobs if item.job_id == job_id), None)
        if job is None:
            raise ValueError(f"unknown maintenance job: {job_id}")
        handler = self._handler_for(job)
        await handler()

    def _should_fire(self, job: ScheduledMaintenanceJob, now_et: datetime) -> bool:
        if job.weekdays is not None and now_et.weekday() not in job.weekdays:
            return False
        if now_et.hour != job.hour or now_et.minute != job.minute:
            return False
        session_key = now_et.date().isoformat()
        if self._last_fired.get(job.job_id) == session_key:
            return False
        return True

    async def run_weekend_parameter_tuner(self) -> None:
        if self.override_registry.is_research_halted():
            # LS-1: RESEARCH_HALT is a deliberate operator state; skipping the job is the CORRECT
            # expected behaviour, not a critical incident. WARNING, not CRITICAL (no false page).
            log.warning("RESEARCH_HALT active - skipping weekend parameter tuner")
            return
        await self.supervisor.execute_runbook(RunbookStep.WEEKEND_PARAMETER_TUNER)

    async def run_weekly_policy_brain(self) -> None:
        if self.override_registry.is_research_halted():
            # LS-1: expected halt-skip, not a critical incident. WARNING, not CRITICAL.
            log.warning("RESEARCH_HALT active - skipping weekly policy swap")
            return
        await self.supervisor.execute_runbook(RunbookStep.WEEKLY_POLICY_SWAP)

    async def run_vault_backup(self) -> None:
        """Off-hours vault backup. No-ops unless config `backup.enabled` is true
        (P2). Runs the standalone backup entrypoint via a thread (blocking I/O)."""
        import asyncio

        from src.persistence.backup import run_scheduled_backup

        archive = await asyncio.to_thread(run_scheduled_backup)
        if archive is None:
            log.info("vault_backup skipped (backup.enabled=false)")
        else:
            log.info("vault_backup complete: %s", archive)

    async def run_pre_open_readiness(self) -> None:
        await self.supervisor.execute_runbook(RunbookStep.PRE_OPEN_HYDRATION)

    async def run_post_close_reconciliation(self) -> None:
        await self.supervisor.execute_runbook(RunbookStep.POST_CLOSE_RECONCILIATION)

    def _release_live_consumption(self) -> None:
        self._consumption_blocked = False
        self._consumption_block_reason = ""

    def _block_live_consumption(self, reason: str) -> None:
        self._consumption_blocked = True
        self._consumption_block_reason = reason

    async def _invoke_weekend_tuner(self) -> dict[str, Any]:
        return await asyncio.to_thread(_run_weekend_tuner_subprocess)

    async def _invoke_weekly_policy_brain(self) -> dict[str, Any]:
        return await asyncio.to_thread(_run_policy_brain_subprocess)

    async def _invoke_pre_open_readiness(self) -> dict[str, Any]:
        from src.engine.recon_recovery import is_deferred_pre_open_recon_scheduled

        payload: dict[str, Any] = {}
        if is_deferred_pre_open_recon_scheduled():
            from src.config import load_config
            from src.engine.orchestrator import TradingOrchestrator

            orchestrator = TradingOrchestrator(load_config(), self.config_watcher)
            recon_ok = await orchestrator.run_scheduled_pre_flight_reconciliation()
            payload["deferred_pre_flight_recon"] = {
                "scheduled": True,
                "success": recon_ok,
            }
        checks = await asyncio.to_thread(self._run_pre_open_checks)
        payload.update(checks)
        return payload

    async def _invoke_post_close_reconciliation(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_post_close_tasks)

    def _run_pre_open_checks(self) -> dict[str, Any]:
        config = self.config_watcher.get_latest()
        # R6 (INCIDENT-20260722): the readiness symbol set is DERIVED from the currently ENABLED
        # strategy legs at validation time -- NOT a hardcoded fallback. A config-disabled leg leaves
        # the set automatically (no perpetual re-stamp); re-enabling a leg pulls it back in and
        # readiness then fails loudly if its champion is missing/stale (never silently skipped).
        symbols = _active_mean_reversion_symbols()
        auto_seed_result: dict[str, Any] = {"seeded": False, "symbols": []}
        try:
            auto_seed_result = persistence.auto_seed_baseline_champions_if_needed(
                self.vault_path,
                tuple(s.upper() for s in symbols),
            )
            if auto_seed_result.get("seeded"):
                log.info(
                    "pre_open_auto_seed champions symbols=%s reason=%s",
                    auto_seed_result.get("symbols"),
                    auto_seed_result.get("reason"),
                )
        except Exception as exc:
            log.warning("pre_open_auto_seed_failed error=%s", exc)
            auto_seed_result = {"seeded": False, "error": str(exc)}

        paper = "paper" in config.alpaca_base_url.lower()
        inventory: dict[str, dict[str, bool]] = {}
        short_client = None
        try:
            from alpaca.trading.client import TradingClient

            short_client = TradingClient(
                config.alpaca_api_key, config.alpaca_secret_key, paper=paper
            )
        except Exception as exc:  # pragma: no cover - client construction
            log.warning("pre_open_short_client_unavailable error=%s", exc)
        for symbol in symbols:
            # Short-availability is a WARNING-only signal in _validate_pre_open_payload
            # (it never blocks the gate), so any lookup failure degrades gracefully.
            try:
                from src.router.risk_manager import asset_context_from_alpaca

                if short_client is None:
                    raise RuntimeError("short-availability client unavailable")
                asset = short_client.get_asset(symbol)
                ctx = asset_context_from_alpaca(asset)
                inventory[symbol] = {
                    "shortable": ctx.shortable,
                    "easy_to_borrow": ctx.easy_to_borrow,
                }
            except Exception as exc:
                log.warning("pre_open_inventory_check_failed symbol=%s error=%s", symbol, exc)
                inventory[symbol] = {
                    "shortable": False,
                    "easy_to_borrow": False,
                    "error": str(exc),
                }

        staleness = audit_champion_staleness(self.vault_path, symbols)
        # R5 (INCIDENT-20260722): snapshot each present + fresh champion into the LKG (the
        # 'on every successful validation' trigger) so the fallback is always real.
        fresh_syms = [s for s in symbols if not staleness.get(s, {}).get("stale", True)]
        if fresh_syms:
            try:
                persistence.snapshot_champions_to_lkg(fresh_syms, self.vault_path)
            except Exception as exc:  # never fail the readiness check on an LKG write
                log.warning("champion_lkg_snapshot_failed error=%s", exc)
        config_ready = verify_strategy_config_readiness(symbols)
        self.config_watcher.request_reload()
        from src.engine.config_engine import ConfigurationPrecedenceResolver

        alignment = ConfigurationPrecedenceResolver().enforce_preflight_config_alignment(
            self.config_watcher,
            strategy_ids=tuple(self.config_watcher.strategy_uuids.keys()),
        )

        return {
            "symbols": symbols,
            "inventory": inventory,
            "staleness": staleness,
            "config_ready": config_ready,
            "auto_seed": auto_seed_result,
            "config_alignment": {
                "aligned": alignment.aligned,
                "reason": alignment.reason,
                "mismatches": list(alignment.mismatches),
            },
        }

    def _run_post_close_tasks(self) -> dict[str, Any]:
        from dataclasses import asdict

        from src.config import DB_PATH
        from src.engine.config_engine import VaultBackupManager

        backup_manager = VaultBackupManager(vault_path=self.vault_path)
        allocation = refresh_allocation_matrix_snapshot(self.vault_path)
        health_review = review_health_score_ledger(self.vault_path)
        vault_backup_drill = backup_manager.execute_vault_backup_drill()
        trading_backup_drill = backup_manager.execute_sqlite_backup_drill(DB_PATH)
        shadow_eval = run_shadow_policy_evening_evaluation()
        policy_lifecycle = run_ai_policy_lifecycle_progression(self.vault_path)
        drift_review = run_model_drift_evaluation(self.vault_path)
        return {
            "allocation": allocation,
            "health_review": health_review,
            "vault_backup": vault_backup_drill.snapshot_path,
            "vault_backup_drill": asdict(vault_backup_drill),
            "trading_backup": trading_backup_drill.snapshot_path,
            "trading_backup_drill": asdict(trading_backup_drill),
            "shadow_policy": shadow_eval,
            "policy_lifecycle": policy_lifecycle,
            "drift_review": drift_review,
        }

    def _restore_last_known_good_configs(self) -> bool:
        restored_any = False
        # R5 (INCIDENT-20260722 FINDING-6): FIRST restore the CHAMPION from the LKG -- the artifact
        # pre_open_readiness actually consumes. The incident's failure was champion-staleness, which
        # the config-file restore below could never fix; restoring the LKG champion (re-stamped fresh)
        # clears a staleness/absence failure. Symbols = the currently enabled readiness legs.
        readiness_symbols = _active_mean_reversion_symbols() or list(persistence.DEFAULT_AUTO_SEED_SYMBOLS)
        for symbol in readiness_symbols:
            # Guard (INCIDENT 2026-08-07 latch fix): NEVER auto-restamp a NON-baseline (real tuned)
            # stale champion -- that would launder params computed on >TTL-old data as fresh and defeat
            # the TTL. Such a symbol is left stale so the (re-)validation still fails and the gate stays
            # blocked (correct: it needs a genuine re-tune, not a re-stamp). No-op today (all champions
            # are baselines); load-bearing the day a real champion source ships.
            if not persistence.champion_restore_is_baseline_safe(symbol, self.vault_path):
                log.critical(
                    "MAINTENANCE CHAMPION NOT AUTO-RESTORABLE symbol=%s (non-baseline/tuned champion "
                    "stale -- requires re-tune, gate stays blocked)", symbol)
                continue
            if persistence.restore_champion_from_lkg(symbol, self.vault_path):
                restored_any = True
                log.critical("MAINTENANCE LKG CHAMPION RESTORED symbol=%s (re-stamped from LKG)", symbol)
            else:
                log.critical("MAINTENANCE LKG CHAMPION MISSING for symbol=%s", symbol)
        for symbol, yaml_path in STRATEGY_YAML.items():
            if not yaml_path.exists():
                continue
            raw_yaml = _fetch_latest_config_backup(self.vault_path, symbol)
            if raw_yaml is None:
                backup = persistence.get_prior_config_backup(symbol, self.vault_path)
                if backup is not None:
                    _, raw_yaml = backup
            if raw_yaml is None:
                log.critical(
                    "MAINTENANCE LKG RESTORE MISSING backup for symbol=%s",
                    symbol,
                )
                continue
            yaml_path.write_text(raw_yaml, encoding="utf-8")
            restored_any = True
            log.critical(
                "MAINTENANCE LKG RESTORED symbol=%s path=%s",
                symbol,
                yaml_path,
            )
        if restored_any:
            self.config_watcher.request_reload()
        return restored_any


def _run_weekend_tuner_subprocess() -> dict[str, Any]:
    script = ROOT / "scripts" / "adaptive_parameter_tuner.py"
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=TUNER_SUBPROCESS_TIMEOUT_SECONDS,
    )
    payload: dict[str, Any] = {
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }
    if TUNER_REPORT_PATH.exists():
        payload["report"] = json.loads(TUNER_REPORT_PATH.read_text(encoding="utf-8"))
    if completed.returncode != 0:
        raise RuntimeError(
            f"adaptive_parameter_tuner exited {completed.returncode}: {completed.stderr[-500:]}"
        )
    return payload


def _run_policy_brain_subprocess() -> dict[str, Any]:
    script = ROOT / "scripts" / "optimize_policy_brain.py"
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    payload: dict[str, Any] = {
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }
    if completed.stdout.strip():
        try:
            payload["result"] = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload["result"] = None
    if completed.returncode != 0:
        raise RuntimeError(
            f"optimize_policy_brain exited {completed.returncode}: {completed.stderr[-500:]}"
        )
    return payload


def _validate_tuner_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    report = payload.get("report")
    if not isinstance(report, dict):
        return False, "missing tuner report"
    results = report.get("results", {})
    if not results:
        return False, "empty tuner results"
    for symbol, result in results.items():
        if not isinstance(result, dict):
            return False, f"{symbol}: invalid result payload"
        status = str(result.get("status", "error"))
        if status in TUNER_FAILURE_STATUSES:
            return False, f"{symbol}:{status}"
        if status not in TUNER_SUCCESS_STATUSES:
            return False, f"{symbol}:unrecognized_status:{status}"
    return True, ""


def _validate_policy_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return False, "missing policy optimizer result"
    if int(result.get("sample_count", 0) or 0) <= 0:
        return False, "insufficient shadow RL samples"
    promoted = bool(result.get("promoted", False))
    metrics = result.get("metrics") or {}
    if promoted:
        return True, ""
    model_sharpe = float(metrics.get("model_sharpe", 0.0) or 0.0)
    rules_sharpe = float(metrics.get("rules_sharpe", 0.0) or 0.0)
    if model_sharpe < rules_sharpe:
        return True, ""
    return True, ""


def _validate_pre_open_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    if not payload.get("config_ready", False):
        return False, "strategy config not ready"
    auto_seed = payload.get("auto_seed") or {}
    freshly_seeded = {
        str(symbol).upper()
        for symbol in auto_seed.get("symbols", [])
        if auto_seed.get("seeded")
    }
    staleness = payload.get("staleness") or {}
    # R6 (INCIDENT-20260722): an ENABLED leg's MISSING champion must FAIL readiness (never silently
    # skipped) -- so `no_champion` is no longer excluded (auto-seed runs first, so a genuine miss means
    # the symbol has no baseline params, a real config problem). `vault_missing` stays excluded (an
    # infra read failure, not a per-symbol champion state).
    stale_symbols = [
        symbol
        for symbol, meta in staleness.items()
        if bool(meta.get("stale", False))
        and str(symbol).upper() not in freshly_seeded
        and meta.get("reason") != "vault_missing"
    ]
    if stale_symbols:
        missing = [s for s in stale_symbols if str(staleness[s].get("reason")) == "no_champion"]
        label = "missing/stale champions" if missing else "stale champions"
        return False, f"{label}: {','.join(stale_symbols)}"
    inventory = payload.get("inventory") or {}
    blocked = [
        symbol
        for symbol, meta in inventory.items()
        if not meta.get("easy_to_borrow", False) or not meta.get("shortable", False)
    ]
    if blocked:
        log.warning("pre_open_short_inventory_warning symbols=%s", blocked)
    return True, ""


def _validate_post_close_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    drill = payload.get("vault_backup_drill")
    if isinstance(drill, dict) and drill.get("verification_passed"):
        return True, ""
    if payload.get("vault_backup"):
        return True, ""
    return False, "research vault backup missing"


def audit_champion_staleness(
    vault_path: Path,
    symbols: list[str],
    max_age_days: int = CHAMPION_STALENESS_MAX_DAYS,
) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}

    now = datetime.now(timezone.utc)
    out: dict[str, dict[str, Any]] = {}
    try:
        persistence.ensure_regime_champions_table(vault_path)
        with sqlite3.connect(vault_path) as conn:
            for symbol in symbols:
                row = conn.execute(
                    """
                    SELECT MAX(promoted_at) AS promoted_at, MAX(composite_score) AS score
                    FROM regime_champions
                    WHERE symbol = ?
                    """,
                    (symbol.upper(),),
                ).fetchone()
                if row is None or row[0] is None:
                    out[symbol] = {"stale": True, "reason": "no_champion"}
                    continue
                promoted_at = datetime.fromisoformat(str(row[0]))
                if promoted_at.tzinfo is None:
                    promoted_at = promoted_at.replace(tzinfo=timezone.utc)
                age_days = (now - promoted_at.astimezone(timezone.utc)).days
                out[symbol] = {
                    "stale": age_days > max_age_days,
                    "age_days": age_days,
                    "composite_score": float(row[1] or 0.0),
                    "promoted_at": row[0],
                }
    except (sqlite3.Error, OSError, ValueError) as exc:
        log.warning("audit_champion_staleness_failed error=%s", exc)
        return {
            symbol: {"stale": True, "reason": "vault_query_failed", "error": str(exc)}
            for symbol in symbols
        }
    return out


def verify_strategy_config_readiness(symbols: list[str]) -> bool:
    if not symbols:
        return False
    for symbol in symbols:
        yaml_path = STRATEGY_YAML.get(symbol.upper())
        if yaml_path is None or not yaml_path.exists():
            return False
    return True


def refresh_allocation_matrix_snapshot(vault_path: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "legs": {},
    }
    if not vault_path.exists():
        ALLOCATION_SNAPSHOT_PATH.write_text(
            json.dumps(snapshot, indent=2),
            encoding="utf-8",
        )
        return snapshot

    with sqlite3.connect(vault_path) as conn:
        rows = conn.execute(
            """
            SELECT symbol, regime, composite_score, promoted_at
            FROM regime_champions
            ORDER BY symbol, composite_score DESC
            """
        ).fetchall()
    for symbol, regime, score, promoted_at in rows:
        snapshot["legs"].setdefault(symbol, []).append(
            {
                "regime": regime,
                "composite_score": float(score),
                "promoted_at": promoted_at,
            }
        )
    ALLOCATION_SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, indent=2),
        encoding="utf-8",
    )
    return snapshot


def review_health_score_ledger(vault_path: Path) -> dict[str, Any]:
    if not vault_path.exists():
        return {"transitions_24h": 0, "degraded_or_halted": 0}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with sqlite3.connect(vault_path) as conn:
        rows = conn.execute(
            """
            SELECT new_status, COUNT(*) AS n
            FROM system_health_ledger
            WHERE timestamp >= ?
            GROUP BY new_status
            """,
            (cutoff,),
        ).fetchall()
    summary = {str(status): int(count) for status, count in rows}
    degraded = summary.get("DEGRADED", 0) + summary.get("HALTED", 0)
    if degraded > 0:
        log.critical(
            "POST_CLOSE HEALTH REVIEW degraded_or_halted_24h=%s summary=%s",
            degraded,
            summary,
        )
    return {
        "transitions_24h": int(sum(summary.values())),
        "degraded_or_halted": degraded,
        "by_status": summary,
    }


def backup_research_vault(vault_path: Path) -> Path | None:
    if not vault_path.exists():
        return None
    VAULT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = VAULT_BACKUP_DIR / f"research_vault_{stamp}.db"
    shutil.copy2(vault_path, backup_path)
    return backup_path


def run_shadow_policy_evening_evaluation() -> dict[str, Any]:
    from scripts.optimize_policy_brain import ShadowPolicyMetricsEngine

    engine = ShadowPolicyMetricsEngine(db_path=RESEARCH_VAULT_PATH)
    return engine.run_evening_evaluation()


def run_ai_policy_lifecycle_progression(
    vault_path: Path = RESEARCH_VAULT_PATH,
) -> dict[str, Any]:
    from src.engine.governance import ImmutableChangeJournal, ensure_governance_schema
    from src.engine.policy_lifecycle import AIPolicyLifecycleManager

    ensure_governance_schema(vault_path)
    manager = AIPolicyLifecycleManager(db_path=vault_path)
    journal = ImmutableChangeJournal(db_path=vault_path)
    result = manager.run_evening_progression(change_journal=journal)
    return result


def run_model_drift_evaluation(vault_path: Path) -> dict[str, Any]:
    from src.engine.drift_evaluator import (
        CLASS_OLD_AND_WRONG,
        DIRECTIVE_FORCE_MINI_SWEEP,
        DIRECTIVE_STAGED_ROLLBACK,
        ModelDriftEvaluator,
    )

    evaluator = ModelDriftEvaluator(db_path=vault_path)
    legs: list[tuple[str, str, str | None]] = []
    try:
        persistence.ensure_regime_champions_table(vault_path)
        with sqlite3.connect(vault_path) as conn:
            rows = conn.execute(
                """
                SELECT symbol, regime
                FROM regime_champions
                GROUP BY symbol, regime
                """
            ).fetchall()
        strategy_map = {
            "QQQ": "mean_reversion_qqq",
            "SPY": "mean_reversion_spy",
        }
        for symbol, regime in rows:
            legs.append((str(symbol), str(regime), strategy_map.get(str(symbol).upper())))
    except sqlite3.Error as exc:
        return {"error": str(exc), "verdicts": [], "actions": []}

    verdicts = evaluator.evaluate_portfolio(legs, register_alerts=True)
    actions: list[dict[str, Any]] = []
    for verdict in verdicts:
        if verdict.classification != CLASS_OLD_AND_WRONG:
            continue
        if verdict.directive == DIRECTIVE_FORCE_MINI_SWEEP:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "adaptive_parameter_tuner.py"),
                    "--symbol",
                    verdict.symbol,
                    "--force-lookback-days",
                    "30",
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            actions.append(
                {
                    "symbol": verdict.symbol,
                    "directive": verdict.directive,
                    "exit_code": completed.returncode,
                    "alert_id": verdict.alert_id,
                }
            )
        elif verdict.directive == DIRECTIVE_STAGED_ROLLBACK:
            persistence.mark_failed_live_attribution(
                symbol=verdict.symbol,
                regime=verdict.regime,
                strategy_id=verdict.strategy_id or verdict.symbol,
                db_path=vault_path,
            )
            actions.append(
                {
                    "symbol": verdict.symbol,
                    "directive": verdict.directive,
                    "alert_id": verdict.alert_id,
                }
            )

    return {
        "verdicts": [
            {
                "symbol": v.symbol,
                "regime": v.regime,
                "classification": v.classification,
                "directive": v.directive,
                "reason": v.reason,
                "alert_id": v.alert_id,
                "metrics": {
                    "champion_age_days": v.metrics.champion_age_days,
                    "consecutive_misses": v.metrics.consecutive_misses,
                    "win_rate_decay": v.metrics.win_rate_decay,
                    "tracking_error_decay": v.metrics.tracking_error_decay,
                },
            }
            for v in verdicts
        ],
        "actions": actions,
    }


def _fetch_latest_config_backup(vault_path: Path, symbol: str) -> str | None:
    if not vault_path.exists():
        return None
    with sqlite3.connect(vault_path) as conn:
        row = conn.execute(
            """
            SELECT raw_yaml_content
            FROM config_version_registry
            WHERE symbol = ?
            ORDER BY version_id DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
    if row is None:
        return None
    return str(row[0])
