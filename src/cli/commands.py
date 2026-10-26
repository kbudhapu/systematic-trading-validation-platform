"""CLI subcommand handlers (E7/R7, F13).

Extracted verbatim from the root `main.py`, which had accreted every command body
alongside the argparse wiring. `main.py` is now a thin entrypoint that owns only
the argument parser (its `--help` surface is unchanged) and dispatches to these
handlers. Behavior is byte-identical -- the functions were moved, not modified.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path

import structlog

from src.backtest.engine import run_backtest
from src.backtest.sweep import cmd_sweep
from src.config import load_config
from src.control.config_watcher import ConfigWatcher
from src.control.maintenance_scheduler import MaintenanceScheduler
from src.engine.orchestrator import TradingOrchestrator

log = structlog.get_logger()


def _install_uvloop() -> None:
    """Install uvloop event loop policy when available (not on Windows)."""
    try:
        import uvloop

        uvloop.install()
        log.info("uvloop_installed")
    except ImportError:
        log.info("uvloop_unavailable", fallback="stdlib asyncio")


async def cmd_sweep_run() -> None:
    """Run parameter grid search with train/holdout validation."""
    await cmd_sweep()


def _load_runtime_config():
    config_dir = os.getenv("TRADING_CONFIG_DIR", "").strip()
    if not config_dir:
        return load_config(), None
    root = Path(config_dir)
    config = load_config(
        env_path=root / "env.yaml",
        strategies_dir=root / "strategies",
    )
    return config, root


async def cmd_backtest() -> None:
    """Run historical backtest with configured pass/fail gates."""
    config, _ = _load_runtime_config()
    await run_backtest(config)


async def cmd_run() -> None:
    """Start the event-driven trading loop."""
    # Dual-runtime guard (MIG-DUALRUNTIME): refuse to start if another live run
    # already holds the instance lock. systemd enforces one unit; this is
    # defence-in-depth against a stray second `main.py run` trading the same
    # account. flock auto-releases on process death, so a systemd restart is
    # never blocked by a stale lock.
    from src.single_instance import SingleInstanceError, acquire_run_lock

    try:
        acquire_run_lock()
    except SingleInstanceError as exc:
        log.error("single_instance_refused", error=str(exc))
        sys.exit(1)

    config, config_root = _load_runtime_config()
    if not config.alpaca_api_key:
        log.error("missing_keys", hint="Set ALPACA_API_KEY in .env")
        sys.exit(1)

    if config_root is not None:
        orchestrator = TradingOrchestrator(config_path=config_root)
    else:
        watcher = ConfigWatcher()
        orchestrator = TradingOrchestrator(config, watcher)
    try:
        from api.main import set_orchestrator

        set_orchestrator(orchestrator)
    except ImportError:
        pass

    # Graceful shutdown on SIGTERM (the systemd `systemctl stop` / redeploy path) and
    # SIGINT. Python's default SIGTERM disposition kills the process with NO cleanup, so
    # asyncio.run(cmd_run()) would die mid-run and leak the Alpaca websocket -> the next
    # process trips Alpaca's connection cap ("connection limit exceeded" on redeploy).
    # We cancel the run loop so the finally can release the socket cleanly. add_signal_handler
    # is POSIX-only (the droplet); on Windows it raises NotImplementedError -> suppressed.
    run_task = asyncio.ensure_future(orchestrator.run_forever())

    def _request_shutdown(signame: str) -> None:
        log.info("shutdown_signal_received", signal=signame)
        if not run_task.done():
            run_task.cancel()

    loop = asyncio.get_running_loop()
    for _sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(_sig, _request_shutdown, _sig.name)

    try:
        await run_task
    except asyncio.CancelledError:
        log.info("run_forever_cancelled_for_shutdown")
    finally:
        # Bounded + idempotent: releases the market-data websocket before exit so a redeploy
        # restart does not storm. Runs on clean end, cancellation, OR a run_forever crash.
        orchestrator.shutdown()


async def cmd_bod_email() -> None:
    """Send beginning-of-day email briefing."""
    config, config_root = _load_runtime_config()
    if config_root is not None:
        await TradingOrchestrator(config_path=config_root).run_bod()
    else:
        await TradingOrchestrator(config).run_bod()


async def cmd_eod_email() -> None:
    """Send end-of-day email report."""
    config, config_root = _load_runtime_config()
    if config_root is not None:
        await TradingOrchestrator(config_path=config_root).run_eod()
    else:
        await TradingOrchestrator(config).run_eod()


async def cmd_maintenance() -> None:
    """Run the maintenance scheduler daemon (operational jobs only).

    R3: the durable daemon carries pre_open_readiness + post_close_reconciliation.
    It excludes the two doctrine-illegal parameter mutators (weekend_parameter_tuner,
    weekly_policy_brain -- also gated by RESEARCH_HALT) and vault_backup (owned by
    the standalone MbappeDailyBackup task, P2)."""
    from src.control.maintenance_scheduler import OPERATIONAL_JOBS

    watcher = ConfigWatcher()
    scheduler = MaintenanceScheduler(watcher, jobs=OPERATIONAL_JOBS)
    await scheduler.run_daemon()


async def cmd_maintenance_once(job_id: str) -> None:
    """Run a single maintenance job immediately.

    The supervisor-backed jobs (pre_open_readiness, post_close_reconciliation,
    ...) require a RUNNING supervisor -- execute_runbook refuses on an INACTIVE
    one. run_daemon() starts it; a one-shot must too. Start (no broker
    pre-flight for a one-shot) -> run -> stop."""
    watcher = ConfigWatcher()
    scheduler = MaintenanceScheduler(watcher)
    await scheduler.supervisor.start(run_pre_flight=False)
    try:
        await scheduler.run_job(job_id)
    finally:
        await scheduler.supervisor.stop()


def cmd_governance_snapshot() -> None:
    """Print operational governance telemetry as JSON."""
    import json

    from src.engine.audit_panel import GovernanceTelemetryProvider

    provider = GovernanceTelemetryProvider()
    print(json.dumps(provider.snapshot_as_dict(), indent=2))


def cmd_governance_kill(
    kill_level: str,
    operator: str,
    rationale: str,
    scope_key: str,
) -> None:
    from src.engine.governance import HumanOverrideRegistry, KillLevel

    registry = HumanOverrideRegistry()
    state = registry.engage(
        KillLevel(kill_level.upper()),
        scope_key=scope_key,
        operator=operator,
        rationale=rationale,
    )
    log.info(
        "governance_kill_engaged",
        kill_level=state.kill_level.value,
        scope_key=state.scope_key,
        engaged_at=state.engaged_at,
    )


def cmd_governance_release(
    kill_level: str,
    operator: str,
    rationale: str,
    scope_key: str,
) -> None:
    if kill_level.upper() == "PRE_FLIGHT_RECON_LOCK":
        from src.engine.governance import release_pre_flight_recon_lock

        released = release_pre_flight_recon_lock(
            operator=operator,
            rationale=rationale,
        )
        if not released:
            log.warning("pre_flight_recon_lock_not_active")
            sys.exit(1)
        log.info(
            "pre_flight_recon_lock_released",
            operator=operator,
            rationale=rationale,
        )
        return

    from src.engine.governance import HumanOverrideRegistry, KillLevel

    registry = HumanOverrideRegistry()
    state = registry.release(
        KillLevel(kill_level.upper()),
        scope_key=scope_key,
        operator=operator,
        rationale=rationale,
    )
    log.info(
        "governance_kill_released",
        kill_level=state.kill_level.value,
        scope_key=state.scope_key,
        active=state.active,
    )


def cmd_governance_post_mortem(event_id: str, output_path: str) -> None:
    from pathlib import Path

    from src.engine.audit_panel import generate_rollback_post_mortem

    report = generate_rollback_post_mortem(event_id)
    if output_path:
        Path(output_path).write_text(report, encoding="utf-8")
        log.info("post_mortem_written", path=output_path, event_id=event_id)
    else:
        print(report)


def cmd_bootstrap(force_clean_sweep: bool, no_auto_seed: bool) -> None:
    """Run interactive or parameterized vault bootstrap playbook."""
    from src.engine.config_engine import run_human_bootstrap_playbook

    result = run_human_bootstrap_playbook(
        force_clean_sweep=force_clean_sweep,
        trust_auto_seed=not no_auto_seed,
        interactive=not force_clean_sweep,
    )
    log.info(
        "bootstrap_complete",
        action=result.action,
        clean_sweep=result.clean_sweep_executed,
        seeded=result.auto_seed.get("seeded"),
        integrity_ok=result.vault_integrity.integrity_ok,
        messages=list(result.messages),
    )
