import { BotRun, EquitySnapshot } from "@/lib/types";

/** Coerce Supabase numeric/json rows into plain client-safe props. */
export function normalizeBotRun(row: Record<string, unknown> | null): BotRun | null {
  if (!row) return null;
  return {
    status: String(row.status ?? "unknown"),
    message: String(row.message ?? ""),
    equity: Number(row.equity ?? 0),
    drawdown_pct: Number(row.drawdown_pct ?? 0),
    cycle_ms: Number(row.cycle_ms ?? 0),
    created_at: String(row.created_at ?? ""),
    halted: Boolean(row.halted),
  };
}

export function normalizeSnapshots(
  rows: Record<string, unknown>[] | null
): EquitySnapshot[] {
  return (rows ?? []).map((row) => ({
    recorded_at: String(row.recorded_at ?? ""),
    equity: Number(row.equity ?? 0),
    pct_return: Number(row.pct_return ?? 0),
  }));
}
