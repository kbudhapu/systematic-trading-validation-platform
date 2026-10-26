-- Migration 016 — secure control-command enqueue RPC (CP7 F1 / Gate A / D-migration-1)
--
-- After 015 removed the direct authenticated/dashboard_api_node INSERT on control_commands,
-- this is the ONLY sanctioned path for the dashboard to enqueue a command. It is a
-- SECURITY DEFINER function owned by a dedicated NOLOGIN role (`command_enqueue_owner`),
-- EXECUTE-granted to dashboard_api_node only. The function enforces, at the DB tier:
--   • command_type whitelist (mirrors src/control/command_queue.py ControlCommandType),
--   • exact per-type confirm token (KILL / ENGAGE / RELEASE / GO_LIVE / RELOAD),
--   • per-type payload validation (kill_level whitelist, scope_key, rationale).
-- The confirm tier is thus authoritative in the database — a raw PostgREST caller cannot
-- bypass it (they have no INSERT and no EXECUTE unless they hold the dashboard_api_node key).
--
-- APPLY ORDER: 015 → 016 → 017 → 018. validate_migration.py exits 2 on DDL — expected.

BEGIN;

-- Dedicated definer owner (NOT postgres / service_role). NOLOGIN → unreachable except as
-- the owner of this one function. It needs INSERT on control_commands via a policy because
-- RLS is enabled and the owner is neither the table owner nor BYPASSRLS.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'command_enqueue_owner') THEN
        CREATE ROLE command_enqueue_owner NOLOGIN;
    END IF;
END
$$;

-- The migration runner (Supabase `postgres`, a limited admin — NOT a superuser) must be a member
-- of the definer role to ALTER FUNCTION … OWNER TO it. Standard Supabase pattern; benign (postgres
-- is already top admin). Operator-authorized. Idempotent.
GRANT command_enqueue_owner TO postgres;

GRANT USAGE ON SCHEMA public TO command_enqueue_owner;
-- Postgres requires the NEW OWNER of a public-schema object to hold CREATE on the schema for the
-- ALTER … OWNER below. Granted transiently and REVOKED immediately after the transfer (see end),
-- so the end-state is USAGE-only (least-privilege). Operator-authorized. command_enqueue_owner is
-- NOLOGIN + reachable only via the validating SECURITY DEFINER function, so this is inert in practice.
GRANT CREATE ON SCHEMA public TO command_enqueue_owner;
GRANT SELECT, INSERT ON control_commands TO command_enqueue_owner;

DROP POLICY IF EXISTS "command_enqueue_owner_insert" ON control_commands;
CREATE POLICY "command_enqueue_owner_insert"
    ON control_commands FOR INSERT TO command_enqueue_owner WITH CHECK (true);
DROP POLICY IF EXISTS "command_enqueue_owner_select" ON control_commands;
CREATE POLICY "command_enqueue_owner_select"
    ON control_commands FOR SELECT TO command_enqueue_owner USING (true);

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
    -- KEEP IN SYNC with src/engine/governance.py :: KillLevel
    v_kill_levels text[] := ARRAY['STRATEGY_HALT','PORTFOLIO_HALT','RESEARCH_HALT','AI_HALT'];
    v_required_confirm text;
    v_command_id uuid;
BEGIN
    IF p_command_type IS NULL OR p_command_type <> ALL (v_allowed_types) THEN
        RAISE EXCEPTION 'enqueue_control_command: unknown command_type %', p_command_type
            USING ERRCODE = '22023';
    END IF;

    -- Per-type confirm token (DB-tier enforcement; PING is a no-op probe, no confirm).
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

    -- Per-type payload validation.
    IF p_command_type IN ('ENGAGE_KILL_SWITCH','RELEASE_KILL_SWITCH') THEN
        IF NOT (p_payload ? 'kill_level')
           OR (p_payload->>'kill_level') <> ALL (v_kill_levels) THEN
            RAISE EXCEPTION 'enqueue_control_command: invalid or missing kill_level'
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
    -- Idempotent: a repeated idempotency_key returns the existing command_id (matches the
    -- Python enqueue path behaviour) rather than erroring.
    WHEN unique_violation THEN
        SELECT command_id INTO v_command_id
        FROM control_commands WHERE idempotency_key = p_idempotency_key;
        RETURN v_command_id;
END;
$$;

ALTER FUNCTION public.enqueue_control_command(text, jsonb, text, text, text)
    OWNER TO command_enqueue_owner;

-- Ownership transferred — drop the transient CREATE so the owner is USAGE-only (least-privilege).
REVOKE CREATE ON SCHEMA public FROM command_enqueue_owner;

-- Only dashboard_api_node may call it (not authenticated, not anon, not PUBLIC).
-- NB: Supabase default privileges auto-GRANT EXECUTE on new functions to authenticated + anon;
-- that survives a FROM PUBLIC revoke, so it must be revoked from those roles EXPLICITLY.
REVOKE ALL ON FUNCTION public.enqueue_control_command(text, jsonb, text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.enqueue_control_command(text, jsonb, text, text, text) FROM authenticated, anon;
GRANT EXECUTE ON FUNCTION public.enqueue_control_command(text, jsonb, text, text, text)
    TO dashboard_api_node;

COMMIT;
