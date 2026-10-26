"""
CLI entrypoint — async orchestrator with uvloop on Linux/macOS.

Commands:
  backtest   Run walk-forward backtest
  run        Start live/paper trading loop
  bod-email  Send morning briefing
  eod-email  Send end-of-day report
  maintenance          Run scheduled maintenance daemon
  maintenance-once     Run one maintenance job (--job-id)
  bootstrap            Run human vault bootstrap playbook
  governance-snapshot  Print operational governance telemetry JSON
  governance-kill      Engage a kill switch level (--kill-level, --operator, --rationale)
  governance-release   Release a kill switch level
  governance-post-mortem  Export rollback post-mortem Markdown (--event-id)

E7/R7 (F13): the command bodies live in `src/cli/commands.py`; this module owns
only the argument parser and dispatch, so the `--help` surface is unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from src.ssl_certs import install_ssl_certificates

install_ssl_certificates()

import structlog

from src.cli.commands import (
    _install_uvloop,
    cmd_backtest,
    cmd_bod_email,
    cmd_bootstrap,
    cmd_eod_email,
    cmd_governance_kill,
    cmd_governance_post_mortem,
    cmd_governance_release,
    cmd_governance_snapshot,
    cmd_maintenance,
    cmd_maintenance_once,
    cmd_run,
    cmd_sweep_run,
)

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)
log = structlog.get_logger()


def main() -> None:
    """Parse CLI args and dispatch to the appropriate async command."""
    from src.config import load_env
    load_env()   # R3: .env -> os.environ BEFORE any os.getenv (NTFY_TOPIC, SMTP, Supabase, ...)
    parser = argparse.ArgumentParser(description="Trading Bot V1")
    parser.add_argument(
        "command",
        choices=[
            "backtest",
            "sweep",
            "run",
            "bod-email",
            "eod-email",
            "maintenance",
            "maintenance-once",
            "bootstrap",
            "governance-snapshot",
            "governance-kill",
            "governance-release",
            "governance-post-mortem",
            "operator-report",
        ],
        help=(
            "backtest | sweep | run | bod-email | eod-email | maintenance | "
            "maintenance-once | bootstrap | governance-snapshot | governance-kill | "
            "governance-release | governance-post-mortem | operator-report"
        ),
    )
    parser.add_argument(
        "--job-id",
        default="",
        help="Maintenance job id for maintenance-once",
    )
    parser.add_argument(
        "--force-clean-sweep",
        action="store_true",
        help="Delete corrupted vault and rerun full tuner initialization",
    )
    parser.add_argument(
        "--no-auto-seed",
        action="store_true",
        help="Disable automated baseline champion seeding during bootstrap",
    )
    parser.add_argument(
        "--kill-level",
        default="PORTFOLIO_HALT",
        help="Kill level for governance-kill / governance-release",
    )
    parser.add_argument(
        "--scope-key",
        default="GLOBAL",
        help="Scope key (strategy_id for STRATEGY_HALT, else GLOBAL)",
    )
    parser.add_argument(
        "--operator",
        default="cli",
        help="Operator id for governance kill/release",
    )
    parser.add_argument(
        "--rationale",
        default="manual cli intervention",
        help="Human-readable rationale for governance kill/release",
    )
    parser.add_argument(
        "--event-id",
        default="",
        help="Rollback event id for governance-post-mortem (journal:N or drift:N)",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output path for governance-post-mortem Markdown report",
    )
    args = parser.parse_args()

    _install_uvloop()

    commands = {
        "backtest": cmd_backtest,
        "sweep": cmd_sweep_run,
        "run": cmd_run,
        "bod-email": cmd_bod_email,
        "eod-email": cmd_eod_email,
        "maintenance": cmd_maintenance,
    }
    if args.command == "maintenance-once":
        if not args.job_id:
            log.error("missing_job_id", hint="Pass --job-id for maintenance-once")
            sys.exit(1)
        asyncio.run(cmd_maintenance_once(args.job_id))
        return
    if args.command == "bootstrap":
        cmd_bootstrap(args.force_clean_sweep, args.no_auto_seed)
        return
    if args.command == "governance-snapshot":
        cmd_governance_snapshot()
        return
    if args.command == "governance-kill":
        cmd_governance_kill(
            args.kill_level,
            args.operator,
            args.rationale,
            args.scope_key,
        )
        return
    if args.command == "governance-release":
        cmd_governance_release(
            args.kill_level,
            args.operator,
            args.rationale,
            args.scope_key,
        )
        return
    if args.command == "governance-post-mortem":
        if not args.event_id:
            log.error("missing_event_id", hint="Pass --event-id journal:N or drift:N")
            sys.exit(1)
        cmd_governance_post_mortem(args.event_id, args.output)
        return
    if args.command == "operator-report":
        from src.control.operator_report import operator_report
        print(operator_report())
        return
    asyncio.run(commands[args.command]())


if __name__ == "__main__":
    main()
