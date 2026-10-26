"use client";

/**
 * Shared time-range selector + slicer for series charts (portfolio equity curve, leg return
 * curve). Built once, reused — the presets and the "not enough history" fallback behave
 * identically everywhere: a range with too little data falls back to the FULL available span
 * with an "only N days of history" note, never an empty chart.
 */

export const RANGES = [
  { key: "1D", ms: 24 * 3600_000 },
  { key: "1M", ms: 30 * 24 * 3600_000 },
  { key: "6M", ms: 182 * 24 * 3600_000 },
  { key: "1Y", ms: 365 * 24 * 3600_000 },
  { key: "5Y", ms: 5 * 365 * 24 * 3600_000 },
  { key: "All", ms: Infinity },
] as const;
export type RangeKey = (typeof RANGES)[number]["key"];

export function sliceByRange<T extends { t: string }>(
  base: T[],
  range: RangeKey,
): { view: T[]; truncatedNote: string | null } {
  if (base.length < 2) return { view: base, truncatedNote: null };
  const rangeMs = RANGES.find((r) => r.key === range)?.ms ?? Infinity;
  const lastT = new Date(base[base.length - 1].t).getTime();
  const firstT = new Date(base[0].t).getTime();
  const spanDays = Math.max(1, Math.ceil((lastT - firstT) / 86_400_000));
  if (!Number.isFinite(rangeMs)) return { view: base, truncatedNote: null };
  const cutoff = lastT - rangeMs;
  const sliced = base.filter((p) => new Date(p.t).getTime() >= cutoff);
  if (sliced.length < 2 || firstT >= cutoff) {
    return {
      view: base,
      truncatedNote:
        lastT - firstT < rangeMs
          ? `only ${spanDays} day${spanDays === 1 ? "" : "s"} of history`
          : null,
    };
  }
  return { view: sliced, truncatedNote: null };
}

export function RangeButtons({
  value,
  onChange,
}: {
  value: RangeKey;
  onChange: (r: RangeKey) => void;
}) {
  return (
    <div className="flex overflow-hidden rounded-md border border-[var(--border)]">
      {RANGES.map((r) => (
        <button
          key={r.key}
          type="button"
          onClick={() => onChange(r.key)}
          aria-pressed={value === r.key}
          className={`px-2.5 py-1 text-[0.68rem] transition-colors ${
            value === r.key
              ? "bg-[var(--accent-soft)] font-semibold text-[var(--text)]"
              : "text-[var(--muted)] hover:text-[var(--text)]"
          }`}
        >
          {r.key}
        </button>
      ))}
    </div>
  );
}
