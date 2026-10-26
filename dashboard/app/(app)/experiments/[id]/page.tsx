import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { safeRows, strOrNull, type Row } from "@/lib/dashboard-data";
import { SourcedPanel, NoFeedBody } from "@/components/SourcedPanel";
import { PageHeader } from "@/components/atoms";
import {
  VerdictChip,
  TrialsPanel,
  PsdBudgetPanel,
  CriteriaMarkdown,
  ExperimentSummary,
} from "@/components/ExperimentViz";
import { parseArtifact } from "@/lib/experiment";
import { ts } from "@/lib/format";
import { McptHistogram } from "@/components/McptHistogram";
import { DiagnosticReportView } from "@/components/DiagnosticReportView";
import { ResearchEquityPanel } from "@/components/ResearchEquityPanel";
import {
  EVIDENCE_META_COLS,
  toEvidenceRow,
  toEquityCurveRow,
  dedupePreferPresent,
} from "@/lib/evidence";

export const dynamic = "force-dynamic";

export default async function ExperimentDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const experimentId = decodeURIComponent(id);
  const supabase = await createClient();

  const res = await safeRows<Row>(
    supabase
      .from("experiment_artifacts")
      .select(
        "experiment_id, version, verdict, criteria_sha256, artifact_json, published_at, published_by",
      )
      .eq("experiment_id", experimentId)
      .order("published_at", { ascending: false })
      .limit(1),
  );

  const row = res.rows[0] ?? null;
  const back = (
    <Link
      href="/experiments"
      className="text-xs text-[var(--muted)] hover:text-[var(--accent)]"
    >
      ← all experiments
    </Link>
  );

  if (!res.ok || !row) {
    return (
      <div className="space-y-4">
        {back}
        <PageHeader title={experimentId} subtitle="experiment detail" />
        <SourcedPanel title="Artifact" source="experiment_artifacts" provenance="NO-FEED">
          <NoFeedBody reason="No published artifact reachable for this experiment_id." />
        </SourcedPanel>
      </div>
    );
  }

  const art = parseArtifact(row.artifact_json);
  const publishedAt = art.asOf ?? strOrNull(row.published_at);
  const hasTrials = art.trials.length > 0;

  // ── Research evidence for this experiment (mirror 025). Metadata only — null_array is
  //    lazy-loaded per histogram when it opens. Curves are a separate table. ──
  const evRes = await safeRows<Row>(
    supabase
      .from("research_evidence")
      .select(EVIDENCE_META_COLS)
      .eq("experiment_id", experimentId)
      .order("source_row_id", { ascending: true })
      .returns<Row[]>(),
  );
  const evRows = evRes.rows.map(toEvidenceRow);
  const mcpt = dedupePreferPresent(evRows.filter((r) => r.evidenceKind === "mcpt_null"));
  const reports = evRows.filter((r) => r.evidenceKind === "diagnostic_report");

  const curveRes = await safeRows<Row>(
    supabase
      .from("research_equity_curves")
      .select(
        "source_row_id, experiment_id, trial_key, evidence_class, series_kind, n_trades, series_json, drawdown_json, max_drawdown, generator_ref, schema_version, created_at",
      )
      .eq("experiment_id", experimentId)
      .order("source_row_id", { ascending: true })
      .returns<Row[]>(),
  );
  const curves = curveRes.rows.map(toEquityCurveRow);
  const evLastSync = evRows[0]?.createdAt ?? curves[0]?.createdAt ?? null;
  const evProvenance = evRes.ok && evRes.rows.length > 0 ? "DERIVED" : "NO-FEED";

  return (
    <div className="space-y-4">
      {back}
      <div className="flex flex-wrap items-center gap-3">
        <PageHeader title={art.title ?? experimentId} subtitle={art.experimentId ?? experimentId} />
        {art.verdict && (
          <span className="ml-auto">
            <VerdictChip verdict={art.verdict} detail={art.verdictDetail} />
          </span>
        )}
      </div>

      <ExperimentSummary art={art} />

      <SourcedPanel
        title="Criteria & decision"
        source="artifact_json.criteria_md"
        lastSync={publishedAt}
        provenance="REGISTRY"
        cadenceMs={24 * 60 * 60_000}
        note={`as_of ${ts(art.asOf)} · provenance ${art.provenance ?? "REGISTRY"} · published ${ts(
          strOrNull(row.published_at),
        )}${art.verdictDetail ? ` · ruling: ${art.verdictDetail}` : ""}`}
      >
        <CriteriaMarkdown md={art.criteriaMd} />
      </SourcedPanel>

      {hasTrials && (
        <SourcedPanel
          title="Wave-1 statistics"
          source="artifact_json.trials[].stats"
          lastSync={publishedAt}
          provenance="REGISTRY"
          cadenceMs={24 * 60 * 60_000}
          note="Cost-stress ladder (1×/2×/4×), CI95, MCPT p-value and DSR — extracted per family by the publisher. Rendered as-is; nothing computed here."
        >
          <TrialsPanel trials={art.trials} />
        </SourcedPanel>
      )}

      <SourcedPanel
        title="Trial budget (alpha-spend)"
        source="artifact_json.psd"
        lastSync={publishedAt}
        provenance="REGISTRY"
        cadenceMs={24 * 60 * 60_000}
        note="Grid points, timeframes and objective variants evaluated — the PSD trial-count ledger."
      >
        <PsdBudgetPanel psd={art.psd} />
      </SourcedPanel>

      {/* ── RD2/RD4/RD5: verdict-time evidence from the research vault mirror ── */}
      <SourcedPanel
        title="Verdict-time evidence — MCPT nulls"
        source="research_evidence · evidence_kind=mcpt_null"
        lastSync={evLastSync}
        provenance={mcpt.rows.length > 0 ? "DERIVED" : evProvenance}
        cadenceMs={24 * 60 * 60_000}
        note={
          mcpt.rows.length > 0
            ? `${mcpt.rows.length} trial${mcpt.rows.length === 1 ? "" : "s"}${
                mcpt.hiddenDeferred > 0
                  ? ` · ${mcpt.hiddenDeferred} deferred duplicate${
                      mcpt.hiddenDeferred === 1 ? "" : "s"
                    } hidden (present preferred)`
                  : ""
              }`
            : undefined
        }
      >
        {mcpt.rows.length > 0 ? (
          <div className="space-y-4">
            {mcpt.rows
              .slice()
              .sort((a, b) => a.trialKey.localeCompare(b.trialKey))
              .map((r) => (
                <div key={r.sourceRowId} className="rounded-md border border-[var(--border-soft)] p-3">
                  <div className="mb-1 text-xs font-semibold text-[var(--text)]">{r.trialKey}</div>
                  <McptHistogram row={r} />
                </div>
              ))}
          </div>
        ) : (
          <NoFeedBody reason="No MCPT null evidence mirrored for this experiment (ships after 025 + backfill)." />
        )}
      </SourcedPanel>

      {reports.length > 0 && (
        <SourcedPanel
          title="Diagnostic reports (from-birth)"
          source="research_evidence · evidence_kind=diagnostic_report"
          lastSync={evLastSync}
          provenance="REGISTRY"
          cadenceMs={24 * 60 * 60_000}
        >
          <DiagnosticReportView rows={reports} />
        </SourcedPanel>
      )}

      <SourcedPanel
        title="Registered equity curves"
        source="research_equity_curves"
        lastSync={curves[0]?.createdAt ?? null}
        provenance={curves.length > 0 ? "DERIVED" : "NO-FEED"}
        cadenceMs={24 * 60 * 60_000}
        note={curves.length > 0 ? `${curves.length} curve${curves.length === 1 ? "" : "s"}` : undefined}
      >
        <ResearchEquityPanel curves={curves} />
      </SourcedPanel>
    </div>
  );
}
