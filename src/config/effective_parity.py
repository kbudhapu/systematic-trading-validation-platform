"""RA-1 (FIX-2): effective-runtime-config parity attestation.

`ConfigurationParityAuditor` validates the FILE-derived config; the bot actually runs
`ConfigWatcher.get_latest()` -- the MERGE of local yaml + Supabase overrides + runtime
overrides. Nothing asserted that effective-runtime-config == audited-file-config (this is
what made C1 Step 0 read the FILE `max_drawdown_pct`, unable to confirm the effective value).

This emits, at boot, a deterministic hash + the guardrail values of the EFFECTIVE config with
enablement-source attribution -- so future audits quote runtime, not files -- and surfaces any
divergence from the audited file config via the operator alert store. It is ADDITIVE and
NON-FATAL: a mismatch logs + alerts, it never blocks boot.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

import structlog

log = structlog.get_logger()


def _canonical(config) -> dict:
    """Deterministic material view of a resolved config (stable key order)."""
    strategies = {
        s.strategy_id: {
            "enabled": bool(s.enabled),
            "symbol": s.symbol,
            "timeframe": s.timeframe,
            "asset_class": getattr(s, "asset_class", None),
            "params": s.params,
        }
        for s in (config.strategies or [])
    }
    return {
        "environment": config.environment,
        "strategies": strategies,
        "risk_max_drawdown_pct": getattr(getattr(config, "risk", None), "max_drawdown_pct", None),
    }


def _hash(canon: dict) -> str:
    return hashlib.sha256(json.dumps(canon, sort_keys=True, default=str).encode()).hexdigest()


def _guardrails(config) -> dict:
    """Guardrail values AS THEY APPEAR IN THE EFFECTIVE CONFIG (runtime, not files)."""
    per_leg = {
        s.strategy_id: {
            "regime_filter": s.params.get("regime_filter"),
            "borrow_drag_coefficient": s.params.get("borrow_drag_coefficient"),
        }
        for s in (config.strategies or []) if s.enabled
    }
    return {
        "risk_max_drawdown_pct": getattr(getattr(config, "risk", None), "max_drawdown_pct", None),
        "per_leg": per_leg,
    }


def _enablement_attribution(effective, audited) -> dict:
    """For each ENABLED leg in the effective config: does it match the audited (file)
    charter (local_charter) or diverge (remote_*)? -- makes a mismatch attributable."""
    audited_by_id = {s.strategy_id: s for s in (audited.strategies or [])}
    out: dict[str, str] = {}
    for s in (effective.strategies or []):
        if not s.enabled:
            continue
        a = audited_by_id.get(s.strategy_id)
        if a is None:
            out[s.strategy_id] = "remote_only"          # enabled at runtime, absent from file
        elif not a.enabled:
            out[s.strategy_id] = "remote_enabled"       # file disables it, runtime enabled it
        elif a.params != s.params:
            out[s.strategy_id] = "remote_param_override"  # same leg, params differ
        else:
            out[s.strategy_id] = "local_charter"        # matches the file
    return out


def emit_effective_config_parity(
    effective, audited, *, alert_sink: Callable[[dict], Any] | None = None
) -> dict:
    """Log the effective-config attestation and, on divergence vs the audited file config,
    log a warning + emit a non-fatal operator alert. Returns a summary dict (for tests)."""
    eff_canon, aud_canon = _canonical(effective), _canonical(audited)
    eff_hash, aud_hash = _hash(eff_canon), _hash(aud_canon)
    match = eff_hash == aud_hash
    guardrails = _guardrails(effective)
    attribution = _enablement_attribution(effective, audited)
    log.info(
        "effective_config_parity",
        effective_config_hash=eff_hash,
        audited_file_config_hash=aud_hash,
        match=match,
        guardrails=guardrails,
        enablement=attribution,
    )
    if not match:
        diverging = {sid: src for sid, src in attribution.items() if src != "local_charter"}
        eff_s, aud_s = eff_canon["strategies"], aud_canon["strategies"]
        field_diffs = {
            k: {"effective": eff_s.get(k), "audited": aud_s.get(k)}
            for k in sorted(set(eff_s) | set(aud_s)) if eff_s.get(k) != aud_s.get(k)
        }
        detail = {
            "effective_config_hash": eff_hash,
            "audited_file_config_hash": aud_hash,
            "diverging_sources": diverging,
            "field_diffs": field_diffs,
            "guardrails": guardrails,
        }
        log.warning("effective_config_parity_mismatch", **detail)
        if alert_sink is not None:
            alert_sink({
                "kind": "config_parity_mismatch",
                "severity": "warning",
                "message": ("Effective runtime config diverges from the audited file config "
                            f"(sources: {diverging or 'value_drift'})."),
                "detail": detail,
            })
    return {
        "match": match,
        "effective_config_hash": eff_hash,
        "audited_file_config_hash": aud_hash,
        "guardrails": guardrails,
        "enablement": attribution,
    }
