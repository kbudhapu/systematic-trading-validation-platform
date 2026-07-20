-- Migration 020 — ENGAGE/RELEASE payload contract: escalation_level (kill_level dies).
--
-- C5 drill (closure) found a control-plane payload-contract mismatch: the D2 enqueue RPC (016)
-- validated/keyed ENGAGE/RELEASE on `kill_level`, but the engine's emergency handler reads
-- `escalation_level` (src/engine/engine_preemption.py::parse_escalation_level, :55-60, consumed at
-- src/control/emergency_command_listener.py:107 and src/engine/orchestrator.py:1412). A kill issued
-- through the sanctioned RPC was claimed (E2 bus) but parsed to NOMINAL → no engage. This makes
-- `escalation_level` the canonical key on BOTH sides.
--
-- The value whitelist below is copied VERBATIM from the RiskEscalationLevel enum
-- (src/engine/engine_preemption.py:22-25) — the exact accepted set of parse_escalation_level.
-- tests/test_command_payload_contract.py asserts set-equality + key-name identity against the engine
-- module so the RPC and engine can never drift apart silently.
--
-- CREATE OR REPLACE preserves the command_enqueue_owner ownership (016) and the EXECUTE ACL, but we
-- RE-ASSERT the least-privilege ACL explicitly (the 016 lesson: Supabase default privileges auto-grant
-- EXECUTE to authenticated + anon and that survives a FROM-PUBLIC revoke).

CREATE OR REPLACE FUNCTION public.enqueue_control_command(
    p_command_type   text,
    p_payload        jsonb,
    p_confirm        text,
    p_requested_by   text,
    p_idempotency_key text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    -- KEEP IN SYNC with src/control/command_queue.py :: ControlCommandType
    v_allowed_types text[] := ARRAY[
        'FLATTEN_AND_HALT','GO_LIVE','ENGAGE_KILL_SWITCH',
        'RELEASE_KILL_SWITCH','RELOAD_CONFIG','PING'
    ];
    -- VERBATIM from src/engine/engine_preemption.py:22-25 (RiskEscalationLevel enum);
    -- this is exactly the set parse_escalation_level(:55-60) accepts.
    v_escalation_levels text[] := ARRAY[
        'NOMINAL','ENTRY_GATE_HALT','STRATEGY_LIQUIDATE','GLOBAL_FLATTEN_AND_HALT'
    ];
    v_required_confirm text;
    v_command_id uuid;
BEGIN
    IF p_command_type IS NULL OR p_command_type <> ALL (v_allowed_types) THEN
        RAISE EXCEPTION 'enqueue_control_command: unknown command_type %', p_command_type
            USING ERRCODE = '22023';
    END IF;

    v_required_confirm := CASE p_command_type
        WHEN 'FLATTEN_AND_HALT'     THEN 'KILL'
        WHEN 'ENGAGE_KILL_SWITCH'   THEN 'ENGAGE'
        WHEN 'RELEASE_KILL_SWITCH'  THEN 'RELEASE'
        WHEN 'GO_LIVE'              THEN 'GO_LIVE'
        WHEN 'RELOAD_CONFIG'        THEN 'RELOAD'
        WHEN 'PING'                 THEN NULL
    END;
    IF v_required_confirm IS NOT NULL AND (p_confirm IS DISTINCT FROM v_required_confirm) THEN
        RAISE EXCEPTION 'enqueue_control_command: confirm token mismatch for %', p_command_type
            USING ERRCODE = '22023';
    END IF;

    -- ENGAGE/RELEASE payload validation on the canonical key: escalation_level (was kill_level).
    IF p_command_type IN ('ENGAGE_KILL_SWITCH','RELEASE_KILL_SWITCH') THEN
        IF NOT (p_payload ? 'escalation_level')
           OR upper(p_payload->>'escalation_level') <> ALL (v_escalation_levels) THEN
            RAISE EXCEPTION 'enqueue_control_command: invalid or missing escalation_level'
                USING ERRCODE = '22023';
        END IF;
        IF length(coalesce(p_payload->>'scope_key','')) = 0 THEN
            RAISE EXCEPTION 'enqueue_control_command: scope_key required'
                USING ERRCODE = '22023';
        END IF;
        IF length(coalesce(p_payload->>'rationale','')) = 0 THEN
            RAISE EXCEPTION 'enqueue_control_command: rationale required for engage/release'
                USING ERRCODE = '22023';
        END IF;
    END IF;

    INSERT INTO control_commands (command_type, payload_json, status, requested_by, idempotency_key)
    VALUES (p_command_type, coalesce(p_payload, '{}'::jsonb), 'pending', p_requested_by, p_idempotency_key)
    RETURNING command_id INTO v_command_id;

    RETURN v_command_id;

EXCEPTION
    WHEN unique_violation THEN
        SELECT command_id INTO v_command_id
        FROM control_commands WHERE idempotency_key = p_idempotency_key;
        RETURN v_command_id;
END;
$$;

-- Re-assert least-privilege EXECUTE (016 default-privileges lesson).
REVOKE ALL ON FUNCTION public.enqueue_control_command(text, jsonb, text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.enqueue_control_command(text, jsonb, text, text, text) FROM authenticated, anon;
GRANT EXECUTE ON FUNCTION public.enqueue_control_command(text, jsonb, text, text, text) TO dashboard_api_node;
