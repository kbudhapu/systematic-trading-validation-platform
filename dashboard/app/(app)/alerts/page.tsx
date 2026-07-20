import { createClient } from "@/lib/supabase/server";
import { safeRows, strOrNull, type Row } from "@/lib/dashboard-data";
import { SourcedPanel, NoFeedBody } from "@/components/SourcedPanel";
import { PageHeader, Banner } from "@/components/atoms";
import { AlertsPanel, type AlertItem } from "@/components/AlertsFeed";

export const dynamic = "force-dynamic";

export default async function AlertsPage() {
  const supabase = await createClient();

  const eventsRes = await safeRows<Row>(
    supabase
      .from("system_events")
      .select("id, event_type, severity, message, created_at")
      .in("severity", ["warning", "critical"])
      .order("created_at", { ascending: false })
      .limit(500),
  );

  const alerts: AlertItem[] = eventsRes.rows.map((r, i) => ({
    id: String(r.id ?? i),
    event_type: String(r.event_type ?? "event"),
    severity: String(r.severity ?? "warning"),
    message: String(r.message ?? ""),
    created_at: String(r.created_at ?? ""),
  }));

  return (
    <div className="space-y-4">
      <PageHeader
        title="Alerts"
        subtitle="Warning/critical system events, collapsed by kind + reason."
      />

      <SourcedPanel
        title="Alerts"
        source="system_events"
        lastSync={strOrNull(eventsRes.rows[0]?.created_at)}
        provenance={eventsRes.ok ? "DERIVED" : "NO-FEED"}
        cadenceMs={5 * 60_000}
        note="Identical alerts (same event_type + message) are folded into one row with a count and first/last-seen. All groups are visible by default — including bar_freshness."
      >
        {eventsRes.ok ? (
          <AlertsPanel alerts={alerts} />
        ) : (
          <NoFeedBody reason="system_events not reachable in the mirror." />
        )}
      </SourcedPanel>

      <Banner tone="info">
        Ack is local (never written back to the mirror). The historical <code>bar_freshness</code>
        criticals (pre 2026-07-16 17:29 UTC) were a sensor false-critical, fixed in
        deploy-droplet-20260716b — any <code>bar_freshness</code> alert AFTER that is a genuine
        staleness event. Nothing is hidden by default.
      </Banner>
    </div>
  );
}
