"""Cross-repo drift guard: migration 025 CREATE TABLE == the #272 machine block.

The dashboard's research_evidence / research_equity_curves migration is contract-locked to the
machine-readable block in docs/research/EVIDENCE_MIRROR_SCHEMA.md (PR #272). If the mirror's
column contract and this migration ever disagree — a renamed column, a changed type, an added or
dropped field, a flipped NOT NULL — the build fails HERE, before a mismatched table can be applied.

Parses both sides independently (the doc's JSON block, and the SQL DDL) and asserts equality
column-name, pg-type, and NOT NULL, in order.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCHEMA_DOC = REPO / "docs" / "research" / "EVIDENCE_MIRROR_SCHEMA.md"
MIGRATION = REPO / "supabase" / "migrations" / "025_research_evidence_mirror.sql"

_PG_TYPES = ("BIGINT", "DOUBLE PRECISION", "TIMESTAMPTZ", "INTEGER", "JSONB", "TEXT")


def _load_block() -> dict:
    md = SCHEMA_DOC.read_text(encoding="utf-8")
    anchor = md.split('name="machine-readable-contract"', 1)
    assert len(anchor) == 2, "machine-readable-contract anchor missing"
    m = re.search(r"```json\s*(\{.*?\})\s*```", anchor[1], re.DOTALL)
    assert m, "no json fence under the machine-readable-contract heading"
    return json.loads(m.group(1))


def _parse_create_table(sql: str, table: str) -> list[tuple[str, str, bool]]:
    """Return [(column, pg_type, nullable)] from a CREATE TABLE body, in declared order.

    Skips constraint / index lines; normalizes `col TYPE PRIMARY KEY` -> NOT NULL (a PK is NOT
    NULL by definition, matching how the block marks source_row_id)."""
    m = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);",
        sql, re.DOTALL | re.IGNORECASE,
    )
    assert m, f"CREATE TABLE {table} not found in the migration"
    out: list[tuple[str, str, bool]] = []
    for raw in m.group(1).splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        col = line.split()[0]
        rest = line[len(col):].strip()
        # strip an inline comment
        rest = rest.split("--", 1)[0].strip()
        # find the pg type (longest match first so 'DOUBLE PRECISION' beats nothing)
        pg_type = next((t for t in _PG_TYPES if rest.upper().startswith(t)), None)
        if pg_type is None:
            continue  # not a column line (e.g. a table constraint)
        tail = rest[len(pg_type):].upper()
        nullable = ("NOT NULL" not in tail) and ("PRIMARY KEY" not in tail)
        out.append((col, pg_type, nullable))
    return out


@pytest.mark.parametrize("table", ["research_evidence", "research_equity_curves"])
def test_migration_matches_contract_block(table):
    block = _load_block()[table]
    sql = _parse_create_table(MIGRATION.read_text(encoding="utf-8"), table)

    block_cols = [(c["column"], c["pg_type"], c["nullable"]) for c in block]

    # same columns, same order
    assert [c[0] for c in sql] == [c[0] for c in block_cols], (
        f"{table}: column set/order drift\n  migration: {[c[0] for c in sql]}\n"
        f"  contract : {[c[0] for c in block_cols]}"
    )
    # same type + nullability, per column
    for (sc, st, sn), (bc, bt, bn) in zip(sql, block_cols):
        assert st == bt, f"{table}.{sc}: type {st!r} != contract {bt!r}"
        assert sn == bn, f"{table}.{sc}: nullable {sn} != contract {bn}"


def test_forbidden_columns_absent_from_migration():
    """family / gate_threshold must never be COLUMNS (ruling: no source, would NULL-fill).

    Checks parsed column names, not raw text — the words appear in an explanatory comment on
    purpose, which is fine; a column definition is not."""
    sql = MIGRATION.read_text(encoding="utf-8")
    defined = set()
    for table in ("research_evidence", "research_equity_curves"):
        defined |= {c[0].lower() for c in _parse_create_table(sql, table)}
    for col in ("family", "gate_threshold"):
        assert col not in defined, f"{col} is a column (out by #272 ruling)"


def test_source_row_id_is_the_primary_key_not_experiment_trial():
    """Append-only preservation depends on the PK being source_row_id."""
    sql = MIGRATION.read_text(encoding="utf-8")
    for table in ("research_evidence", "research_equity_curves"):
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);", sql, re.DOTALL)
        body = m.group(1)
        assert re.search(r"source_row_id\s+BIGINT\s+PRIMARY KEY", body), (
            f"{table}: source_row_id must be the BIGINT PRIMARY KEY"
        )
        assert "PRIMARY KEY (experiment_id" not in body, (
            f"{table}: must NOT key on experiment_id+trial_key (would collapse generations)"
        )


def test_gv9_explicit_revoke_present():
    """Every mirror table ends with the explicit GV-9 revoke from authenticated AND anon."""
    sql = MIGRATION.read_text(encoding="utf-8")
    for table in ("research_evidence", "research_equity_curves"):
        assert re.search(
            rf"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {table}\s+FROM authenticated, anon;",
            sql,
        ), f"{table}: missing GV-9 explicit revoke"
        # authenticated gets SELECT only
        assert re.search(rf"GRANT SELECT ON {table}\s+TO authenticated;", sql)
        assert f"service_role_all_{table}" in sql
