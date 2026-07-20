"""Config immutability & hash pinning (LLD section 3).

A promoted leg's full effective configuration is serialized canonically and
SHA-256 pinned. The live loop asserts running-hash == pinned-hash at startup and
on reload; a mismatch forces SAFE_MODE (block new entries) and emits a
DiagnosticReport -- the running config is NEVER silently adopted (LLD L-C).

Canonicalization is stable under key order and float formatting so that a config
which is semantically identical always hashes identically (e.g. `{a: 2.30, b: 1}`
== `{b: 1, a: 2.3}`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from src.engine.engine_preemption import RiskEscalationEngine, RiskEscalationLevel


def _normalize(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return {str(k): _normalize(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, float):
        # normalize float representation (2.30 and 2.3 -> same); collapse -0.0
        norm = float(f"{value:.12g}")
        return 0.0 if norm == 0.0 else norm
    return value


def canonical_config_json(config: dict) -> str:
    """Canonical serialization: recursively sorted keys + normalized floats."""
    return json.dumps(_normalize(config), sort_keys=True, separators=(",", ":"))


def canonical_config_hash(config: dict) -> str:
    """SHA-256 of the canonical serialization (64-char hex)."""
    return hashlib.sha256(canonical_config_json(config).encode("utf-8")).hexdigest()


def pin_config_hash(config: dict) -> str:
    """Compute the hash to pin at promotion (stored in the registry)."""
    return canonical_config_hash(config)


def assert_running_hash(
    running_config: dict,
    pinned_hash: str,
    *,
    leg_id: str,
    phase: str = "startup",
    escalation: RiskEscalationEngine | None = None,
    report_sink: Callable[[dict], None] | None = None,
) -> bool:
    """Assert the running config matches the pinned hash. On mismatch: force
    SAFE_MODE (block new entries) + emit a DiagnosticReport; NEVER adopt the
    running config. Returns True on match, False on mismatch."""
    running_hash = canonical_config_hash(running_config)
    if running_hash == pinned_hash:
        return True
    if escalation is not None:
        escalation.transition(RiskEscalationLevel.ENTRY_GATE_HALT,
                              strategy_id=leg_id, commanded_by=f"config_hash_mismatch_{phase}")
    if report_sink is not None:
        report_sink({
            "kind": "config_hash_mismatch", "leg_id": leg_id, "phase": phase,
            "pinned_hash": pinned_hash, "running_hash": running_hash,
            "action": "SAFE_MODE", "adopted": False,
        })
    return False
