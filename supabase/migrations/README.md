# Migration rules (mbappe Supabase)

Standing discipline (GV-9, 2026-07-17, extends the 015 least-privilege doctrine):

1. **Every new-table migration ENDS with the explicit revoke:**

   ```sql
   REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON <table> FROM authenticated, anon;
   ```

   Supabase's default privileges grant broad table rights to `authenticated` AND `anon` at
   CREATE time (the 001-blanket-grant class that produced dashboard-audit finding F2). RLS with
   no matching policy CONTAINS those grants, but defense-in-depth demands they not exist at all
   — an accidental permissive policy must not be one grant away from a write path. The template
   is migration `021_leg_return_series.sql`.

2. **Dashboard principals are SELECT-only by policy AND by grant.** Writes go through
   `service_role` (bot lane) or a SECURITY DEFINER RPC (command bus, migration 016) — never a
   direct table grant to `authenticated`/`anon`.

3. **Migrations are committed to this directory BEFORE application; application is
   operator-gated** unless a queue explicitly pre-authorizes it. File and live state must match —
   drift found during verification is REPORTED, never silently re-run (verify, don't re-apply).

4. **Append-only tables** (journals, artifacts) enforce it with no-UPDATE/no-DELETE triggers
   (template: `018_experiment_artifacts.sql`), not by convention.
