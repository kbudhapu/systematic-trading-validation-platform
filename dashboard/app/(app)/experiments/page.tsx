import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { safeRows, strOrNull, type Row } from "@/lib/dashboard-data";
import { SourcedPanel, NoFeedBody } from "@/components/SourcedPanel";
import { PageHeader, Banner } from "@/components/atoms";
import { VerdictChip } from "@/components/ExperimentViz";
import { parseArtifact } from "@/lib/experiment";
import { RegistryTabs, type RegistryTab } from "@/components/RegistryTabs";
import { ts } from "@/lib/format";

export const dynamic = "force-dynamic";

const KIND_ORDER: Record<string, number> = {
  EXPERIMENT: 0,
  QUARANTINED: 1,
  QUARANTINED_TOMBSTONE: 2,
  DOCTRINE: 3,
  STANDING: 4,
  PREAMBLE: 5,
};

export default async function ExperimentsPage() {
  const supabase = await createClient();

  const res = await safeRows<Row>(
    supabase
      .from("experiment_artifacts")
      .select(
        "experiment_id, version, verdict, criteria_sha256, artifact_json, published_at, published_by",
      )
      .order("published_at", { ascending: false })
      .limit(400),
  );

  // Latest published version per experiment_id (grouping only, no computation).
  // _META-ANALYTICS is the publisher-computed analytics artifact — it renders on
  // /experiments/analytics, not as a registry row here.
  const seen = new Set<string>();
  const items = res.rows
    .filter((r) => {
      const id = String(r.experiment_id ?? "");
      if (!id || id.startsWith("_META") || seen.has(id)) return false;
      seen.add(id);
      return true;
    })
    .map((r) => {
      const art = parseArtifact(r.artifact_json);
      return {
        id: String(r.experiment_id),
        version: strOrNull(r.version),
        verdict: art.verdict ?? strOrNull(r.verdict) ?? undefined,
        verdictDetail: art.verdictDetail,
        title: art.title ?? String(r.experiment_id),
        kind: art.kind ?? "",
        status: art.status ?? "",
        nTrials: art.trials.length,
        asOf: art.asOf ?? strOrNull(r.published_at),
      };
    })
    .sort(
      (a, b) =>
        (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9) ||
        a.id.localeCompare(b.id),
    );

  const lastSync = strOrNull(res.rows[0]?.published_at);
  const experiments = items.filter((i) => i.kind === "EXPERIMENT");
  const withVerdict = experiments.filter((i) => i.verdict).length;

  // Item-4 fix: split the one flat registry list into per-KIND groups. The artifact already carries a
  // NOT-NULL `kind`; this is a PURE DISPLAY grouping by lifecycle (experiment verdicts = immutable,
  // standing = recorded dispositions, doctrine = amendable, quarantine). No registry content touched.
  const GROUPS: { kind: string; label: string }[] = [
    { kind: "EXPERIMENT", label: "Experiments" },
    { kind: "DOCTRINE", label: "Doctrine" },
    { kind: "STANDING", label: "Standing" },
    { kind: "QUARANTINED", label: "Quarantined" },
    { kind: "QUARANTINED_TOMBSTONE", label: "Quarantine — tombstoned" },
    { kind: "PREAMBLE", label: "Preamble" },
  ];
  const knownKinds = new Set(GROUPS.map((g) => g.kind));
  const otherItems = items.filter((i) => !knownKinds.has(i.kind));

  const renderItem = (it: (typeof items)[number]) => (
    <li
      key={it.id}
      className="rounded-md border border-[var(--border-soft)] bg-[var(--card-2)] p-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Link
          href={`/experiments/${encodeURIComponent(it.id)}`}
          className="text-sm font-semibold text-[var(--text)] hover:text-[var(--accent)]"
        >
          {it.title}
        </Link>
        {it.verdict ? (
          <VerdictChip verdict={it.verdict} detail={it.verdictDetail} />
        ) : (
          <span className="rounded bg-[var(--card)] px-1.5 py-0.5 text-[0.6rem] uppercase tracking-wide text-[var(--dim)]">
            {it.kind || "—"}
          </span>
        )}
        <span className="ml-auto text-[0.68rem] text-[var(--dim)] tnum">
          {it.version ? `v${it.version} · ` : ""}
          {ts(it.asOf)}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 text-[0.68rem] text-[var(--dim)]">
        <span>{it.id}</span>
        {it.status && <span>{it.status}</span>}
        {it.nTrials > 0 && (
          <span>
            {it.nTrials} trial{it.nTrials === 1 ? "" : "s"}
          </span>
        )}
      </div>
    </li>
  );

  return (
    <div className="space-y-4">
      <PageHeader
        title="Experiments"
        subtitle="Published registry artifacts. Every statistic is pre-computed by the publisher — the dashboard only renders."
        right={
          <div className="flex items-center gap-2">
            <Link
              href="/experiments/evidence"
              className="rounded-md border border-[var(--border)] px-2.5 py-1 text-xs text-[var(--muted)] hover:text-[var(--accent)]"
            >
              Research evidence →
            </Link>
            <Link
              href="/experiments/analytics"
              className="rounded-md border border-[var(--border)] px-2.5 py-1 text-xs text-[var(--muted)] hover:text-[var(--accent)]"
            >
              Meta-research analytics →
            </Link>
          </div>
        }
      />

      {res.ok && items.length > 0 ? (
        <>
          {/* Item-1: the per-KIND groups are now TABS rather than a vertical stack — reaching
              Standing or Preamble no longer means scrolling past every experiment. The grouping
              is unchanged (still `it.kind`); only which panel is visible changes. */}
          <RegistryTabs
            tabs={[
              ...GROUPS.filter((g) => items.some((i) => i.kind === g.kind)).map((g) => {
                const rows = items.filter((i) => i.kind === g.kind);
                return {
                  key: g.kind,
                  label: g.label,
                  count: rows.length,
                  content: (
                    <SourcedPanel
                      title={`Registry — ${g.label}`}
                      source="experiment_artifacts"
                      lastSync={lastSync}
                      provenance="REGISTRY"
                      cadenceMs={24 * 60 * 60_000}
                      note={
                        g.kind === "EXPERIMENT"
                          ? `${rows.length} experiments (${withVerdict} verdicted)`
                          : `${rows.length} ${g.label.toLowerCase()}`
                      }
                    >
                      <ul className="space-y-2">{rows.map(renderItem)}</ul>
                    </SourcedPanel>
                  ),
                } satisfies RegistryTab;
              }),
              ...(otherItems.length > 0
                ? [
                    {
                      key: "OTHER",
                      label: "Other",
                      count: otherItems.length,
                      content: (
                        <SourcedPanel
                          title="Registry — Other"
                          source="experiment_artifacts"
                          lastSync={lastSync}
                          provenance="REGISTRY"
                          cadenceMs={24 * 60 * 60_000}
                          note={`${otherItems.length} uncategorised`}
                        >
                          <ul className="space-y-2">{otherItems.map(renderItem)}</ul>
                        </SourcedPanel>
                      ),
                    } satisfies RegistryTab,
                  ]
                : []),
            ]}
          />
        </>
      ) : (
        <SourcedPanel
          title="Registry"
          source="experiment_artifacts"
          lastSync={lastSync}
          provenance="NO-FEED"
          cadenceMs={24 * 60 * 60_000}
        >
          <NoFeedBody reason="experiment_artifacts not reachable / no published rows." />
        </SourcedPanel>
      )}

      <Banner tone="info">
        Verdict chips carry REGISTRY provenance and normalize the registry&rsquo;s free-text ruling to
        PASS / PASS-FRAGILE / REJECTED / SHELVED (hover for the raw prose). Non-experiment rows
        (doctrine, standing rules) show their kind instead. No statistic is recomputed here.
      </Banner>
    </div>
  );
}
