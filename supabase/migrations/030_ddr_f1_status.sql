-- Migration 030 — DDR-F1 shakedown STATUS mirror (HEALTH, not just presence).
--
-- The DDR-F1 daily runner (mbappe DDR-F1 timer, first session 2026-08-12 09:25 ET) writes only
-- local state (ddr_f1_state.json, ddr_f1_health.log, ddr_f1_decision_log.db) and pushes NOTHING to
-- Supabase — so, exactly like cash-out before migration 027, it is invisible on the dashboard in
-- EVERY state: armed-and-waiting, ran-ok, ran-with-no-fills, or errored. This table gives it a
-- dashboard-legible HEALTH signal so quiet-vs-broken is VISIBLE — critical here because of the known
-- pre-open-pull risk that could produce a silent ran-no-fills on the first live session.
--
-- Singleton (id=1), upserted by the runner after each session AND on the halt branch (additive,
-- best-effort — NEVER affects the runner's trading / kill-switch / sizing / timer logic; a pure
-- status read+push). last_run_result separates the health states:
--   'armed_waiting' — armed, timer live, no session has run yet (the initial push)
--   'ran_ok'        — session ran, orders submitted AND fills captured
--   'ran_no_fills'  — session ran, orders submitted but 0 fills (the quiet-vs-broken signal;
--                     the pre-open-pull risk manifests here)
--   'errored'       — the run raised
--   'halted'        — kill switch fired (DDR_F1_HALT=1 or HALT file); no submissions
-- "didn't run" is inferred by the dashboard from a stale last_run_utc.
--
-- RLS mirrors cf_status (027) / engine_heartbeat (015) EXACTLY: dashboard_api_node + authenticated
-- SELECT, service_role writes, all other writes REVOKED. Paper-only shakedown; NOT a trading input;
-- no money plane. Displays DEPLOYED reality: allocation $25,000 / gross cap $21,250 (85%) from the
-- AllocationEnvelope — NOT the $30k in the mirror spec (that spec-vs-deployed drift is a flagged
-- finding routed to the DDR lane, not reconciled here).

BEGIN;

CREATE TABLE IF NOT EXISTS ddr_f1_status (
    id                        INTEGER PRIMARY KEY,
    mode                      TEXT,          -- 'armed' | 'dry-run' | 'observe'
    kill_switch_active        BOOLEAN,       -- DDR_F1_HALT=1 or HALT file present at run time
    last_run_result           TEXT,          -- armed_waiting | ran_ok | ran_no_fills | errored | halted
    next_session              TEXT,          -- next scheduled session (date, best-effort)
    last_session              TEXT,          -- last session run (date); null before first session
    last_run_utc              TIMESTAMPTZ,   -- when the runner last ran (staleness -> "didn't run")
    orders_submitted          INTEGER,       -- last session: orders accepted (null pre-first-session)
    fills_captured            INTEGER,       -- last session: fills captured
    max_deviation_bps         NUMERIC,       -- last session: max fill-vs-NBBO-mid deviation
    deviation_gate_bps        NUMERIC,       -- the frozen fill-deviation gate (context for the above)
    shakedown_session_count   INTEGER,       -- distinct sessions in the decision log (toward shakedown)
    allocation_usd            NUMERIC,       -- DEPLOYED allocation ($25,000) — from AllocationEnvelope
    gross_cap_usd             NUMERIC,       -- DEPLOYED gross cap ($21,250 = 85%)
    first_live_session        TEXT,          -- from ddr_f1_state.json
    registration_sha          TEXT,          -- frozen-registration hash (freeze parity, like cf parser sha)
    updated_at                TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT ddr_f1_status_singleton CHECK (id = 1)
);

ALTER TABLE ddr_f1_status ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON ddr_f1_status TO dashboard_api_node;
GRANT SELECT ON ddr_f1_status TO authenticated;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ddr_f1_status FROM authenticated, anon, dashboard_api_node;

DROP POLICY IF EXISTS "dashboard_api_node_ddr_f1_status_select" ON ddr_f1_status;
CREATE POLICY "dashboard_api_node_ddr_f1_status_select"
    ON ddr_f1_status FOR SELECT TO dashboard_api_node USING (true);

DROP POLICY IF EXISTS "authenticated_ddr_f1_status_select" ON ddr_f1_status;
CREATE POLICY "authenticated_ddr_f1_status_select"
    ON ddr_f1_status FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS "service_role_all_ddr_f1_status" ON ddr_f1_status;
CREATE POLICY "service_role_all_ddr_f1_status"
    ON ddr_f1_status FOR ALL TO service_role USING (true) WITH CHECK (true);

COMMIT;
