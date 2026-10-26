-- Version-matched optimistic updates for non-params strategy fields.

CREATE OR REPLACE FUNCTION update_strategy_field_optimistic(
    p_strategy_id UUID,
    p_field_name TEXT,
    p_field_value JSONB,
    p_expected_version INTEGER
)
RETURNS TABLE (
    id UUID,
    name TEXT,
    version_id INTEGER,
    updated_at TIMESTAMPTZ,
    enabled BOOLEAN,
    environment TEXT,
    params JSONB
)
LANGUAGE plpgsql
AS $$
DECLARE
    updated_row strategies%ROWTYPE;
BEGIN
    IF p_field_name NOT IN ('enabled', 'environment') THEN
        RAISE EXCEPTION 'UNSUPPORTED_STRATEGY_FIELD'
            USING ERRCODE = '22023',
                  DETAIL = format('Field %s is not allowed for optimistic update.', p_field_name);
    END IF;

    IF p_field_name = 'enabled' THEN
        UPDATE strategies
        SET enabled = (p_field_value #>> '{}')::boolean,
            version_id = strategies.version_id + 1,
            updated_at = NOW()
        WHERE strategies.id = p_strategy_id
          AND strategies.version_id = p_expected_version
        RETURNING * INTO updated_row;
    ELSE
        UPDATE strategies
        SET environment = p_field_value #>> '{}',
            version_id = strategies.version_id + 1,
            updated_at = NOW()
        WHERE strategies.id = p_strategy_id
          AND strategies.version_id = p_expected_version
        RETURNING * INTO updated_row;
    END IF;

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
        updated_row.enabled,
        updated_row.environment,
        updated_row.params;
END;
$$;

GRANT EXECUTE ON FUNCTION update_strategy_field_optimistic(UUID, TEXT, JSONB, INTEGER)
    TO authenticated;
