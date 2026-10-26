-- Push NOTIFY events when dashboard or control plane mutates strategy configuration.

ALTER TABLE strategies
    ADD COLUMN IF NOT EXISTS version_id INTEGER NOT NULL DEFAULT 1;

CREATE OR REPLACE FUNCTION notify_strategy_config_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_notify(
        'strategy_config_update',
        json_build_object(
            'id', NEW.id,
            'name', NEW.name,
            'version_id', NEW.version_id,
            'updated_at', NEW.updated_at
        )::text
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS strategy_config_update_notify ON strategies;
CREATE TRIGGER strategy_config_update_notify
    AFTER INSERT OR UPDATE ON strategies
    FOR EACH ROW
    EXECUTE FUNCTION notify_strategy_config_update();
