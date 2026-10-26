-- Migration 031 — Tax liability estimate mirror (advisory overlay on the attribution system).
--
-- The tax estimator (src/research/tax/, PR #440) computes the running tax liability on realized
-- gains from live_attribution_ledger — but, like cash-out (027) and DDR-F1 (030) before their
-- mirrors, it is invisible on the dashboard until its output is pushed to Supabase. This table
-- gives it a dashboard-legible row so the operator can SEE what to reserve.
--
-- ADVISORY / TRACKED-NOT-ENCUMBERED: the operator compounds ALL capital (including the tax-owed
-- portion) through the year for the extra growth and pulls the owed tax at the due date. This row
-- is the after-tax OVERLAY, NOT a reservation — gains still compound against GROSS. It moves no
-- capital and is not a trading input.
--
-- Singleton (id=1), upserted by the tax mirror (src/research/tax/mirror.py). last_run_result
-- separates the health states: 'ok' (computed with realized lots), 'empty' (computed, no realized
-- gains yet — the correct $0 state today), 'error' (the compute/push raised). "didn't run" is
-- inferred by the dashboard from a stale updated_at.
--
-- The effective_rate is OPERATOR-SET and EFFECTIVE-DATED-FORWARD (config/tax_rate_schedule.yaml):
-- rate_effective_date + rate_assumption travel with the number so it is never shown bare, and a
-- forward rate change never rewrites a closed prior year.
--
-- RLS mirrors cf_status (027) / ddr_f1_status (030) EXACTLY: dashboard_api_node + authenticated
-- SELECT, service_role writes, all other writes REVOKED. Not a money plane; advisory display only.

BEGIN;

CREATE TABLE IF NOT EXISTS tax_estimate (
    id                        INTEGER PRIMARY KEY,
    tax_year                  INTEGER,
    -- realized-gains inputs (from live_attribution_ledger, the authoritative realized source)
    ytd_net_realized          NUMERIC,       -- net of losses within the tax year
    gross_gains               NUMERIC,       -- sum of positive realized lots
    gross_losses              NUMERIC,       -- sum of negative realized lots (<= 0)
    n_lots                    INTEGER,        -- closed lots in the tax year
    n_long_term               INTEGER,        -- lots whose KNOWN holding period crosses 1yr (flagged)
    -- rate (operator-set, effective-dated-forward)
    effective_rate            NUMERIC,        -- the rate that applied in tax_year
    rate_assumption           TEXT,           -- what the rate is based on (never shown bare)
    rate_effective_date       TEXT,           -- the schedule effective_date the rate came from
    -- liability
    tax_owed                  NUMERIC,        -- max(0, net x rate) — floored at $0
    -- capital overlay (three numbers; advisory, does NOT encumber compounding)
    gross_capital             NUMERIC,        -- Σ allocated_slice (the compounding base)
    after_tax_if_paid_now     NUMERIC,        -- gross_capital - tax_owed (visibility only)
    liquid_capital            NUMERIC,        -- Σ idle (not-currently-deployed; from capital-state)
    -- guardrails
    liability_level           TEXT,           -- OK | WARN | CRITICAL (owed vs liquid)
    liability_message         TEXT,
    next_due_date             TEXT,           -- Apr 15 following the tax year (advisory)
    must_be_liquid_message    TEXT,           -- "reserve ~$X by [due date]; currently $Y liquid"
    -- health
    last_run_result           TEXT,           -- ok | empty | error
    updated_at                TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT tax_estimate_singleton CHECK (id = 1)
);

ALTER TABLE tax_estimate ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON tax_estimate TO dashboard_api_node;
GRANT SELECT ON tax_estimate TO authenticated;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON tax_estimate FROM authenticated, anon, dashboard_api_node;

DROP POLICY IF EXISTS "dashboard_api_node_tax_estimate_select" ON tax_estimate;
CREATE POLICY "dashboard_api_node_tax_estimate_select"
    ON tax_estimate FOR SELECT TO dashboard_api_node USING (true);

DROP POLICY IF EXISTS "authenticated_tax_estimate_select" ON tax_estimate;
CREATE POLICY "authenticated_tax_estimate_select"
    ON tax_estimate FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS "service_role_all_tax_estimate" ON tax_estimate;
CREATE POLICY "service_role_all_tax_estimate"
    ON tax_estimate FOR ALL TO service_role USING (true) WITH CHECK (true);

COMMIT;
