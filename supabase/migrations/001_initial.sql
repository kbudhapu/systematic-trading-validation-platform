-- Trading bot dashboard schema (run in Supabase SQL editor)

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Strategies (source of truth for bot config)
CREATE TABLE IF NOT EXISTS strategies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    module TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL DEFAULT '15Min',
    enabled BOOLEAN NOT NULL DEFAULT false,
    environment TEXT NOT NULL DEFAULT 'paper' CHECK (environment IN ('backtest', 'paper', 'live')),
    params JSONB NOT NULL DEFAULT '{}',
    poll_interval_seconds INT NOT NULL DEFAULT 900,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Email / reporting
CREATE TABLE IF NOT EXISTS reporting_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_to TEXT NOT NULL DEFAULT '',
    email_from TEXT NOT NULL DEFAULT '',
    bod_hour_et INT NOT NULL DEFAULT 9,
    eod_hour_et INT NOT NULL DEFAULT 16,
    email_enabled BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Performance sessions (paper vs live chart baselines)
CREATE TABLE IF NOT EXISTS performance_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id UUID REFERENCES strategies(id) ON DELETE CASCADE,
    environment TEXT NOT NULL CHECK (environment IN ('paper', 'live')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    baseline_equity NUMERIC NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_performance_sessions_active
    ON performance_sessions (strategy_id, is_active) WHERE is_active = true;

-- Equity snapshots for Fidelity-style % chart
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES performance_sessions(id) ON DELETE CASCADE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    equity NUMERIC NOT NULL,
    cash NUMERIC NOT NULL DEFAULT 0,
    pct_return NUMERIC NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_equity_snapshots_session_time
    ON equity_snapshots (session_id, recorded_at DESC);

-- Trades
CREATE TABLE IF NOT EXISTS trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id UUID REFERENCES strategies(id) ON DELETE SET NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    qty NUMERIC NOT NULL,
    entry_price NUMERIC,
    exit_price NUMERIC,
    pnl NUMERIC,
    status TEXT NOT NULL DEFAULT 'filled'
);

CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades (timestamp DESC);

-- Order blotter
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id UUID REFERENCES strategies(id) ON DELETE SET NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty NUMERIC NOT NULL,
    order_type TEXT NOT NULL DEFAULT 'market',
    status TEXT NOT NULL DEFAULT 'pending',
    reject_reason TEXT,
    expected_price NUMERIC,
    filled_price NUMERIC,
    slippage_bps NUMERIC,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    filled_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_orders_submitted ON orders (submitted_at DESC);

-- Bot operational runs (heartbeat)
CREATE TABLE IF NOT EXISTS bot_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id UUID REFERENCES strategies(id) ON DELETE SET NULL,
    environment TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    cycle_ms INT,
    equity NUMERIC,
    drawdown_pct NUMERIC,
    halted BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bot_runs_created ON bot_runs (created_at DESC);

-- Backtest results
CREATE TABLE IF NOT EXISTS backtest_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id UUID REFERENCES strategies(id) ON DELETE SET NULL,
    params JSONB NOT NULL DEFAULT '{}',
    sharpe NUMERIC,
    max_drawdown NUMERIC,
    total_return NUMERIC,
    total_trades INT,
    win_rate NUMERIC,
    profit_factor NUMERIC,
    equity_curve JSONB,
    passed BOOLEAN NOT NULL DEFAULT false,
    flags JSONB DEFAULT '[]',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- System events (errors, halts, go-live)
CREATE TABLE IF NOT EXISTS system_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_system_events_created ON system_events (created_at DESC);

-- Config audit
CREATE TABLE IF NOT EXISTS config_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID,
    table_name TEXT NOT NULL,
    old_value JSONB,
    new_value JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Risk state (circuit breaker, peak equity)
CREATE TABLE IF NOT EXISTS risk_state (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id UUID REFERENCES strategies(id) ON DELETE CASCADE,
    peak_equity NUMERIC NOT NULL DEFAULT 0,
    halted BOOLEAN NOT NULL DEFAULT false,
    halt_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed default strategy + reporting (idempotent)
INSERT INTO strategies (name, module, symbol, timeframe, enabled, environment, params, poll_interval_seconds)
SELECT
    'mean_reversion_spy',
    'mean_reversion_spy',
    'SPY',
    '15Min',
    false,
    'paper',
    '{"sma_period": 55, "threshold_sigma": 2.5, "exit_sigma": 0.4}'::jsonb,
    900
WHERE NOT EXISTS (SELECT 1 FROM strategies WHERE name = 'mean_reversion_spy');

INSERT INTO reporting_settings (email_to, email_from, bod_hour_et, eod_hour_et, email_enabled)
SELECT '', '', 9, 16, false
WHERE NOT EXISTS (SELECT 1 FROM reporting_settings);

-- RLS: enable and allow authenticated users full access (single-user dashboard)
ALTER TABLE strategies ENABLE ROW LEVEL SECURITY;
ALTER TABLE reporting_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE performance_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE equity_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE config_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE risk_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY "authenticated_all" ON strategies FOR ALL TO authenticated USING (true) WITH CHECK (true);
CREATE POLICY "authenticated_all" ON reporting_settings FOR ALL TO authenticated USING (true) WITH CHECK (true);
CREATE POLICY "authenticated_all" ON performance_sessions FOR ALL TO authenticated USING (true) WITH CHECK (true);
CREATE POLICY "authenticated_all" ON equity_snapshots FOR ALL TO authenticated USING (true) WITH CHECK (true);
CREATE POLICY "authenticated_all" ON trades FOR ALL TO authenticated USING (true) WITH CHECK (true);
CREATE POLICY "authenticated_all" ON orders FOR ALL TO authenticated USING (true) WITH CHECK (true);
CREATE POLICY "authenticated_all" ON bot_runs FOR ALL TO authenticated USING (true) WITH CHECK (true);
CREATE POLICY "authenticated_all" ON backtest_runs FOR ALL TO authenticated USING (true) WITH CHECK (true);
CREATE POLICY "authenticated_all" ON system_events FOR ALL TO authenticated USING (true) WITH CHECK (true);
CREATE POLICY "authenticated_all" ON config_audit FOR ALL TO authenticated USING (true) WITH CHECK (true);
CREATE POLICY "authenticated_all" ON risk_state FOR ALL TO authenticated USING (true) WITH CHECK (true);

-- Service role bypasses RLS (used by VPS bot sync)
