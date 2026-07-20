"""Permanent fence against RPC↔engine payload-contract drift (C5 finding / migration 020).

The `enqueue_control_command` RPC (SQL) and the engine's `parse_escalation_level` (Python) MUST agree
on BOTH the payload key (`escalation_level`) and the accepted value set (RiskEscalationLevel). This
test parses the RPC's whitelist out of the migration file and compares it to the engine module, so a
change to either side ALONE fails CI — which is exactly what let the original mismatch reach prod.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from src.engine.engine_preemption import RiskEscalationLevel, parse_escalation_level

MIGRATION = (
    pathlib.Path(__file__).resolve().parent.parent
    / "supabase" / "migrations" / "020_command_payload_contract.sql"
)


def _rpc_escalation_whitelist(sql: str) -> set[str]:
    """Extract the v_escalation_levels ARRAY[...] literal from the RPC source."""
    m = re.search(r"v_escalation_levels\s+text\[\]\s*:=\s*ARRAY\[(.*?)\]", sql, re.DOTALL)
    assert m, "could not find v_escalation_levels ARRAY[...] in migration 020"
    return {tok.strip().strip("'") for tok in m.group(1).split(",") if tok.strip()}


def test_rpc_whitelist_equals_engine_escalation_enum():
    rpc = _rpc_escalation_whitelist(MIGRATION.read_text(encoding="utf-8"))
    engine = {lvl.value for lvl in RiskEscalationLevel}
    assert rpc == engine, f"RPC whitelist {rpc} != engine RiskEscalationLevel {engine}"


def test_every_rpc_whitelisted_value_is_accepted_by_parse_escalation_level():
    for value in _rpc_escalation_whitelist(MIGRATION.read_text(encoding="utf-8")):
        # parse_escalation_level is the engine's authoritative reader — it must accept each.
        assert parse_escalation_level({"escalation_level": value}) == RiskEscalationLevel(value)


def test_parse_escalation_level_rejects_a_non_member_and_the_retired_key():
    with pytest.raises(ValueError):
        parse_escalation_level({"escalation_level": "PORTFOLIO_HALT"})  # a KillLevel, not an escalation level
    with pytest.raises(ValueError):
        parse_escalation_level({"kill_level": "PORTFOLIO_HALT"})        # retired key → no escalation_level


def test_rpc_keys_on_escalation_level_not_the_retired_kill_level():
    sql = MIGRATION.read_text(encoding="utf-8")
    # validates the canonical key
    assert "p_payload ? 'escalation_level'" in sql
    # must NOT validate/read the retired payload key
    assert "p_payload ? 'kill_level'" not in sql
    assert "p_payload->>'kill_level'" not in sql
