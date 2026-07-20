-- Serverless optimistic concurrency for dashboard parameter writes.

CREATE OR REPLACE FUNCTION update_strategy_params_optimistic(
    p_strategy_id UUID,
    p_new_params JSONB,
    p_expected_version INTEGER
)
RETURNS TABLE (
    id UUID,
    name TEXT,
    version_id INTEGER,
    updated_at TIMESTAMPTZ,
    params JSONB
)
LANGUAGE plpgsql
AS $$
DECLARE
    updated_row strategies%ROWTYPE;
BEGIN
    UPDATE strategies
    SET params = p_new_params,
        version_id = strategies.version_id + 1,
        updated_at = NOW()
    WHERE strategies.id = p_strategy_id
      AND strategies.version_id = p_expected_version
    RETURNING * INTO updated_row;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'STRATEGY_VERSION_CONFLICT'
            USING ERRCODE = '40001',
                  DETAIL = 'Strategy configuration version conflict; reload and retry.',
                  HINT = 'Fetch the latest version_id before saving.';
    END IF;

    RETURN QUERY
    SELECT
        updated_row.id,
        updated_row.name,
        updated_row.version_id,
        updated_row.updated_at,
        updated_row.params;
END;
$$;

GRANT EXECUTE ON FUNCTION update_strategy_params_optimistic(UUID, JSONB, INTEGER)
    TO authenticated;
