import type { SupabaseClient } from "@supabase/supabase-js";

/**
 * Safe read helpers for dashboard v2.
 *
 * Phase-1 tables (leg_attribution_snapshots, experiment_artifacts,
 * dashboard_summary_snapshots, …) may not be reachable in every mirror. Every
 * read goes through `safeRows` so a missing table / RLS denial degrades to a
 * NO-FEED render instead of crashing the page. We NEVER invent numbers.
 */

export type Row = Record<string, unknown>;

export type SafeResult<T = Row> = {
  rows: T[];
  ok: boolean;
  error: string | null;
};

/** Await a PostgREST query and coerce failures into an empty, flagged result. */
export async function safeRows<T = Row>(
  query: PromiseLike<{ data: T[] | null; error: { message: string } | null }>,
): Promise<SafeResult<T>> {
  try {
    const { data, error } = await query;
    if (error) {
      return { rows: [], ok: false, error: error.message };
    }
    return { rows: data ?? [], ok: true, error: null };
  } catch (e) {
    return { rows: [], ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

export function firstRow<T = Row>(res: SafeResult<T>): T | null {
  return res.rows.length > 0 ? res.rows[0] : null;
}

/**
 * PostgREST row-cap safety (see #282, #282-sweep).
 *
 * PostgREST enforces `db-max-rows` (1000 on this project) on EVERY response, REGARDLESS of
 * `.limit(N)`. So a fetch `.order(col, { ascending: true }).limit(N>1000)` silently returns the
 * OLDEST 1000 rows — never the newest — freezing curves and freshness badges on stale data with
 * no error. NEVER use ascending + a >1000 limit for a latest value or a time-series.
 *
 * Route every "latest / freshness / as-of" read through `latestRow` and every time-series curve
 * through `capSafeSeries`. Both order DESCending so the cap keeps the NEWEST rows.
 */

/** A query builder we can order + limit, then await. The supabase-js filter builder satisfies it. */
export interface CapSafeBuilder<T> {
  order(column: string, options: { ascending: boolean }): CapSafeBuilder<T>;
  limit(
    count: number,
  ): PromiseLike<{ data: T[] | null; error: { message: string } | null }>;
}

/**
 * Cap-immune LATEST single row: applies `.order(col, desc).limit(1)`. Correct by construction —
 * one row can never be truncated by the cap. Use for freshness / as-of / headline values.
 */
export async function latestRow<T = Row>(
  builder: CapSafeBuilder<T>,
  orderCol: string,
): Promise<{ row: T | null; ok: boolean; error: string | null }> {
  const res = await safeRows<T>(builder.order(orderCol, { ascending: false }).limit(1));
  return { row: res.rows[0] ?? null, ok: res.ok, error: res.error };
}

/**
 * Cap-safe time-series: fetches `.order(col, desc).limit(cap)` (newest-first) and returns the rows
 * REVERSED to chronological, plus `latest` (the newest row, pre-reverse). The cap keeps the NEWEST
 * `cap` rows. NOTE: a series with >`cap` rows of genuinely-needed history needs keyset pagination —
 * this returns only the newest `cap`; surface that limitation rather than assume completeness.
 */
export async function capSafeSeries<T = Row>(
  builder: CapSafeBuilder<T>,
  orderCol: string,
  cap = 1000,
): Promise<{ chronological: T[]; latest: T | null; ok: boolean; error: string | null }> {
  const res = await safeRows<T>(builder.order(orderCol, { ascending: false }).limit(cap));
  return {
    chronological: [...res.rows].reverse(),
    latest: res.rows[0] ?? null,
    ok: res.ok,
    error: res.error,
  };
}

/** num-or-null: preserves NULL (never coerces a missing value to 0). */
export function numOrNull(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isNaN(n) ? null : n;
}

export function strOrNull(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  return String(v);
}

/** Resolve the active trading-environment label from strategies (best-effort). */
export async function resolveEnvironment(
  supabase: SupabaseClient,
): Promise<string> {
  const res = await safeRows<Row>(
    supabase.from("strategies").select("environment, name"),
  );
  const portfolio = res.rows.find((r) => r.name === "portfolio");
  return String(
    portfolio?.environment ?? res.rows[0]?.environment ?? "paper",
  );
}
