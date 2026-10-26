-- Reset per-leg paper sessions after P&L attribution fix.
-- Portfolio session is unchanged. Bot creates fresh leg sessions on next cycle.

UPDATE performance_sessions
SET is_active = false, ended_at = now()
WHERE is_active = true
  AND strategy_id IN (
    SELECT id FROM strategies WHERE module != 'portfolio'
  );
