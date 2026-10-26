-- Single-writer command bus for control-plane → trading-bot coordination.

CREATE TABLE IF NOT EXISTS control_commands (
    command_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    command_type TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    requested_by TEXT,
    idempotency_key TEXT UNIQUE,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_control_commands_pending
    ON control_commands (created_at ASC)
    WHERE status = 'pending';

ALTER TABLE control_commands ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_all_control_commands"
    ON control_commands
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
