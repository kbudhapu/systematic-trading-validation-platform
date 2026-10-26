-- Migration 021 — leg_return_series (unitized per-leg return curve; L2 of the leg-return queue).
--
-- One row per leg per telemetry tick: indexed NAV (starts 100), cumulative return %, cumulative
-- dollar P&L, plus the unitization internals (units, capital_base) for auditability. Computed by
-- unitization in the bot's telemetry cycle (src/control/leg_return.py): capital reallocations
-- issue/redeem units at the CURRENT NAV — the curve is reallocation-neutral BY CONSTRUCTION
-- (asserted in tests/test_leg_return.py); only P&L moves NAV.
--
-- L1 verdict: UNITIZATION-VIABLE (docs/audits/leg_return_series_audit_2026-07-16.md).
-- Producer: sync_leg_attribution's telemetry site — allocation lives in-process
-- (coordination.risk_budgets), its only at-rest copy being an unmirrored droplet-local vault file.

BEGIN;

CREATE TABLE IF NOT EXISTS leg_return_series (
    id                     BIGSERIAL PRIMARY KEY,
    recorded_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    strategy_id            TEXT,                 -- strategy UUID (may be null if unresolved)
    leg_name               TEXT NOT NULL,        -- e.g. mean_reversion_qqq
    indexed_nav            DOUBLE PRECISION NOT NULL,  -- starts at 100
    cumulative_return_pct  DOUBLE PRECISION NOT NULL,  -- nav/100 - 1, in percent points
    dollar_pnl             DOUBLE PRECISION NOT NULL,  -- cumulative realized+unrealized $
    units                  DOUBLE PRECISION NOT NULL,  -- unitization internals (audit)
    capital_base           DOUBLE PRECISION NOT NULL,  -- budgets[leg] × portfolio equity at tick
    provenance             TEXT NOT NULL DEFAULT 'DERIVED'
);

CREATE INDEX IF NOT EXISTS idx_leg_return_series_leg_time
    ON leg_return_series (leg_name, recorded_at DESC);

ALTER TABLE leg_return_series ENABLE ROW LEVEL SECURITY;

-- RLS: dashboard users read-only; writes only by the bot (service_role / trading_bot_node lane).
GRANT SELECT ON leg_return_series TO authenticated;

-- GV-9 (2026-07-17, standing rule): every new-table migration ends with the EXPLICIT revoke —
-- Supabase's default privileges grant broad table rights to authenticated AND anon at CREATE
-- (the 001-blanket-grant class); RLS-with-no-policy contains them, but defense-in-depth demands
-- the grants not exist at all. Matches the live state for authenticated (operator revoked
-- 2026-07-16); the anon half was found still granted live during the GV-9 verification and is
-- reported for operator action (this file is the source of truth going forward).
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON leg_return_series FROM authenticated, anon;

DROP POLICY IF EXISTS "authenticated_leg_return_series_select" ON leg_return_series;
CREATE POLICY "authenticated_leg_return_series_select"
    ON leg_return_series FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS "service_role_all_leg_return_series" ON leg_return_series;
CREATE POLICY "service_role_all_leg_return_series"
    ON leg_return_series FOR ALL TO service_role USING (true) WITH CHECK (true);

COMMIT;
