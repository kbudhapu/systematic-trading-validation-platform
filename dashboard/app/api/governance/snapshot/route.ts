import { noCacheJson } from "@/lib/no-cache";
import { createClient } from "@/lib/supabase/server";
import { getTradingEnvironment } from "@/lib/trading-environment";

export const dynamic = "force-dynamic";

export async function GET() {
  const supabase = await createClient();
  const environment = getTradingEnvironment();

  const { data, error } = await supabase
    .from("dashboard_summary_snapshots")
    .select("generated_at, summary_json, performance_json, updated_at")
    .eq("environment", environment)
    .maybeSingle();

  if (error) {
    return noCacheJson({ error: error.message }, { status: 500 });
  }
  if (!data) {
    return noCacheJson(
      { error: "dashboard_summary_not_ready", environment },
      { status: 404 },
    );
  }

  return noCacheJson({
    generated_at: data.generated_at,
    updated_at: data.updated_at,
    environment,
    ...(typeof data.summary_json === "object" && data.summary_json !== null
      ? data.summary_json
      : {}),
    performance_metrics:
      typeof data.performance_json === "object" && data.performance_json !== null
        ? data.performance_json
        : {},
    wal_backlog_depth:
      typeof data.performance_json === "object" &&
      data.performance_json !== null &&
      typeof (data.performance_json as Record<string, unknown>).wal_backlog_depth ===
        "number"
        ? (data.performance_json as Record<string, unknown>).wal_backlog_depth
        : undefined,
    shadow_matrix_lag_seconds:
      typeof data.performance_json === "object" && data.performance_json !== null
        ? (data.performance_json as Record<string, unknown>).shadow_matrix_lag_seconds
        : undefined,
    tape_latch_active:
      typeof data.performance_json === "object" &&
      data.performance_json !== null &&
      typeof (data.performance_json as Record<string, unknown>).tape_latch_active ===
        "boolean"
        ? (data.performance_json as Record<string, unknown>).tape_latch_active
        : undefined,
  });
}
