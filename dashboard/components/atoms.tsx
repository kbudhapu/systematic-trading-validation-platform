import { ReactNode } from "react";
import { ageMs, hms } from "@/lib/format";

/**
 * Shared liveness indicator (A1). Green when the source is fresher than
 * `thresholdMs` (default 2 min), red when stale, with an "as of HH:MM:SS".
 * Green/red is a SAFE/DANGER signal here — a stale heartbeat is a danger — which
 * is the sanctioned use of color (alongside P&L sign). ONE freshness source: the
 * timestamp passed in (A3), so the dot and any nearby text can never disagree.
 */
export function HealthDot({
  lastSync,
  thresholdMs = 120_000,
  label = "heartbeat",
}: {
  lastSync: string | null | undefined;
  thresholdMs?: number;
  label?: string;
}) {
  const age = ageMs(lastSync);
  const fresh = age < thresholdMs;
  const color = fresh ? "var(--green)" : "var(--red)";
  return (
    <span
      className="inline-flex items-center gap-1.5"
      title={
        lastSync
          ? `${label} ${fresh ? "live" : "stale"} — last at ${lastSync}`
          : `${label}: no timestamp`
      }
    >
      <span
        className="h-2 w-2 shrink-0 rounded-full"
        style={{ background: color }}
        aria-hidden
      />
      <span className="text-xs text-[var(--muted)]">
        {label} {fresh ? "live" : "stale"}
        <span className="text-[var(--dim)]"> · as of {hms(lastSync)}</span>
      </span>
    </span>
  );
}

/** Notice banner. tone is presentational only; "danger" is the sole red use. */
export function Banner({
  tone = "info",
  children,
}: {
  tone?: "info" | "frozen" | "warn" | "danger";
  children: ReactNode;
}) {
  const styles: Record<string, string> = {
    info: "border-[var(--border)] bg-[var(--card-2)] text-[var(--muted)]",
    frozen: "border-[var(--border)] bg-[var(--accent-soft)] text-[var(--text)]",
    warn: "border-[var(--warn)] bg-[var(--warn-soft)] text-[var(--warn)]",
    danger: "border-[var(--red)] bg-[rgba(239,68,68,0.08)] text-[var(--red)]",
  };
  return (
    <div
      className={`rounded-md border px-3 py-2 text-xs leading-relaxed ${styles[tone]}`}
    >
      {children}
    </div>
  );
}

/** Headline stat. `sign` opts into the P&L red/green convention. */
export function StatTile({
  label,
  value,
  sub,
  valueClass = "text-[var(--text)]",
}: {
  label: string;
  value: string;
  sub?: string;
  valueClass?: string;
}) {
  return (
    <div className="rounded-md border border-[var(--border-soft)] bg-[var(--card-2)] px-3 py-2.5">
      <div className="text-[0.68rem] uppercase tracking-wide text-[var(--muted)]">
        {label}
      </div>
      <div className={`mt-0.5 text-xl font-semibold tnum ${valueClass}`}>
        {value}
      </div>
      {sub && <div className="mt-0.5 text-[0.68rem] text-[var(--dim)]">{sub}</div>}
    </div>
  );
}

/** Neutral chip (e.g. COMMISSIONING tags, verdict states). Never red/green. */
export function Chip({
  children,
  title,
}: {
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className="prov-badge"
      style={{ textTransform: "none", letterSpacing: "0.02em" }}
    >
      {children}
    </span>
  );
}

export function PageHeader({
  title,
  subtitle,
  right,
}: {
  title: string;
  subtitle?: string;
  right?: ReactNode;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-start justify-between gap-2">
      <div>
        <h1 className="text-lg font-semibold tracking-tight text-[var(--text)]">
          {title}
        </h1>
        {subtitle && (
          <p className="mt-0.5 text-sm text-[var(--muted)]">{subtitle}</p>
        )}
      </div>
      {right && <div className="mt-1 shrink-0">{right}</div>}
    </div>
  );
}
