"""Twin-signature / interface-drift contract — the kill_level and payload/artifact_json twins.

Generalizes the guard first written for the log_system_event twin (GV-8 / Finding B, pinned in
tests/test_log_system_event_contract.py after the live 2026-07-16 ConnectionTerminated incident:
an error handler that crashed on a drifted kwarg converted network noise into portfolio state).
That was the THIRD interface-drift catch; this file pins the other two paired interfaces so the
same divergence class cannot recur silently:

  * kill_level  — the CLI (`kill_level: str`) hands off to the engine registry
    (`kill_level: KillLevel`); the CLI converts with `KillLevel(kill_level.upper())`, so a new or
    non-upper enum value would break the hand-off at runtime. Pinned: param name shared, every
    enum value round-trips through the CLI conversion, engage/release agree on the caller params.

  * payload / artifact_json — the SAME artifact dict is written to the local vault column
    `payload_json` (persist_evidence_artifact) and to the Supabase column `artifact_json`
    (publish_experiment_artifact). A rename on either end silently breaks the publish bridge.
    Pinned: the local persister's caller param is `payload` and it stores `payload_json`; the
    publisher writes the artifact under `artifact_json` on table `experiment_artifacts`.
"""
from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

from src.cli.commands import cmd_governance_kill, cmd_governance_release
from src.engine.governance import HumanOverrideRegistry, KillLevel
from src.persistence import research_evidence_store as res
import scripts.publish_experiment_artifact as pub


# ═══════════════════════════════════════════════════════════════════════════════
# kill_level twin — CLI (str) ↔ engine registry (KillLevel enum)
# ═══════════════════════════════════════════════════════════════════════════════
_KILL_CALLER_PARAMS = {"kill_level", "scope_key", "operator", "rationale"}


def test_kill_level_param_name_is_shared_across_the_twin():
    """Both ends name the parameter `kill_level` — a rename on either side breaks the hand-off."""
    for fn in (cmd_governance_kill, cmd_governance_release):
        assert "kill_level" in inspect.signature(fn).parameters, f"{fn.__name__} lost kill_level"
    for method in (HumanOverrideRegistry.engage, HumanOverrideRegistry.release):
        assert "kill_level" in inspect.signature(method).parameters, f"{method.__name__} lost kill_level"


def test_engage_and_release_agree_on_the_caller_params():
    engage = set(inspect.signature(HumanOverrideRegistry.engage).parameters) - {"self"}
    release = set(inspect.signature(HumanOverrideRegistry.release).parameters) - {"self"}
    assert _KILL_CALLER_PARAMS <= engage, f"engage missing {_KILL_CALLER_PARAMS - engage}"
    assert _KILL_CALLER_PARAMS <= release, f"release missing {_KILL_CALLER_PARAMS - release}"


def test_every_kill_level_round_trips_through_the_cli_conversion():
    """The CLI receives a string and does `KillLevel(kill_level.upper())`. Every enum value must
    survive that conversion — a lower/mixed-case value (e.g. 'AiHalt') would raise at runtime and
    crash the kill command, exactly the drift class this file guards."""
    assert set(KillLevel)  # non-empty
    for lvl in KillLevel:
        assert lvl.value == lvl.value.upper(), f"{lvl.name} value is not upper-case canonical"
        assert KillLevel(lvl.value.upper()) is lvl, f"{lvl.name} does not round-trip via .upper()"


# ═══════════════════════════════════════════════════════════════════════════════
# payload / artifact_json twin — local vault column ↔ Supabase column
# ═══════════════════════════════════════════════════════════════════════════════
def test_local_persister_takes_payload_and_stores_payload_json(tmp_path: Path):
    """The vault end: caller param is `payload`; it persists to the `payload_json` column."""
    assert "payload" in inspect.signature(res.persist_evidence_artifact).parameters
    db = tmp_path / "ev.db"
    rid = res.persist_evidence_artifact(
        experiment_id="EXP-TWIN", trial_key="t0", artifact_type="mcpt_null",
        evidence_class="original", payload={"null_max": 0.7, "k": [1, 2, 3]}, db_path=db,
    )
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT payload_json FROM research_evidence_artifacts WHERE id=?", (rid,)
        ).fetchone()
    assert json.loads(row[0]) == {"null_max": 0.7, "k": [1, 2, 3]}


class _FakeTable:
    def __init__(self, name, captured):
        captured["table"] = name
        self._c = captured

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def insert(self, row):
        self._c["insert"] = row
        return self

    def execute(self):
        return type("R", (), {"data": []})()


class _FakeClient:
    def __init__(self, captured):
        self._c = captured

    def table(self, name):
        return _FakeTable(name, self._c)


def test_publisher_writes_the_artifact_under_artifact_json(monkeypatch):
    """The Supabase end: the built artifact rides into the `artifact_json` column of
    `experiment_artifacts` — NOT `payload_json` or a bare `artifact`. Behavioral, so a column
    rename on either the publisher or the schema is caught."""
    captured: dict = {}
    art = {"experiment_id": "EXP-TWIN", "verdict": "PASS", "criteria_md": "criteria",
           "trials": [], "psd": None, "kind": "cal_flow"}
    monkeypatch.setattr(pub, "load_from_vault", lambda db: [art])
    monkeypatch.setattr(pub, "build_meta_analytics", lambda arts: {**art, "experiment_id": "META"})
    monkeypatch.setattr(pub, "_service_client", lambda: _FakeClient(captured))

    rc = pub.main(["--only", "EXP-TWIN"])
    assert rc == 0
    assert captured["table"] == "experiment_artifacts"
    row = captured["insert"]
    assert "artifact_json" in row, "publisher no longer writes the artifact_json column"
    assert "payload_json" not in row and "artifact" not in row, "column name drifted"
    assert row["artifact_json"] == art, "the vault artifact must ride into artifact_json unchanged"
