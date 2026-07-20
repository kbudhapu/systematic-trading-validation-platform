"""
FastAPI control API — thin sidecar on the VPS.

Never runs strategy math; enqueues commands for the trading-bot single writer.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from src.ssl_certs import install_ssl_certificates

install_ssl_certificates()

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from src.backtest.engine import backtest_result_to_dict, run_backtest
from src.config import ROOT, load_config
from src.control.command_queue import (
    ControlCommandType,
    enqueue_control_command,
    enqueue_control_command_local,
)
from src.control.config_watcher import ConfigWatcher
from src.control.session_manager import SessionManager
from src.control.supabase_client import get_supabase
from src.control.supabase_sync import SupabaseSync
from src.engine.orchestrator import TradingOrchestrator
from src.persistence import db as persistence

load_dotenv(ROOT / ".env")

_orchestrator: TradingOrchestrator | None = None
_config_watcher = ConfigWatcher()


def _verify_token(
    credentials: HTTPAuthorizationCredentials | None = Security(HTTPBearer(auto_error=False)),
) -> None:
    token = os.getenv("CONTROL_API_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=503, detail="CONTROL_API_TOKEN not configured")
    if credentials is None or credentials.credentials != token:
        raise HTTPException(status_code=401, detail="Invalid API token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    persistence.init_db()
    yield


app = FastAPI(title="Trading Bot Control API", version="0.1.0", lifespan=lifespan)
auth_dep = [Depends(_verify_token)]


class GoLiveRequest(BaseModel):
    strategy_id: str
    confirm: str


class KillSwitchRequest(BaseModel):
    confirm: str


class GovernanceKillRequest(BaseModel):
    escalation_level: str  # canonical control-command key (migration 020; kill_level retired)
    scope_key: str = "GLOBAL"
    operator: str
    rationale: str
    confirm: str


class CommandIngestRequest(BaseModel):
    command_id: str | None = None
    command_type: str
    payload_json: dict[str, Any] | None = None
    requested_by: str | None = None
    idempotency_key: str | None = None


@app.post("/commands/ingest", dependencies=auth_dep)
def ingest_command_local(body: CommandIngestRequest) -> dict[str, Any]:
    """Stage a control command directly in local SQLite (dashboard cloud fallback)."""
    try:
        command_type = ControlCommandType(body.command_type)
    except ValueError as exc:
        raise HTTPException(400, f"Unknown command type: {body.command_type}") from exc
    command = enqueue_control_command_local(
        command_type,
        body.payload_json or {},
        requested_by=body.requested_by,
        idempotency_key=body.idempotency_key,
        command_id=body.command_id,
    )
    return {
        "status": "queued",
        "channel": "sqlite_local",
        "command_id": command.command_id,
        "command_type": command.command_type.value,
    }


@app.get("/governance/snapshot", dependencies=auth_dep)
def governance_snapshot() -> dict[str, Any]:
    """Operational governance telemetry for dashboard visibility."""
    from src.engine.audit_panel import GovernanceTelemetryProvider

    provider = GovernanceTelemetryProvider()
    return provider.snapshot_as_dict()


@app.post("/governance/kill-switch/engage", dependencies=auth_dep)
def governance_kill_engage(body: GovernanceKillRequest) -> dict[str, Any]:
    if body.confirm != "ENGAGE":
        raise HTTPException(400, 'Confirmation must be exactly "ENGAGE"')
    from src.engine.engine_preemption import RiskEscalationLevel

    try:
        level = RiskEscalationLevel(body.escalation_level.upper())
    except ValueError as exc:
        raise HTTPException(400, f"Unknown escalation_level: {body.escalation_level}") from exc
    command = enqueue_control_command(
        ControlCommandType.ENGAGE_KILL_SWITCH,
        payload={
            "escalation_level": level.value,
            "scope_key": body.scope_key,
            "operator": body.operator,
            "rationale": body.rationale,
        },
        requested_by=body.operator,
        idempotency_key=f"engage:{level.value}:{body.scope_key}:{uuid.uuid4()}",
    )
    return {
        "status": "queued",
        "command_id": command.command_id,
        "escalation_level": level.value,
        "scope_key": body.scope_key,
    }


@app.post("/governance/kill-switch/release", dependencies=auth_dep)
def governance_kill_release(body: GovernanceKillRequest) -> dict[str, Any]:
    if body.confirm != "RELEASE":
        raise HTTPException(400, 'Confirmation must be exactly "RELEASE"')
    from src.engine.engine_preemption import RiskEscalationLevel

    try:
        level = RiskEscalationLevel(body.escalation_level.upper())
    except ValueError as exc:
        raise HTTPException(400, f"Unknown escalation_level: {body.escalation_level}") from exc
    command = enqueue_control_command(
        ControlCommandType.RELEASE_KILL_SWITCH,
        payload={
            "escalation_level": level.value,
            "scope_key": body.scope_key,
            "operator": body.operator,
            "rationale": body.rationale,
        },
        requested_by=body.operator,
        idempotency_key=f"release:{level.value}:{body.scope_key}:{uuid.uuid4()}",
    )
    return {
        "status": "queued",
        "command_id": command.command_id,
        "escalation_level": level.value,
        "scope_key": body.scope_key,
    }


@app.get("/governance/post-mortem/{event_id}", dependencies=auth_dep)
def governance_post_mortem(event_id: str) -> dict[str, str]:
    from src.engine.audit_panel import generate_rollback_post_mortem

    report = generate_rollback_post_mortem(event_id)
    return {"event_id": event_id, "report_markdown": report}


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness check for systemd / load balancer."""
    return {
        "status": "ok",
        "supabase": get_supabase() is not None,
    }


@app.get("/status", dependencies=auth_dep)
def status() -> dict[str, Any]:
    """Local SQLite status for ops dashboard fallback."""
    runs = persistence.get_recent_bot_runs(1)
    latest = runs[0] if runs else None
    return {
        "latest_run": latest,
        "recent_errors": [
            e
            for e in persistence.get_system_events(10)
            if e.get("severity") in ("warning", "critical")
        ],
    }


@app.post("/reload", dependencies=auth_dep)
def reload_config() -> dict[str, str]:
    """Enqueue config reload for the trading-bot single writer."""
    command = enqueue_control_command(
        ControlCommandType.RELOAD_CONFIG,
        requested_by="CONTROL_API",
    )
    return {"status": "queued", "command_id": command.command_id}


@app.post("/backtest", dependencies=auth_dep)
async def trigger_backtest() -> dict[str, Any]:
    """Run backtest and optionally sync result to Supabase."""
    config = _config_watcher.get_latest()
    if not config.alpaca_api_key:
        raise HTTPException(400, "ALPACA_API_KEY not configured")

    result = await run_backtest(config)
    result_dict = backtest_result_to_dict(result)

    sync = SupabaseSync(SessionManager())
    run_id = sync.sync_backtest_result(
        _config_watcher.strategy_uuid,
        config.strategy.params,
        result_dict,
    )
    return {"run_id": run_id, **result_dict}


@app.post("/email/bod", dependencies=auth_dep)
async def trigger_bod_email() -> dict[str, str]:
    orch = TradingOrchestrator(load_config(), _config_watcher)
    await orch.run_bod()
    return {"status": "sent"}


@app.post("/email/eod", dependencies=auth_dep)
async def trigger_eod_email() -> dict[str, str]:
    orch = TradingOrchestrator(load_config(), _config_watcher)
    await orch.run_eod()
    return {"status": "sent"}


@app.post("/sessions/go-live", dependencies=auth_dep)
async def go_live(body: GoLiveRequest) -> dict[str, Any]:
    """Enqueue go-live for the trading-bot single writer."""
    if body.confirm != "GO LIVE":
        raise HTTPException(400, 'Confirmation must be exactly "GO LIVE"')

    config = _config_watcher.get_latest()
    if not config.alpaca_api_key:
        raise HTTPException(400, "Alpaca keys not configured")

    command = enqueue_control_command(
        ControlCommandType.GO_LIVE,
        payload={"strategy_id": body.strategy_id},
        requested_by="CONTROL_API",
    )
    return {
        "status": "queued",
        "command_id": command.command_id,
        "strategy_id": body.strategy_id,
    }


@app.post("/kill-switch", dependencies=auth_dep)
async def kill_switch(body: KillSwitchRequest) -> dict[str, str]:
    """Enqueue flatten-and-halt for the trading-bot single writer."""
    if body.confirm != "KILL":
        raise HTTPException(400, 'Confirmation must be exactly "KILL"')

    command = enqueue_control_command(
        ControlCommandType.FLATTEN_AND_HALT,
        requested_by="CONTROL_API",
    )
    return {"status": "queued", "command_id": command.command_id}


def set_orchestrator(orch: TradingOrchestrator) -> None:
    """Register the running orchestrator instance (called from main.py)."""
    global _orchestrator
    _orchestrator = orch


def main() -> None:
    """Run uvicorn for local dev: python -m api.main"""
    import uvicorn

    uvicorn.run(
        "api.main:app",
        host=os.getenv("CONTROL_API_HOST", "0.0.0.0"),
        port=int(os.getenv("CONTROL_API_PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    main()
