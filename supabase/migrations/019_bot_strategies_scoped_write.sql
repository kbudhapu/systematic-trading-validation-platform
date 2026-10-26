-- Migration 019 — trading_bot_node scoped UPDATE on strategies (unblocks service-key removal).
--
-- CP7 G-A3 / D4 blocker: removing the droplet service_role key (which BYPASSES RLS) requires the
-- bot's own engine-path strategy writers to keep working as trading_bot_node. Those writers touch
-- ONLY three columns (verified against live code):
--   • flatten-disable  src/engine/orchestrator.py:1163  → {"enabled": false}
--   • go-live          src/control/session_manager.py:154 → {"environment": "live", "updated_at"}
-- (write_remote_strategy_update in config_engine.py:567 is a generic writer with ZERO callers —
--  unwired. If params-promotion is ever wired it must go through a parity-audited SECURITY DEFINER
--  RPC with write-time parity (CP7 G-C2), NOT a params grant. The column grant below FENCES it: a
--  params UPDATE by trading_bot_node is column-privilege-denied by Postgres.)
--
-- params is intentionally NOT grantable to any non-service_role role — a strategy PARAMS write must
-- pass ConfigurationParityAuditor (write-time parity, CP7 G-C2), never a blanket grant.
--
-- APPLY ORDER: 015 → 016 → 017 → 018 → 019. validate_migration.py exits 2 on DDL — expected.

BEGIN;

-- Clear any blanket UPDATE (defensive; trading_bot_node held no UPDATE on strategies before this).
REVOKE UPDATE ON strategies FROM trading_bot_node;

-- Column-scoped UPDATE: enabled + environment + updated_at ONLY. params/version_id/etc. excluded,
-- so a params write via trading_bot_node is rejected at the column-privilege layer.
GRANT UPDATE (enabled, environment, updated_at) ON strategies TO trading_bot_node;

-- RLS UPDATE policy (the role also needs a matching policy, not just the grant). USING(true) so the
-- bot may update any strategy row it owns operationally; WITH CHECK constrains the resulting
-- environment to the known set (rejects a write that would set an out-of-band environment).
DROP POLICY IF EXISTS "trading_bot_node_strategies_update" ON strategies;
CREATE POLICY "trading_bot_node_strategies_update"
    ON strategies
    FOR UPDATE
    TO trading_bot_node
    USING (true)
    WITH CHECK (environment IN ('backtest', 'paper', 'live'));

COMMIT;
