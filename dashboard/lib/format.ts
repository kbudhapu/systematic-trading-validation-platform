/**
 * Shared display formatters for dashboard v2.
 *
 * Rules enforced here:
 *  - null / undefined / zero-price renders the em-dash "—", never "$0".
 *  - P&L sign carries green(+)/red(-) — the ONLY place color encodes sign.
 *  - Provenance / staleness never use color here (handled by neutral CSS).
 *
 * NOTE: these are pure display helpers. No inferential statistics are
 * computed anywhere in this file.
 */

export const DASH = "—";

const usd0 = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const usd2 = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function isBlank(n: number | null | undefined): boolean {
  return n === null || n === undefined || Number.isNaN(n);
}

/** Currency with no cents. null/undefined -> "—". */
export function currency(n: number | null | undefined): string {
  if (isBlank(n)) return DASH;
  return usd0.format(n as number);
}

/** Currency with cents. null/undefined -> "—". */
export function currency2(n: number | null | undefined): string {
  if (isBlank(n)) return DASH;
  return usd2.format(n as number);
}

/**
 * Entry / fill price. A literal 0.0 price is a recording defect, not a real
 * fill, so it renders "—" (per the phantom-row finding).
 */
export function price(n: number | null | undefined): string {
  if (isBlank(n) || n === 0) return DASH;
  return usd2.format(n as number);
}

/** Signed P&L string. null -> "—". Zero -> "$0" (a real zero P&L is valid). */
export function signedCurrency(n: number | null | undefined): string {
  if (isBlank(n)) return DASH;
  const v = n as number;
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return `${sign}${usd0.format(Math.abs(v))}`;
}

/** Percent, signed. null -> "—". */
export function percent(n: number | null | undefined, digits = 2): string {
  if (isBlank(n)) return DASH;
  const v = n as number;
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(digits)}%`;
}

/** Plain integer quantity. null -> "—". */
export function qty(n: number | null | undefined): string {
  if (isBlank(n)) return DASH;
  return new Intl.NumberFormat("en-US").format(n as number);
}

/**
 * Tailwind text-color class for a P&L sign. green/red are RESERVED for this
 * (and danger/safe) only. Zero / null -> neutral.
 */
export function pnlClass(n: number | null | undefined): string {
  if (isBlank(n) || n === 0) return "text-[var(--text)]";
  return (n as number) > 0 ? "text-[var(--green)]" : "text-[var(--red)]";
}

/** Absolute + short timestamp. null/empty -> "—". */
export function ts(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toISOString().replace("T", " ").slice(0, 19) + "Z";
}

/** Just the HH:MM:SS (UTC) of a timestamp, for compact "as of" labels. null -> "—". */
export function hms(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toISOString().slice(11, 19);
}

/** Age in ms of a timestamp (Infinity when missing/invalid). Single freshness source. */
export function ageMs(iso: string | null | undefined): number {
  if (!iso) return Infinity;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return Infinity;
  return Math.max(0, Date.now() - d.getTime());
}

/** Coarse "time ago" for freshness hints. Neutral wording only. */
export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown";
  const ms = Date.now() - d.getTime();
  if (ms < 0) return "just now";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ago`;
  const days = Math.floor(h / 24);
  return `${days}d ago`;
}

/** True when `iso` is older than `2 ×` the expected refresh cadence. */
export function isStale(
  iso: string | null | undefined,
  cadenceMs: number,
): boolean {
  if (!iso) return true;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return true;
  return Date.now() - d.getTime() > 2 * cadenceMs;
}
