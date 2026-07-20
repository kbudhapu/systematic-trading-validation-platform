import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { safeRows, strOrNull, type Row } from "@/lib/dashboard-data";
import { SourcedPanel, NoFeedBody } from "@/components/SourcedPanel";
import { PageHeader, Banner } from "@/components/atoms";
import {
  FamilyScorecard,
  PHistogram,
  CostDecayScatter,
  EffectSizeFunnel,
} from "@/components/MetaAnalytics";
import { parseMetaAnalytics } from "@/lib/experiment";

export const dynamic = "force-dynamic";

export default async function ExperimentsAnalyticsPage() {
  const supabase = await createClient();

  // The publisher-computed meta artifact — latest version. All aggregation done in Python.
  const res = await safeRows<Row>(
    supabase
      .from("experiment_artifacts")
      .select("experiment_id, version, artifact_json, published_at")
      .eq("experiment_id", "_META-ANALYTICS")
      .order("version", { ascending: false })
      .limit(1),
  );
  const row = res.rows[0] ?? null;
  const meta = row ? parseMetaAnalytics(row.artifact_json) : null;
  const lastSync = strOrNull(row?.published_at);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link href="/experiments" className="text-xs text-[var(--muted)] hover:text-[var(--accent)]">
          ← experiments
        </Link>
      </div>
      <PageHeader
        title="Meta-research analytics"
        subtitle="Cross-experiment learning surface. Every number was computed by the publisher — nothing is computed here."
      />

      {!res.ok || !meta ? (
        <SourcedPanel title="Analytics" source="experiment_artifacts:_META-ANALYTICS" provenance="NO-FEED">
          <NoFeedBody reason="No _META-ANALYTICS artifact published yet — run scripts/publish_experiment_artifact.py." />
        </SourcedPanel>
      ) : (
        <>
          <SourcedPanel
            title="Family scorecard"
            source="_META-ANALYTICS.analytics.family_scorecard"
            lastSync={lastSync}
            provenance="REGISTRY"
            cadenceMs={24 * 60 * 60_000}
            note="Which hypothesis families are worth the time — trial count, triage pass rate, publisher-computed medians."
          >
            <FamilyScorecard rows={meta.scorecard} nTotal={meta.nTrials} />
          </SourcedPanel>

          <SourcedPanel
            title="p-value calibration"
            source="_META-ANALYTICS.analytics.p_histogram"
            lastSync={lastSync}
            provenance="REGISTRY"
            cadenceMs={24 * 60 * 60_000}
          >
            <PHistogram h={meta.pHistogram} />
          </SourcedPanel>

          <SourcedPanel
            title="Sharpe vs cost-decay"
            source="_META-ANALYTICS.analytics.cost_decay_scatter"
            lastSync={lastSync}
            provenance="REGISTRY"
            cadenceMs={24 * 60 * 60_000}
          >
            <CostDecayScatter s={meta.scatter} />
          </SourcedPanel>

          <SourcedPanel
            title="Effect-size funnel"
            source="_META-ANALYTICS.analytics.effect_size_funnel"
            lastSync={lastSync}
            provenance="REGISTRY"
            cadenceMs={24 * 60 * 60_000}
          >
            <EffectSizeFunnel f={meta.funnel} />
          </SourcedPanel>
        </>
      )}

      <Banner tone="info">
        Render-only surface: medians, pass rates, histogram bins and ladder slopes are computed by
        <code> scripts/publish_experiment_artifact.py</code> into the <code>_META-ANALYTICS</code>{" "}
        artifact. Sorting the table re-orders rendered rows only.
      </Banner>
    </div>
  );
}
