-- Migration 018 — experiment artifacts (dashboard-v2 D5 / N4).
--
-- The ONLY substrate the /experiments dashboard page may render. Append-only + versioned:
-- a published artifact is immutable; a re-run publishes a NEW version (never an UPDATE).
-- Mirrors the immutable_change_journal append-only pattern (src/engine/governance.py:52-79).
-- The dashboard renders STRICTLY the keys documented in docs/experiment_artifact_schema.md v1.
--
-- APPLY ORDER: 015 → 016 → 017 → 018. validate_migration.py exits 2 on DDL — expected.

BEGIN;

CREATE TABLE IF NOT EXISTS experiment_artifacts (
    id               BIGSERIAL PRIMARY KEY,
    experiment_id    TEXT NOT NULL,
    version          INTEGER NOT NULL,
    verdict          TEXT,                       -- PASS | PASS-FRAGILE | REJECTED | SHELVED | null
    criteria_sha256  TEXT NOT NULL,              -- sha256 of the pre-registered criteria block
    artifact_json    JSONB NOT NULL,             -- the schema-v1 keys the dashboard renders
    published_by     TEXT,
    published_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (experiment_id, version)
);

CREATE INDEX IF NOT EXISTS idx_experiment_artifacts_latest
    ON experiment_artifacts (experiment_id, version DESC);

-- Append-only enforcement: no UPDATE, no DELETE (immutable_change_journal pattern).
CREATE OR REPLACE FUNCTION experiment_artifacts_no_mutate()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'experiment_artifacts is append-only (publish a new version instead)';
END;
$$;

DROP TRIGGER IF EXISTS experiment_artifacts_no_update ON experiment_artifacts;
CREATE TRIGGER experiment_artifacts_no_update
    BEFORE UPDATE ON experiment_artifacts
    FOR EACH ROW EXECUTE FUNCTION experiment_artifacts_no_mutate();

DROP TRIGGER IF EXISTS experiment_artifacts_no_delete ON experiment_artifacts;
CREATE TRIGGER experiment_artifacts_no_delete
    BEFORE DELETE ON experiment_artifacts
    FOR EACH ROW EXECUTE FUNCTION experiment_artifacts_no_mutate();

ALTER TABLE experiment_artifacts ENABLE ROW LEVEL SECURITY;

-- RLS: dashboard users read-only; writes only by service_role (the publish script).
GRANT SELECT ON experiment_artifacts TO authenticated;

DROP POLICY IF EXISTS "authenticated_experiment_artifacts_select" ON experiment_artifacts;
CREATE POLICY "authenticated_experiment_artifacts_select"
    ON experiment_artifacts FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS "service_role_all_experiment_artifacts" ON experiment_artifacts;
CREATE POLICY "service_role_all_experiment_artifacts"
    ON experiment_artifacts FOR ALL TO service_role USING (true) WITH CHECK (true);

COMMIT;
