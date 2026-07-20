-- Migration 029 — cash-out forward ATTRIBUTION rollup on cf_status (additive).
--
-- 027 gave cf_status the module HEALTH signal (mode/last_run_result/detections/positions). It did NOT
-- carry the booking ATTRIBUTION — the forward_clock rollup (n_events/n_entered/n_no_trade/n_booked and
-- the realized median edge) that store.forward_clock() computes lived ONLY in droplet SQLite. Per the
-- ratified health-signal doctrine ("a component isn't displayed until it pushes its signal") we
-- mirrored health; this mirrors RESULTS so the Legs page can show cash-out's real quantity alongside
-- the commissioning legs.
--
-- HONEST CONTENT: realized_median_net_cost_1x is the median of (fixed - purchase)/purchase - cost over
-- booked events — a MODELED per-event EDGE FRACTION, NOT broker-reconciled dollars and NOT a $ P&L. The
-- display marks it MODELED_EDGE so it can never be mistaken for the commissioning legs' dollar P&L.
--
-- Additive-only (nullable, no rename, no NOT NULL): an old-schema writer keeps upserting the singleton
-- (its payload omits these -> they land NULL). RLS is table-level; the 027 SELECT policies
-- (dashboard_api_node / authenticated) already cover every column, so NO new policy is needed.

BEGIN;

ALTER TABLE cf_status ADD COLUMN IF NOT EXISTS n_events_detected            INTEGER;  -- is_event filings
ALTER TABLE cf_status ADD COLUMN IF NOT EXISTS n_entered                    INTEGER;  -- ENTER decisions
ALTER TABLE cf_status ADD COLUMN IF NOT EXISTS n_no_trade                   INTEGER;  -- NO_TRADE (gap absent net cost)
ALTER TABLE cf_status ADD COLUMN IF NOT EXISTS n_booked                     INTEGER;  -- booked cash-outs (forward-pass count)
ALTER TABLE cf_status ADD COLUMN IF NOT EXISTS realized_median_net_cost_1x  REAL;     -- MODELED median edge fraction, NOT $

COMMIT;
