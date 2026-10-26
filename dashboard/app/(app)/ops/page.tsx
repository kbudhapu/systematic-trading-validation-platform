import { createClient } from "@/lib/supabase/server";
import { safeRows, firstRow, numOrNull, strOrNull, type Row } from "@/lib/dashboard-data";
import { SourcedPanel, NoFeedBody } from "@/components/SourcedPanel";
import { PageHeader, Banner, StatTile, HealthDot } from "@/components/atoms";
import { ts, ago, qty, currency } from "@/lib/format";

export const dynamic = "force-dynamic";

const HEARTBEAT_CADENCE = 60_000;

// Tables whose freshness we surface, with their timestamp column.
const MIRROR_TABLES: { table: string; tsCol: string }[] = [
  { table: "equity_snapshots", tsCol: "recorded_at" },
  { table: "bot_runs", tsCol: "created_at" },
  { table: "trades", tsCol: "timestamp" },
  { table: "leg_attribution_snapshots", tsCol: "recorded_at" },
  { table: "system_events", tsCol: "created_at" },
  { table: "control_commands", tsCol: "created_at" },
];

export default async function OpsPage() {
  const supabase = await createClient();

  // LIVENESS = the INNER engine-cycle heartbeat (engine_heartbeat.last_successful_cycle_at), the SAME
  // key the Discord dead-man reads. ITEM-1 FIX: the old source bot_runs.created_at is a separate
  // outer-loop insert written by the soak WRAPPER every cycle; during the 2026-08-07→08 ~19h
  // inner-cycle freeze (maintenance gate blocked on stale champions) it kept advancing and MASKED the
  // freeze on the website while Discord (reading the inner heartbeat) paged. Reading the inner
  // heartbeat here means the two surfaces can never again disagree on a real freeze.
  const hbRes = await safeRows<Row>(
    supabase
      .from("engine_heartbeat")
      .select("last_successful_cycle_at, updated_at, environment")
      .order("updated_at", { ascending: false })
      .limit(1),
  );
  const hb = firstRow(hbRes);
  const heartbeatTs = strOrNull(hb?.last_successful_cycle_at);

  // bot_runs is still read for equity/status/cycle DETAIL only — NOT for liveness.
  const runRes = await safeRows<Row>(
    supabase
      .from("bot_runs")
      .select("equity, cycle_ms, status, halted, environment, created_at")
      .order("created_at", { ascending: false })
      .limit(1),
  );
  const run = firstRow(runRes);
  const heartbeatEquity = numOrNull(run?.equity);

  // Per-table mirror lag
  const lags = await Promise.all(
    MIRROR_TABLES.map(async ({ table, tsCol }) => {
      const q = supabase
        .from(table)
        .select(tsCol)
        .order(tsCol, { ascending: false })
        .limit(1) as unknown as PromiseLike<{
        data: Row[] | null;
        error: { message: string } | null;
      }>;
      const res = await safeRows<Row>(q);
      const latest = res.ok ? strOrNull(res.rows[0]?.[tsCol]) : null;
      return { table, tsCol, ok: res.ok, latest };
    }),
  );

  // system_events feed (all severities)
  const eventsRes = await safeRows<Row>(
    supabase
      .from("system_events")
      .select("id, event_type, severity, message, created_at")
      .order("created_at", { ascending: false })
      .limit(30),
  );

  // Command history (Supabase-mirrored only)
  const cmdRes = await safeRows<Row>(
    supabase
      .from("control_commands")
      .select("command_id, command_type, status, requested_by, created_at, processed_at")
      .order("created_at", { ascending: false })
      .limit(30),
  );

  return (
    <div className="space-y-4">
      <PageHeader title="Ops" subtitle="Heartbeat, mirror freshness, events, and command history." />

      {/* Heartbeat + freshness. LIVENESS SOURCE = engine_heartbeat.last_successful_cycle_at (the
          INNER engine cycle, the same key the Discord dead-man reads) so the dot + "last cycle" text
          can never mask a real inner-cycle freeze (Item-1 fix). Equity/status/cycle are DETAIL from
          bot_runs; a NULL equity reads "not reported this beat", NOT a staleness claim. */}
      <SourcedPanel
        title="Heartbeat & freshness"
        source="engine_heartbeat.last_successful_cycle_at"
        lastSync={heartbeatTs}
        provenance={hbRes.ok && hb ? "BROKER" : "NO-FEED"}
        cadenceMs={HEARTBEAT_CADENCE}
        note={undefined}
      >
        {hbRes.ok && hb ? (
          <div className="space-y-3">
            <HealthDot lastSync={heartbeatTs} label="engine cycle heartbeat" />
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <StatTile label="Last cycle" value={ago(heartbeatTs)} sub={ts(heartbeatTs)} />
              <StatTile
                label="Heartbeat equity"
                value={heartbeatEquity == null ? "—" : currency(heartbeatEquity)}
                sub={heartbeatEquity == null ? "not reported this beat" : undefined}
              />
              <StatTile label="Status" value={String(run?.status ?? "—")} />
              <StatTile label="Cycle" value={`${qty(numOrNull(run?.cycle_ms))} ms`} />
            </div>
            <div className="text-xs text-[var(--dim)]">
              env {String(hb.environment ?? run?.environment ?? "—")}
            </div>
          </div>
        ) : (
          <NoFeedBody reason="No engine_heartbeat (inner cycle) reachable." />
        )}
      </SourcedPanel>

      {/* Per-table mirror lag */}
      <SourcedPanel
        title="Mirror lag (per table)"
        source="supabase mirror"
        lastSync={heartbeatTs}
        provenance="DERIVED"
        cadenceMs={5 * 60_000}
      >
        <div className="overflow-x-auto">
          <table className="w-full min-w-[420px] text-sm">
            <thead>
              <tr className="text-left text-[0.68rem] uppercase tracking-wide text-[var(--muted)]">
                <th className="py-1.5 pr-3 font-medium">Table</th>
                <th className="py-1.5 pr-3 font-medium">Latest row</th>
                <th className="py-1.5 font-medium">Lag</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-soft)]">
              {lags.map((l) => (
                <tr key={l.table}>
                  <td className="py-1.5 pr-3 font-mono text-xs text-[var(--text)]">{l.table}</td>
                  <td className="py-1.5 pr-3 text-[0.72rem] text-[var(--muted)] tnum">
                    {l.ok ? ts(l.latest) : "—"}
                  </td>
                  <td className="py-1.5 text-xs">
                    {l.ok ? (
                      <span className="text-[var(--muted)]">{ago(l.latest)}</span>
                    ) : (
                      <span className="prov-badge prov-badge--nofeed">NO-FEED</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </SourcedPanel>

      {/* Backup recency — not mirrored */}
      <SourcedPanel title="Backup recency" source="—" provenance="NO-FEED">
        <NoFeedBody reason="Off-box backup status is not mirrored to Supabase." />
      </SourcedPanel>

      {/* system_events */}
      <SourcedPanel
        title="System events"
        source="system_events"
        lastSync={strOrNull(eventsRes.rows[0]?.created_at)}
        provenance={eventsRes.ok ? "DERIVED" : "NO-FEED"}
        cadenceMs={5 * 60_000}
      >
        {eventsRes.ok && eventsRes.rows.length > 0 ? (
          <ul className="divide-y divide-[var(--border-soft)]">
            {eventsRes.rows.map((e, i) => {
              const sev = String(e.severity ?? "info").toLowerCase();
              const color =
                sev === "critical" ? "var(--red)" : sev === "warning" ? "var(--warn)" : "var(--dim)";
              return (
                <li key={String(e.id ?? i)} className="flex items-start gap-3 py-1.5">
                  <span
                    className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full"
                    style={{ background: color }}
                  />
                  <div className="min-w-0 flex-1">
                    <span className="text-xs font-medium text-[var(--text)]">
                      {String(e.event_type ?? "event")}
                    </span>
                    <span className="ml-2 truncate text-xs text-[var(--muted)]">
                      {String(e.message ?? "")}
                    </span>
                  </div>
                  <span className="shrink-0 text-[0.62rem] text-[var(--dim)] tnum">
                    {ago(strOrNull(e.created_at))}
                  </span>
                </li>
              );
            })}
          </ul>
        ) : (
          <NoFeedBody reason="system_events not reachable / empty." />
        )}
      </SourcedPanel>

      {/* Command history (relocated here from legacy /dashboard/history) */}
      <SourcedPanel
        title="Command history"
        source="control_commands"
        lastSync={strOrNull(cmdRes.rows[0]?.created_at)}
        provenance={cmdRes.ok ? "DERIVED" : "NO-FEED"}
        cadenceMs={10 * 60_000}
        note="Pre-unification command history (~861 rows) is droplet-local SQLite and is NOT mirrored to Supabase — it is not shown here."
      >
        {cmdRes.ok && cmdRes.rows.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm">
              <thead>
                <tr className="text-left text-[0.68rem] uppercase tracking-wide text-[var(--muted)]">
                  <th className="py-1.5 pr-3 font-medium">Type</th>
                  <th className="py-1.5 pr-3 font-medium">Status</th>
                  <th className="py-1.5 pr-3 font-medium">By</th>
                  <th className="py-1.5 pr-3 font-medium">Created</th>
                  <th className="py-1.5 font-medium">Processed</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-soft)]">
                {cmdRes.rows.map((c, i) => (
                  <tr key={String(c.command_id ?? i)}>
                    <td className="py-1.5 pr-3 font-mono text-xs text-[var(--text)]">
                      {String(c.command_type ?? "—")}
                    </td>
                    <td className="py-1.5 pr-3 text-xs text-[var(--muted)]">
                      {String(c.status ?? "—")}
                    </td>
                    <td className="py-1.5 pr-3 text-xs text-[var(--muted)]">
                      {String(c.requested_by ?? "—")}
                    </td>
                    <td className="py-1.5 pr-3 text-[0.68rem] text-[var(--dim)] tnum">
                      {ts(strOrNull(c.created_at))}
                    </td>
                    <td className="py-1.5 text-[0.68rem] text-[var(--dim)] tnum">
                      {ts(strOrNull(c.processed_at))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <NoFeedBody reason="control_commands not reachable / empty in the mirror." />
        )}
      </SourcedPanel>

      <Banner tone="info">
        Freshness and mirror-lag are shown with neutral styling — a dim/STALE marker is a
        provenance signal, not a danger signal.
      </Banner>
    </div>
  );
}
