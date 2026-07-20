"""E3 (CP5-b) — the immutable-change-journal records the ACTUAL trigger source in triggered_by
(SYSTEM_AUTOMATIC vs HUMAN_OPERATOR), instead of hardcoding HUMAN_OPERATOR for every engagement."""
from __future__ import annotations

import sqlite3

import pytest

from src.engine.governance import (
    HumanOverrideRegistry,
    KillLevel,
    TriggeredBy,
    _actor_from_operator,
)


@pytest.fixture
def registry(tmp_path):
    return HumanOverrideRegistry(
        db_path=tmp_path / "research_vault.db",
        state_file=tmp_path / "circuit_breaker_state.json",
    )


def _latest_triggered_by(db_path, event_type):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT triggered_by FROM immutable_change_journal WHERE event_type = ? "
            "ORDER BY journal_id DESC LIMIT 1",
            (event_type,),
        ).fetchone()
    return row["triggered_by"] if row else None


def test_automatic_engage_is_system_automatic(registry):
    # The 2026-07-06 case: RISK_ESCALATION engaged PORTFOLIO_HALT — must be SYSTEM_AUTOMATIC.
    registry.engage(KillLevel.PORTFOLIO_HALT, operator="RISK_ESCALATION",
                    rationale="bar_freshness_critical")
    assert _latest_triggered_by(registry.db_path, "KILL_SWITCH_ENGAGED") == \
        TriggeredBy.SYSTEM_AUTOMATIC.value


def test_capacity_governor_engage_is_system_automatic(registry):
    registry.engage(KillLevel.STRATEGY_HALT, scope_key="mean_reversion_qqq",
                    operator="CAPACITY_GOVERNOR", rationale="turnover_halt")
    assert _latest_triggered_by(registry.db_path, "KILL_SWITCH_ENGAGED") == \
        TriggeredBy.SYSTEM_AUTOMATIC.value


def test_human_operator_engage_is_human(registry):
    registry.engage(KillLevel.PORTFOLIO_HALT, operator="killian@goautolane.com",
                    rationale="manual kill")
    assert _latest_triggered_by(registry.db_path, "KILL_SWITCH_ENGAGED") == \
        TriggeredBy.HUMAN_OPERATOR.value


def test_explicit_actor_overrides_derivation(registry):
    # An explicit actor wins over the operator-string heuristic.
    registry.engage(KillLevel.PORTFOLIO_HALT, operator="ambiguous-name",
                    rationale="x", actor=TriggeredBy.SYSTEM_AUTOMATIC)
    assert _latest_triggered_by(registry.db_path, "KILL_SWITCH_ENGAGED") == \
        TriggeredBy.SYSTEM_AUTOMATIC.value


def test_release_attribution_follows_source(registry):
    registry.engage(KillLevel.PORTFOLIO_HALT, operator="RISK_ESCALATION", rationale="x")
    registry.release(KillLevel.PORTFOLIO_HALT, operator="RISK_ESCALATION", rationale="recovered")
    assert _latest_triggered_by(registry.db_path, "KILL_SWITCH_RELEASED") == \
        TriggeredBy.SYSTEM_AUTOMATIC.value


def test_actor_from_operator_unit():
    assert _actor_from_operator("RISK_ESCALATION") == TriggeredBy.SYSTEM_AUTOMATIC
    assert _actor_from_operator("CAPACITY_GOVERNOR") == TriggeredBy.SYSTEM_AUTOMATIC
    assert _actor_from_operator("degradation_manager") == TriggeredBy.SYSTEM_AUTOMATIC
    assert _actor_from_operator("alice@example.com") == TriggeredBy.HUMAN_OPERATOR
    assert _actor_from_operator("") == TriggeredBy.HUMAN_OPERATOR
