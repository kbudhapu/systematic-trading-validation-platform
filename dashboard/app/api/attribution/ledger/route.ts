import { noCacheJson } from "@/lib/no-cache";
import { createClient } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const rawLimit = Number.parseInt(searchParams.get("limit") ?? "100", 10);
  const limit = Number.isFinite(rawLimit)
    ? Math.min(Math.max(rawLimit, 1), 500)
    : 100;

  const supabase = await createClient();
  const { data, error } = await supabase
    .from("live_attribution_ledger")
    .select(
      "trade_id, timestamp, strategy_id, symbol, side, qty, pnl, regime_id, session_type, liquidity_state, execution_tactic, champion_version_id, ai_policy_execution_state, slippage_pct, metadata_json",
    )
    .order("timestamp", { ascending: false })
    .limit(limit);

  if (error) {
    return noCacheJson({ error: error.message }, { status: 500 });
  }

  return noCacheJson({ rows: data ?? [], limit });
}
