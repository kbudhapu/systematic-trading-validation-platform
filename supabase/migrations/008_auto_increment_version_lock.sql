-- Database-side optimistic concurrency: automatic version bump and updated_at touch.

CREATE OR REPLACE FUNCTION bump_strategy_version_lock()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := NOW();
    NEW.version_id := OLD.version_id + 1;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS strategy_version_lock_bump ON strategies;
CREATE TRIGGER strategy_version_lock_bump
    BEFORE UPDATE ON strategies
    FOR EACH ROW
    EXECUTE FUNCTION bump_strategy_version_lock();
