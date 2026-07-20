-- Role isolation and least-privilege grants for trading bot and dashboard API nodes.

-- Replication / telemetry tables (created by bot WAL drain if absent).
CREATE TABLE IF NOT EXISTS live_attribution_ledger (
    attribution_id BIGSERIAL PRIMARY KEY,
    trade_id TEXT NOT NULL UNIQUE,
    timestamp TIMESTAMPTZ NOT NULL,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty DOUBLE PRECISION NOT NULL,
    pnl DOUBLE PRECISION NOT NULL,
    regime_id TEXT NOT NULL,
    session_type TEXT NOT NULL,
    liquidity_state TEXT NOT NULL,
    execution_tactic TEXT NOT NULL,
    champion_version_id INTEGER,
    ai_policy_execution_state TEXT NOT NULL,
    promotion_id TEXT,
    expected_price DOUBLE PRECISION,
    filled_price DOUBLE PRECISION,
    slippage_pct DOUBLE PRECISION,
    execution_direction_type TEXT,
    slip_direction_long_entry DOUBLE PRECISION,
    slip_direction_long_exit DOUBLE PRECISION,
    slip_direction_short_entry DOUBLE PRECISION,
    slip_direction_short_exit DOUBLE PRECISION,
    markout_5bar DOUBLE PRECISION,
    participation_cap_pct DOUBLE PRECISION,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_constraint_ledger (
    log_id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    cycle_id TEXT NOT NULL,
    constraint_type TEXT NOT NULL,
    strategy_id TEXT,
    symbol TEXT,
    action_taken TEXT NOT NULL,
    sizing_multiplier DOUBLE PRECISION,
    metadata_json TEXT NOT NULL,
    multi_day_net_inventory DOUBLE PRECISION,
    directional_entry_lockout TEXT,
    current_equity DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS dual_policy_shadow_log (
    log_id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    champion_id TEXT NOT NULL,
    challenger_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    session_type TEXT NOT NULL,
    regime_id TEXT NOT NULL,
    market_state_json JSONB NOT NULL DEFAULT '{}',
    champion_action TEXT NOT NULL,
    challenger_action TEXT NOT NULL,
    champion_capital DOUBLE PRECISION NOT NULL,
    challenger_capital DOUBLE PRECISION NOT NULL,
    champion_pnl DOUBLE PRECISION NOT NULL,
    challenger_pnl DOUBLE PRECISION NOT NULL,
    rules_baseline_pnl DOUBLE PRECISION NOT NULL,
    matched_capital_notional DOUBLE PRECISION NOT NULL,
    execution_path_json JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS dashboard_summary_snapshots (
    environment TEXT PRIMARY KEY,
    generated_at TIMESTAMPTZ NOT NULL,
    summary_json JSONB NOT NULL,
    performance_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS engine_heartbeat (
    environment TEXT PRIMARY KEY,
    last_successful_cycle_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_bot_node') THEN
        CREATE ROLE trading_bot_node NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_api_node') THEN
        CREATE ROLE dashboard_api_node NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO trading_bot_node, dashboard_api_node;

GRANT SELECT ON strategies TO trading_bot_node;
GRANT INSERT, SELECT ON live_attribution_ledger TO trading_bot_node;
GRANT INSERT, SELECT ON portfolio_constraint_ledger TO trading_bot_node;
GRANT INSERT, SELECT ON dual_policy_shadow_log TO trading_bot_node;
GRANT INSERT, SELECT, UPDATE ON dashboard_summary_snapshots TO trading_bot_node;
GRANT INSERT, SELECT, UPDATE ON engine_heartbeat TO trading_bot_node;
GRANT INSERT, SELECT ON control_commands TO trading_bot_node;
GRANT UPDATE ON control_commands TO trading_bot_node;

GRANT SELECT ON strategies TO dashboard_api_node;
GRANT SELECT ON dashboard_summary_snapshots TO dashboard_api_node;
GRANT SELECT ON engine_heartbeat TO dashboard_api_node;
GRANT SELECT ON live_attribution_ledger TO dashboard_api_node;
GRANT SELECT ON portfolio_constraint_ledger TO dashboard_api_node;
GRANT SELECT ON dual_policy_shadow_log TO dashboard_api_node;
GRANT SELECT, INSERT ON control_commands TO dashboard_api_node;

GRANT EXECUTE ON FUNCTION update_strategy_params_optimistic(UUID, JSONB, INTEGER)
    TO dashboard_api_node;
GRANT EXECUTE ON FUNCTION update_strategy_field_optimistic(UUID, TEXT, JSONB, INTEGER)
    TO dashboard_api_node;

GRANT EXECUTE ON FUNCTION update_strategy_params_optimistic(UUID, JSONB, INTEGER)
    TO trading_bot_node;
GRANT EXECUTE ON FUNCTION update_strategy_field_optimistic(UUID, TEXT, JSONB, INTEGER)
    TO trading_bot_node;

-- Tighten RLS: dashboard users read-only on strategies; writes via SECURITY DEFINER RPC.
DROP POLICY IF EXISTS "authenticated_all" ON strategies;
CREATE POLICY "authenticated_strategies_select"
    ON strategies
    FOR SELECT
    TO authenticated
    USING (true);

ALTER TABLE live_attribution_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolio_constraint_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE dual_policy_shadow_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE dashboard_summary_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE engine_heartbeat ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "authenticated_all" ON live_attribution_ledger;
DROP POLICY IF EXISTS "authenticated_all" ON portfolio_constraint_ledger;
DROP POLICY IF EXISTS "authenticated_all" ON dual_policy_shadow_log;
DROP POLICY IF EXISTS "authenticated_all" ON dashboard_summary_snapshots;

CREATE POLICY "authenticated_live_attribution_select"
    ON live_attribution_ledger
    FOR SELECT
    TO authenticated
    USING (true);

CREATE POLICY "authenticated_portfolio_constraint_select"
    ON portfolio_constraint_ledger
    FOR SELECT
    TO authenticated
    USING (true);

CREATE POLICY "authenticated_dual_policy_shadow_select"
    ON dual_policy_shadow_log
    FOR SELECT
    TO authenticated
    USING (true);

CREATE POLICY "authenticated_dashboard_summary_select"
    ON dashboard_summary_snapshots
    FOR SELECT
    TO authenticated
    USING (true);

CREATE POLICY "authenticated_engine_heartbeat_select"
    ON engine_heartbeat
    FOR SELECT
    TO authenticated
    USING (true);

CREATE POLICY "trading_bot_node_live_attribution_insert"
    ON live_attribution_ledger
    FOR INSERT
    TO trading_bot_node
    WITH CHECK (true);

CREATE POLICY "trading_bot_node_live_attribution_select"
    ON live_attribution_ledger
    FOR SELECT
    TO trading_bot_node
    USING (true);

CREATE POLICY "trading_bot_node_portfolio_constraint_insert"
    ON portfolio_constraint_ledger
    FOR INSERT
    TO trading_bot_node
    WITH CHECK (true);

CREATE POLICY "trading_bot_node_portfolio_constraint_select"
    ON portfolio_constraint_ledger
    FOR SELECT
    TO trading_bot_node
    USING (true);

CREATE POLICY "trading_bot_node_dual_policy_shadow_insert"
    ON dual_policy_shadow_log
    FOR INSERT
    TO trading_bot_node
    WITH CHECK (true);

CREATE POLICY "trading_bot_node_dual_policy_shadow_select"
    ON dual_policy_shadow_log
    FOR SELECT
    TO trading_bot_node
    USING (true);

CREATE POLICY "trading_bot_node_dashboard_summary_write"
    ON dashboard_summary_snapshots
    FOR ALL
    TO trading_bot_node
    USING (true)
    WITH CHECK (true);

CREATE POLICY "trading_bot_node_engine_heartbeat_write"
    ON engine_heartbeat
    FOR ALL
    TO trading_bot_node
    USING (true)
    WITH CHECK (true);

CREATE POLICY "dashboard_api_node_dashboard_summary_select"
    ON dashboard_summary_snapshots
    FOR SELECT
    TO dashboard_api_node
    USING (true);

CREATE POLICY "dashboard_api_node_strategies_select"
    ON strategies
    FOR SELECT
    TO dashboard_api_node
    USING (true);

CREATE POLICY "authenticated_control_commands_insert"
    ON control_commands
    FOR INSERT
    TO authenticated
    WITH CHECK (true);

CREATE POLICY "authenticated_control_commands_select"
    ON control_commands
    FOR SELECT
    TO authenticated
    USING (true);

CREATE POLICY "dashboard_api_node_control_commands_insert"
    ON control_commands
    FOR INSERT
    TO dashboard_api_node
    WITH CHECK (true);

CREATE POLICY "dashboard_api_node_control_commands_select"
    ON control_commands
    FOR SELECT
    TO dashboard_api_node
    USING (true);

CREATE POLICY "trading_bot_node_control_commands_all"
    ON control_commands
    FOR ALL
    TO trading_bot_node
    USING (true)
    WITH CHECK (true);

-- Service role retains full access for migrations and break-glass operations.
CREATE POLICY "service_role_all_live_attribution"
    ON live_attribution_ledger
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE POLICY "service_role_all_portfolio_constraint"
    ON portfolio_constraint_ledger
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE POLICY "service_role_all_dual_policy_shadow"
    ON dual_policy_shadow_log
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE POLICY "service_role_all_dashboard_summary"
    ON dashboard_summary_snapshots
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE POLICY "service_role_all_engine_heartbeat"
    ON engine_heartbeat
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

COMMENT ON ROLE trading_bot_node IS
    'Least-privilege VPS bot role: INSERT/SELECT on execution logging tables; read-only strategies.';

COMMENT ON ROLE dashboard_api_node IS
    'Least-privilege Vercel dashboard role: optimistic RPC mutators and snapshot reads.';

-- PostgREST JWT role assumption (custom API keys minted for each node).
GRANT trading_bot_node TO authenticator;
GRANT dashboard_api_node TO authenticator;

-- Realtime push for dashboard telemetry (replaces SSE polling).
ALTER TABLE dashboard_summary_snapshots REPLICA IDENTITY FULL;
ALTER TABLE engine_heartbeat REPLICA IDENTITY FULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_publication_tables
        WHERE pubname = 'supabase_realtime'
          AND schemaname = 'public'
          AND tablename = 'dashboard_summary_snapshots'
    ) THEN
        ALTER PUBLICATION supabase_realtime ADD TABLE dashboard_summary_snapshots;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_publication_tables
        WHERE pubname = 'supabase_realtime'
          AND schemaname = 'public'
          AND tablename = 'engine_heartbeat'
    ) THEN
        ALTER PUBLICATION supabase_realtime ADD TABLE engine_heartbeat;
    END IF;
END
$$;

-- Authenticated dashboard users: read-only on strategies (writes via SECURITY DEFINER RPC).
DROP POLICY IF EXISTS "authenticated_strategies_insert" ON strategies;
DROP POLICY IF EXISTS "authenticated_strategies_update" ON strategies;
DROP POLICY IF EXISTS "authenticated_strategies_delete" ON strategies;

-- Dashboard API node may execute optimistic strategy mutators only (no direct table DML).
REVOKE INSERT, UPDATE, DELETE ON strategies FROM dashboard_api_node;
