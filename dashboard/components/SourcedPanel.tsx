import { ReactNode } from "react";
import { ago, isStale } from "@/lib/format";

export type Provenance = "BROKER" | "DERIVED" | "REGISTRY" | "NO-FEED";

const PROV_LABEL: Record<Provenance, string> = {
  BROKER: "BROKER",
  DERIVED: "DERIVED",
  REGISTRY: "REGISTRY",
  "NO-FEED": "NO-FEED",
};

/**
 * The provenance-first panel primitive. Every data surface in dashboard v2
 * renders through this so the source, last-sync time, staleness and
 * provenance are always visible and consistent.
 *
 * Provenance + staleness use NEUTRAL / DIM styling only — red/green are
 * reserved for danger/safe and P&L sign elsewhere.
 */
export function SourcedPanel({
  title,
  source,
  lastSync,
  provenance,
  cadenceMs,
  note,
  children,
}: {
  title: string;
  source: string;
  lastSync?: string | null;
  provenance: Provenance;
  /** expected refresh cadence; panel dims when data older than 2× this. */
  cadenceMs?: number;
  note?: string;
  children: ReactNode;
}) {
  const noFeed = provenance === "NO-FEED";
  const stale =
    !noFeed && cadenceMs !== undefined ? isStale(lastSync, cadenceMs) : false;

  return (
    <section
      className={`rounded-lg border border-[var(--border)] bg-[var(--card)] ${
        noFeed ? "nofeed" : ""
      }`}
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-[var(--border-soft)] px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-wide text-[var(--text)]">
          {title}
        </h2>
        <span
          className={`prov-badge ${noFeed ? "prov-badge--nofeed" : ""}`}
          title={`Provenance: ${provenance}`}
        >
          {PROV_LABEL[provenance]}
        </span>
        <div className="ml-auto flex items-center gap-3 text-[0.68rem] text-[var(--muted)]">
          <span title="Source table / view">
            <span className="text-[var(--dim)]">src</span> {source}
          </span>
          {!noFeed && (
            <span title={lastSync ?? "no timestamp"}>
              <span className="text-[var(--dim)]">sync</span> {ago(lastSync)}
            </span>
          )}
          {stale && (
            <span
              className="rounded border border-[var(--border)] px-1.5 py-0.5 text-[var(--dim)]"
              title={`Older than 2× the ${Math.round(
                (cadenceMs ?? 0) / 1000,
              )}s cadence`}
            >
              STALE
            </span>
          )}
        </div>
      </header>
      {note && (
        <p className="border-b border-[var(--border-soft)] px-4 py-2 text-xs text-[var(--muted)]">
          {note}
        </p>
      )}
      <div className={`px-4 py-3 ${stale ? "is-stale" : ""}`}>{children}</div>
    </section>
  );
}

/** Shaped NO-FEED body for panels whose upstream is not mirrored yet. */
export function NoFeedBody({ reason }: { reason: string }) {
  return (
    <div className="flex min-h-[72px] flex-col items-center justify-center gap-1 py-4 text-center">
      <div className="text-sm font-medium text-[var(--dim)]">NO FEED</div>
      <div className="max-w-md text-xs text-[var(--dim)]">{reason}</div>
    </div>
  );
}
