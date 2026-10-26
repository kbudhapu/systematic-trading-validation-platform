-- Migration 017 — leg attribution snapshots (CP7 / dashboard-v2 D4; supersedes the
-- MIXED equity_snapshots leg rows found in dashboard audit A1/F4).
--
-- The old sync_leg_equity_snapshot wrote DERIVED leg "equity" (baseline+realized+unrealized,
-- fictional cash) into the SAME equity_snapshots table as broker-truth portfolio rows,
-- distinguished only by session_id (A1 MIXED-table hazard). This migration introduces a
-- dedicated, honestly-typed attribution table and RETIRES the leg-equity write path (the
-- Python side is removed in the same D4 commit; portfolio sync_equity_snapshot stays).
--
-- Attribution contract (enforced by sync_leg_attribution, not the DB):
--   realized_pnl          = SUM(pnl) from live_attribution_ledger for the leg (NEVER trades).
--   unrealized_pnl        = broker position unrealized attributed to the leg via the
--                           strategy-tagged client-order-id chain.
--   unattributed_residual = unrealized that could NOT be unambiguously attributed — kept
--                           separate, NEVER guessed into a leg.
--
-- APPLY ORDER: 015 → 016 → 017 → 018. validate_migration.py exits 2 on DDL — expected.

BEGIN;

CREATE TABLE IF NOT EXISTS leg_attribution_snapshots (
    id                     BIGSERIAL PRIMARY KEY,
    recorded_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    strategy_id            TEXT NOT NULL,
    leg_name               TEXT NOT NULL,
    symbol                 TEXT NOT NULL,
    realized_pnl           DOUBLE PRECISION NOT NULL,   -- from live_attribution_ledger
    unrealized_pnl         DOUBLE PRECISION NOT NULL,   -- attributed broker unrealized
    unattributed_residual  DOUBLE PRECISION NOT NULL DEFAULT 0,  -- ambiguous, never guessed
    position_qty           DOUBLE PRECISION NOT NULL,
    provenance             TEXT NOT NULL,               -- e.g. LEDGER_REALIZED+BROKER_UNREALIZED
    source_ts              TIMESTAMPTZ                  -- broker/ledger as-of time
);

CREATE INDEX IF NOT EXISTS idx_leg_attribution_strategy_recent
    ON leg_attribution_snapshots (strategy_id, recorded_at DESC);

ALTER TABLE leg_attribution_snapshots ENABLE ROW LEVEL SECURITY;

-- RLS: dashboard users read-only; the bot (trading_bot_node) inserts + reads; service_role all.
GRANT SELECT              ON leg_attribution_snapshots TO authenticated;
GRANT SELECT, INSERT      ON leg_attribution_snapshots TO trading_bot_node;
GRANT USAGE, SELECT       ON SEQUENCE leg_attribution_snapshots_id_seq TO trading_bot_node;

DROP POLICY IF EXISTS "authenticated_leg_attribution_select" ON leg_attribution_snapshots;
CREATE POLICY "authenticated_leg_attribution_select"
    ON leg_attribution_snapshots FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS "trading_bot_node_leg_attribution_insert" ON leg_attribution_snapshots;
CREATE POLICY "trading_bot_node_leg_attribution_insert"
    ON leg_attribution_snapshots FOR INSERT TO trading_bot_node WITH CHECK (true);
DROP POLICY IF EXISTS "trading_bot_node_leg_attribution_select" ON leg_attribution_snapshots;
CREATE POLICY "trading_bot_node_leg_attribution_select"
    ON leg_attribution_snapshots FOR SELECT TO trading_bot_node USING (true);
DROP POLICY IF EXISTS "service_role_all_leg_attribution" ON leg_attribution_snapshots;
CREATE POLICY "service_role_all_leg_attribution"
    ON leg_attribution_snapshots FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ─────────────────────────────────────────────────────────────────────────────
-- End ALL active performance_sessions.
-- Dashboard audit A3 found 6 simultaneously-active sessions all baselined in the June
-- test window (2026-06-23…06-28), distorting every displayed pct_return. Ending them here
-- means the next portfolio sync calls ensure_session with NO active session → a FRESH
-- portfolio session is created seeded at CURRENT broker equity (the `equity` arg), so
-- returns are measured from a real, current baseline. Verified: src/control/session_manager.py
-- ensure_session() creates a new active session at the passed-in equity when none is active
-- (cited in the D4 PR); portfolio sync_equity_snapshot passes live broker equity.
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE performance_sessions SET is_active = false WHERE is_active = true;

COMMIT;
