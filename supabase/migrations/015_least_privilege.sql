-- Migration 015 — least-privilege data plane (CP1/CP7 F1/F2, unfreeze Gate A)
--
-- Closes the two by-design holes CP1_grant_matrix.md found and live pg_policies
-- confirmed (drift=NONE):
--   F2: migration 001 granted `authenticated_all FOR ALL … WITH CHECK(true)` on 11
--       original tables; migration 012 revoked it on strategies + 4 tables only,
--       leaving 10 money/telemetry/risk tables browser-writable by any logged-in JWT.
--   F1: `authenticated` (and `dashboard_api_node`) could INSERT control_commands
--       directly (raw PostgREST bypasses every confirm string). Migration 016 adds
--       the SECURITY DEFINER enqueue RPC; here we remove the direct-INSERT policies so
--       the ONLY direct writer of control_commands is trading_bot_node (+ service_role,
--       + the 016 definer owner via the RPC).
--
-- SPEC of record: docs/audit/control_plane/CP1_grant_matrix.md §1–§2 (every
-- write-capable `authenticated` row dies here) and CP7_findings.md Gate A / D-migration-1.
--
-- Also (CP1-e / CP7 G-A3): the scoped node roles designed in 012 have several
-- GRANT-without-POLICY rows (RLS denies a grant that has no matching policy). Added
-- below so that when 012's role isolation is actually turned on (D3 + operator mints
-- the trading_bot_node JWT and removes the service_role key), the roles can operate.
--
-- APPLY ORDER: 015 → 016 → 017 → 018. Operator applies (see PR checklist).
-- validate_migration.py exits 2 on DDL — expected for a pure-DDL migration.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- SECTION A — authenticated → SELECT-only on every 001-era table.
-- Drop the blanket FOR ALL policy, revoke table-level DML, add a SELECT policy.
-- anon gets nothing (it never had a policy; revoke any inherited grant for hygiene).
-- ─────────────────────────────────────────────────────────────────────────────

-- equity_snapshots (broker-truth equity — must never be browser-forgeable)
DROP POLICY IF EXISTS "authenticated_all" ON equity_snapshots;
REVOKE INSERT, UPDATE, DELETE ON equity_snapshots FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON equity_snapshots FROM anon;
DROP POLICY IF EXISTS "authenticated_equity_snapshots_select" ON equity_snapshots;
CREATE POLICY "authenticated_equity_snapshots_select"
    ON equity_snapshots FOR SELECT TO authenticated USING (true);

-- trades
DROP POLICY IF EXISTS "authenticated_all" ON trades;
REVOKE INSERT, UPDATE, DELETE ON trades FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON trades FROM anon;
DROP POLICY IF EXISTS "authenticated_trades_select" ON trades;
CREATE POLICY "authenticated_trades_select"
    ON trades FOR SELECT TO authenticated USING (true);

-- orders
DROP POLICY IF EXISTS "authenticated_all" ON orders;
REVOKE INSERT, UPDATE, DELETE ON orders FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON orders FROM anon;
DROP POLICY IF EXISTS "authenticated_orders_select" ON orders;
CREATE POLICY "authenticated_orders_select"
    ON orders FOR SELECT TO authenticated USING (true);

-- bot_runs (heartbeat)
DROP POLICY IF EXISTS "authenticated_all" ON bot_runs;
REVOKE INSERT, UPDATE, DELETE ON bot_runs FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON bot_runs FROM anon;
DROP POLICY IF EXISTS "authenticated_bot_runs_select" ON bot_runs;
CREATE POLICY "authenticated_bot_runs_select"
    ON bot_runs FOR SELECT TO authenticated USING (true);

-- risk_state (kill-state mirror — dead-read today, F9 latent trap; lock it anyway)
DROP POLICY IF EXISTS "authenticated_all" ON risk_state;
REVOKE INSERT, UPDATE, DELETE ON risk_state FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON risk_state FROM anon;
DROP POLICY IF EXISTS "authenticated_risk_state_select" ON risk_state;
CREATE POLICY "authenticated_risk_state_select"
    ON risk_state FOR SELECT TO authenticated USING (true);

-- system_events (audit trail — must not be rewritable by a dashboard user)
DROP POLICY IF EXISTS "authenticated_all" ON system_events;
REVOKE INSERT, UPDATE, DELETE ON system_events FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON system_events FROM anon;
DROP POLICY IF EXISTS "authenticated_system_events_select" ON system_events;
CREATE POLICY "authenticated_system_events_select"
    ON system_events FOR SELECT TO authenticated USING (true);

-- config_audit (audit trail)
DROP POLICY IF EXISTS "authenticated_all" ON config_audit;
REVOKE INSERT, UPDATE, DELETE ON config_audit FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON config_audit FROM anon;
DROP POLICY IF EXISTS "authenticated_config_audit_select" ON config_audit;
CREATE POLICY "authenticated_config_audit_select"
    ON config_audit FOR SELECT TO authenticated USING (true);

-- performance_sessions (return baselines)
DROP POLICY IF EXISTS "authenticated_all" ON performance_sessions;
REVOKE INSERT, UPDATE, DELETE ON performance_sessions FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON performance_sessions FROM anon;
DROP POLICY IF EXISTS "authenticated_performance_sessions_select" ON performance_sessions;
CREATE POLICY "authenticated_performance_sessions_select"
    ON performance_sessions FOR SELECT TO authenticated USING (true);

-- backtest_runs
DROP POLICY IF EXISTS "authenticated_all" ON backtest_runs;
REVOKE INSERT, UPDATE, DELETE ON backtest_runs FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON backtest_runs FROM anon;
DROP POLICY IF EXISTS "authenticated_backtest_runs_select" ON backtest_runs;
CREATE POLICY "authenticated_backtest_runs_select"
    ON backtest_runs FOR SELECT TO authenticated USING (true);

-- reporting_settings (dashboard email settings; browser UPDATE (EmailClient.tsx:39)
-- is removed — v2 renders email settings read-only / server-routed, N6)
DROP POLICY IF EXISTS "authenticated_all" ON reporting_settings;
REVOKE INSERT, UPDATE, DELETE ON reporting_settings FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON reporting_settings FROM anon;
DROP POLICY IF EXISTS "authenticated_reporting_settings_select" ON reporting_settings;
CREATE POLICY "authenticated_reporting_settings_select"
    ON reporting_settings FOR SELECT TO authenticated USING (true);

-- strategies: already SELECT-only for authenticated since 012 (authenticated_strategies_select).
-- Belt-and-suspenders revoke of any inherited table DML so the policy is not the only gate.
REVOKE INSERT, UPDATE, DELETE ON strategies FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON strategies FROM anon;

-- ─────────────────────────────────────────────────────────────────────────────
-- SECTION B — control_commands: remove every direct-INSERT policy except the bot's.
-- authenticated + dashboard_api_node lose direct INSERT here; the ONLY sanctioned
-- dashboard enqueue path becomes the 016 SECURITY DEFINER RPC (granted to
-- dashboard_api_node EXECUTE only). trading_bot_node keeps ALL; service_role keeps ALL.
-- authenticated keeps SELECT (to render command history).
-- ─────────────────────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS "authenticated_control_commands_insert" ON control_commands;
DROP POLICY IF EXISTS "dashboard_api_node_control_commands_insert" ON control_commands;
REVOKE INSERT, UPDATE, DELETE ON control_commands FROM authenticated;
REVOKE INSERT ON control_commands FROM dashboard_api_node;
REVOKE INSERT, UPDATE, DELETE ON control_commands FROM anon;
-- (authenticated_control_commands_select and dashboard_api_node_control_commands_select
--  remain — read-only visibility is retained.)

-- ─────────────────────────────────────────────────────────────────────────────
-- SECTION C — close CP1-e GRANT-without-POLICY gaps (SELECT reads the roles need).
-- ─────────────────────────────────────────────────────────────────────────────
-- trading_bot_node needs to read strategies (has GRANT SELECT 012:95, no policy).
DROP POLICY IF EXISTS "trading_bot_node_strategies_select" ON strategies;
CREATE POLICY "trading_bot_node_strategies_select"
    ON strategies FOR SELECT TO trading_bot_node USING (true);

-- dashboard_api_node has GRANT SELECT on these (012:106-109) but no policy → denied.
DROP POLICY IF EXISTS "dashboard_api_node_live_attribution_select" ON live_attribution_ledger;
CREATE POLICY "dashboard_api_node_live_attribution_select"
    ON live_attribution_ledger FOR SELECT TO dashboard_api_node USING (true);
DROP POLICY IF EXISTS "dashboard_api_node_portfolio_constraint_select" ON portfolio_constraint_ledger;
CREATE POLICY "dashboard_api_node_portfolio_constraint_select"
    ON portfolio_constraint_ledger FOR SELECT TO dashboard_api_node USING (true);
DROP POLICY IF EXISTS "dashboard_api_node_dual_policy_shadow_select" ON dual_policy_shadow_log;
CREATE POLICY "dashboard_api_node_dual_policy_shadow_select"
    ON dual_policy_shadow_log FOR SELECT TO dashboard_api_node USING (true);
DROP POLICY IF EXISTS "dashboard_api_node_engine_heartbeat_select" ON engine_heartbeat;
CREATE POLICY "dashboard_api_node_engine_heartbeat_select"
    ON engine_heartbeat FOR SELECT TO dashboard_api_node USING (true);

-- ─────────────────────────────────────────────────────────────────────────────
-- SECTION D — trading_bot_node telemetry WRITE policies.
-- REQUIRED for CP7 G-A3: the bot writes these tables (src/control/supabase_sync.py
-- + session_manager.py). Today it does so as service_role (BYPASSRLS). When the
-- operator mints the trading_bot_node JWT and removes the service key (D3 / T-FINAL),
-- the bot MUST still be able to write its own telemetry, or the least-privilege plane
-- is inoperable. These grants are to a NOLOGIN server role — NOT browser-reachable —
-- so they are not a re-opening of the F1/F2 surface. Scope = exactly what the writers do.
-- ─────────────────────────────────────────────────────────────────────────────
GRANT SELECT, INSERT         ON system_events        TO trading_bot_node; -- supabase_sync.py:38
GRANT SELECT, INSERT, UPDATE ON orders               TO trading_bot_node; -- :59,:99,:108,:144
GRANT SELECT, INSERT         ON trades               TO trading_bot_node; -- :120
GRANT SELECT, INSERT         ON bot_runs             TO trading_bot_node; -- :177
GRANT SELECT, INSERT         ON equity_snapshots     TO trading_bot_node; -- :212
GRANT SELECT, INSERT, UPDATE ON risk_state           TO trading_bot_node; -- :297,:301
GRANT SELECT, INSERT         ON backtest_runs        TO trading_bot_node; -- :327
GRANT SELECT, INSERT, UPDATE ON performance_sessions TO trading_bot_node; -- session_manager.py:121,:149,:189

DROP POLICY IF EXISTS "trading_bot_node_system_events_write" ON system_events;
CREATE POLICY "trading_bot_node_system_events_write"
    ON system_events FOR INSERT TO trading_bot_node WITH CHECK (true);
DROP POLICY IF EXISTS "trading_bot_node_system_events_select" ON system_events;
CREATE POLICY "trading_bot_node_system_events_select"
    ON system_events FOR SELECT TO trading_bot_node USING (true);

DROP POLICY IF EXISTS "trading_bot_node_orders_insert" ON orders;
CREATE POLICY "trading_bot_node_orders_insert"
    ON orders FOR INSERT TO trading_bot_node WITH CHECK (true);
DROP POLICY IF EXISTS "trading_bot_node_orders_update" ON orders;
CREATE POLICY "trading_bot_node_orders_update"
    ON orders FOR UPDATE TO trading_bot_node USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "trading_bot_node_orders_select" ON orders;
CREATE POLICY "trading_bot_node_orders_select"
    ON orders FOR SELECT TO trading_bot_node USING (true);

DROP POLICY IF EXISTS "trading_bot_node_trades_insert" ON trades;
CREATE POLICY "trading_bot_node_trades_insert"
    ON trades FOR INSERT TO trading_bot_node WITH CHECK (true);
DROP POLICY IF EXISTS "trading_bot_node_trades_select" ON trades;
CREATE POLICY "trading_bot_node_trades_select"
    ON trades FOR SELECT TO trading_bot_node USING (true);

DROP POLICY IF EXISTS "trading_bot_node_bot_runs_insert" ON bot_runs;
CREATE POLICY "trading_bot_node_bot_runs_insert"
    ON bot_runs FOR INSERT TO trading_bot_node WITH CHECK (true);
DROP POLICY IF EXISTS "trading_bot_node_bot_runs_select" ON bot_runs;
CREATE POLICY "trading_bot_node_bot_runs_select"
    ON bot_runs FOR SELECT TO trading_bot_node USING (true);

DROP POLICY IF EXISTS "trading_bot_node_equity_snapshots_insert" ON equity_snapshots;
CREATE POLICY "trading_bot_node_equity_snapshots_insert"
    ON equity_snapshots FOR INSERT TO trading_bot_node WITH CHECK (true);
DROP POLICY IF EXISTS "trading_bot_node_equity_snapshots_select" ON equity_snapshots;
CREATE POLICY "trading_bot_node_equity_snapshots_select"
    ON equity_snapshots FOR SELECT TO trading_bot_node USING (true);

DROP POLICY IF EXISTS "trading_bot_node_risk_state_insert" ON risk_state;
CREATE POLICY "trading_bot_node_risk_state_insert"
    ON risk_state FOR INSERT TO trading_bot_node WITH CHECK (true);
DROP POLICY IF EXISTS "trading_bot_node_risk_state_update" ON risk_state;
CREATE POLICY "trading_bot_node_risk_state_update"
    ON risk_state FOR UPDATE TO trading_bot_node USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "trading_bot_node_risk_state_select" ON risk_state;
CREATE POLICY "trading_bot_node_risk_state_select"
    ON risk_state FOR SELECT TO trading_bot_node USING (true);

DROP POLICY IF EXISTS "trading_bot_node_backtest_runs_insert" ON backtest_runs;
CREATE POLICY "trading_bot_node_backtest_runs_insert"
    ON backtest_runs FOR INSERT TO trading_bot_node WITH CHECK (true);
DROP POLICY IF EXISTS "trading_bot_node_backtest_runs_select" ON backtest_runs;
CREATE POLICY "trading_bot_node_backtest_runs_select"
    ON backtest_runs FOR SELECT TO trading_bot_node USING (true);

DROP POLICY IF EXISTS "trading_bot_node_performance_sessions_insert" ON performance_sessions;
CREATE POLICY "trading_bot_node_performance_sessions_insert"
    ON performance_sessions FOR INSERT TO trading_bot_node WITH CHECK (true);
DROP POLICY IF EXISTS "trading_bot_node_performance_sessions_update" ON performance_sessions;
CREATE POLICY "trading_bot_node_performance_sessions_update"
    ON performance_sessions FOR UPDATE TO trading_bot_node USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "trading_bot_node_performance_sessions_select" ON performance_sessions;
CREATE POLICY "trading_bot_node_performance_sessions_select"
    ON performance_sessions FOR SELECT TO trading_bot_node USING (true);

-- ─────────────────────────────────────────────────────────────────────────────
-- BLOCKER (recorded per hard-stop; NOT resolved here — engine/strategy-write path
-- is owned by the companion queue and strategy writes are FROZEN):
--   The bot also UPDATEs `strategies` as a SAFETY / lifecycle action:
--     orchestrator.py:1163 (flatten-disable: SET enabled=false),
--     session_manager.py:153 (go-live env flip), config_engine.py:617 (promotion).
--   trading_bot_node is intentionally NOT granted UPDATE on strategies here (that would
--   partly reopen the frozen strategy-write surface, and the decision is the companion
--   queue's). CONSEQUENCE: removing the service_role key (T-FINAL) BEFORE a companion-queue
--   ruling grants trading_bot_node scoped UPDATE(enabled,environment,params) on strategies
--   will break flatten-disable + promotion. Until then the bot must retain service_role,
--   OR a follow-up migration grants that scoped UPDATE. See D3 (warn-not-crash fallback).
-- ─────────────────────────────────────────────────────────────────────────────

COMMIT;
