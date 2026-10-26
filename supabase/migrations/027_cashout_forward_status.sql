-- Migration 027 — cash-out forward module STATUS mirror (HEALTH, not just coverage).
--
-- The cashout_forward module (mbappe-cashout-forward.timer) wrote only droplet-local SQLite
-- (cf_filings/cf_positions/cf_bookings/cf_meta) and pushed NOTHING to Supabase, so it was invisible
-- on the dashboard in EVERY state — armed, disarmed, detecting, or silently broken. It sat
-- armed-and-empty from 2026-08-03 and only a forensic EDGAR check confirmed it was real-but-quiet,
-- not broken. This table gives it a dashboard-legible HEALTH signal so quiet-vs-broken is VISIBLE.
--
-- Singleton (id=1), upserted by the module after each poll (best-effort, additive — never affects
-- detection/parser/arming). last_run_result separates 'ok' (polled, found detections) / 'empty'
-- (polled, EDGAR returned 0 — quiet) / 'error' (run raised); "didn't run" is inferred by the
-- dashboard from a stale last_run_utc. RLS mirrors the engine_heartbeat pattern (015): dashboard_api_
-- node SELECT, service_role writes. Not a trading input; no money plane.

BEGIN;

CREATE TABLE IF NOT EXISTS cf_status (
    id                 INTEGER PRIMARY KEY,
    mode               TEXT,                 -- 'observe' | 'armed'
    last_poll_enddt    TEXT,                 -- EDGAR poll checkpoint (YYYY-MM-DD)
    detections_count   INTEGER,              -- cumulative cf_filings rows
    positions_count    INTEGER,              -- cumulative cf_positions rows
    last_run_utc       TIMESTAMPTZ,          -- when the module last ran (staleness -> "didn't run")
    last_run_result    TEXT,                 -- 'ok' | 'empty' | 'error' — the quiet-vs-broken signal
    parser_sha256      TEXT,                 -- frozen parser hash (parity vs the committed reference)
    parity_ok          BOOLEAN,
    updated_at         TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT cf_status_singleton CHECK (id = 1)
);

ALTER TABLE cf_status ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON cf_status TO dashboard_api_node;
GRANT SELECT ON cf_status TO authenticated;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON cf_status FROM authenticated, anon, dashboard_api_node;

DROP POLICY IF EXISTS "dashboard_api_node_cf_status_select" ON cf_status;
CREATE POLICY "dashboard_api_node_cf_status_select"
    ON cf_status FOR SELECT TO dashboard_api_node USING (true);

DROP POLICY IF EXISTS "authenticated_cf_status_select" ON cf_status;
CREATE POLICY "authenticated_cf_status_select"
    ON cf_status FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS "service_role_all_cf_status" ON cf_status;
CREATE POLICY "service_role_all_cf_status"
    ON cf_status FOR ALL TO service_role USING (true) WITH CHECK (true);

COMMIT;
