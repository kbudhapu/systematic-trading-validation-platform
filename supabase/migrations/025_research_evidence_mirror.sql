-- Migration 025 — Research evidence mirror tables (research_evidence / research_equity_curves).
--
-- The READ-ONLY Supabase mirror of the research evidence vault (data/research_vault.db) that the
-- Experiments detail tab renders: MCPT null artifacts + DiagnosticReport summaries
-- (research_evidence, unified, discriminated by evidence_kind) and per-experiment equity/drawdown
-- curves (research_equity_curves). A best-effort mirror upserts these after research runs
-- (src/research/evidence_mirror.py + scripts/backfill_evidence_mirror.py). NOTHING here is a
-- trading input; the vault stays the source of truth.
--
-- CONTRACT-LOCKED to docs/research/EVIDENCE_MIRROR_SCHEMA.md (PR #272). The column set + types
-- below are EXACTLY the machine-readable block in that doc; tests/test_evidence_schema_contract.py
-- parses the block and asserts this CREATE TABLE equals it field-for-field — the cross-repo drift
-- guard. Do NOT hand-edit a column here without regenerating from the block, or the build fails.
--
-- family / gate_threshold are intentionally ABSENT: no source field exists in the vault (ruling in
-- #272); emitting them would NULL-fill every row and break the contract test.
--
-- source_row_id is the vault row id and the UPSERT KEY (append-only preservation): a trial's
-- array_deferred and present generations are distinct vault ids -> two rows, never collapsed. It is
-- NOT (experiment_id, trial_key). DiagnosticReports use the NEGATIVE of their id so the report id
-- space cannot collide with artifact ids inside the unified BIGINT PK.
--
-- Append-only for the browser: authenticated may only SELECT; all writes are by the bot
-- (service_role). Each table ENDS with the explicit GV-9 revoke — Supabase grants broad table
-- rights to authenticated AND anon at CREATE (the 001-blanket-grant class); RLS with no write
-- policy contains them, but defense-in-depth demands the grants not exist at all.

BEGIN;

-- ---- research_evidence: unified MCPT nulls + DiagnosticReport summaries ----
CREATE TABLE IF NOT EXISTS research_evidence (
    source_row_id   BIGINT PRIMARY KEY,               -- vault id (reports: -report.id); upsert key
    evidence_kind   TEXT NOT NULL,                    -- 'mcpt_null' | 'diagnostic_report'
    experiment_id   TEXT NOT NULL,
    trial_key       TEXT NOT NULL,
    evidence_class  TEXT,                             -- 'original' | 'replication' (null for reports)
    array_state     TEXT,                             -- 'array_deferred' | 'present' (null for reports)
    seed            BIGINT,
    n_perm          INTEGER,
    exceedance_k    INTEGER,                          -- payload.exceedance_count_k
    stored_p        DOUBLE PRECISION,
    observed_stat   DOUBLE PRECISION,                 -- payload.observed (present rows only)
    null_max        DOUBLE PRECISION,                 -- payload.null_max (present, where emitted)
    disposition     TEXT,                             -- report.verdict (null for mcpt_null)
    stage           TEXT,                             -- report.stage (null for mcpt_null)
    generator_ref   TEXT,                             -- recipe.generator_module@generator_commit
    data_hash       TEXT,                             -- payload.data.fingerprint
    artifact_hash   TEXT,                             -- store hash of the whole payload
    schema_version  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    null_array      JSONB                             -- payload.null (present rows only)
);
CREATE INDEX IF NOT EXISTS idx_research_evidence_experiment ON research_evidence (experiment_id);
CREATE INDEX IF NOT EXISTS idx_research_evidence_kind ON research_evidence (evidence_kind);

-- ---- research_equity_curves: one row per equity_curve artifact ----
CREATE TABLE IF NOT EXISTS research_equity_curves (
    source_row_id   BIGINT PRIMARY KEY,               -- vault id; upsert key
    experiment_id   TEXT NOT NULL,
    trial_key       TEXT NOT NULL,
    evidence_class  TEXT,
    series_kind     TEXT,
    n_trades        INTEGER,
    series_json     JSONB NOT NULL,                   -- payload.equity_cumsum (verbatim)
    drawdown_json   JSONB NOT NULL,                   -- payload.drawdown (verbatim)
    max_drawdown    DOUBLE PRECISION,
    generator_ref   TEXT,
    artifact_hash   TEXT,
    schema_version  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_equity_curves_experiment ON research_equity_curves (experiment_id);

-- ---- RLS: authenticated SELECT-only; writes only by the bot (service_role) ----
ALTER TABLE research_evidence      ENABLE ROW LEVEL SECURITY;
ALTER TABLE research_equity_curves ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON research_evidence      TO authenticated;
GRANT SELECT ON research_equity_curves TO authenticated;

-- GV-9 standing rule: explicit revoke of every write grant from authenticated AND anon.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON research_evidence      FROM authenticated, anon;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON research_equity_curves FROM authenticated, anon;

DROP POLICY IF EXISTS "authenticated_research_evidence_select" ON research_evidence;
CREATE POLICY "authenticated_research_evidence_select"
    ON research_evidence FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS "service_role_all_research_evidence" ON research_evidence;
CREATE POLICY "service_role_all_research_evidence"
    ON research_evidence FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "authenticated_research_equity_curves_select" ON research_equity_curves;
CREATE POLICY "authenticated_research_equity_curves_select"
    ON research_equity_curves FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS "service_role_all_research_equity_curves" ON research_equity_curves;
CREATE POLICY "service_role_all_research_equity_curves"
    ON research_equity_curves FOR ALL TO service_role USING (true) WITH CHECK (true);

COMMIT;
