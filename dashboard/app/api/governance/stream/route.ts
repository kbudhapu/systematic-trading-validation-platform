import { NextRequest } from "next/server";
import { createClient } from "@supabase/supabase-js";
import type { RealtimeChannel } from "@supabase/supabase-js";
import { requireUser } from "@/lib/supabase/require-user";
import { createDashboardApiClient } from "@/lib/supabase/dashboard-api";
import { getSupabaseEnv } from "@/lib/supabase/env";
import { getTradingEnvironment } from "@/lib/trading-environment";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const STREAM_MAX_MS = 300_000;

type SnapshotRow = {
  generated_at?: string;
  updated_at?: string;
  summary_json?: Record<string, unknown> | null;
  performance_json?: Record<string, unknown> | null;
};

function mergeSnapshotPayload(
  environment: string,
  row: SnapshotRow,
): Record<string, unknown> {
  const summary =
    typeof row.summary_json === "object" && row.summary_json !== null
      ? row.summary_json
      : {};
  const performance =
    typeof row.performance_json === "object" && row.performance_json !== null
      ? row.performance_json
      : {};
  return {
    generated_at: row.generated_at,
    updated_at: row.updated_at,
    environment,
    ...summary,
    performance_metrics: performance,
  };
}

function resolveRealtimeClient() {
  const scoped = createDashboardApiClient();
  if (scoped) {
    return scoped;
  }
  const env = getSupabaseEnv();
  if (!env) {
    return null;
  }
  return createClient(env.url, env.key, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}

export async function GET(request: NextRequest) {
  const user = await requireUser();
  if (!user) {
    return new Response("Unauthorized", { status: 401 });
  }

  const supabase = resolveRealtimeClient();
  if (!supabase) {
    return new Response("Supabase realtime is not configured", { status: 503 });
  }

  const environment = getTradingEnvironment();
  const encoder = new TextEncoder();
  const startedAt = Date.now();

  const stream = new ReadableStream({
    start(controller) {
      let channel: RealtimeChannel | null = null;
      let closed = false;

      const closeStream = () => {
        if (closed) {
          return;
        }
        closed = true;
        if (channel) {
          void supabase.removeChannel(channel);
          channel = null;
        }
        controller.close();
      };

      const pushSnapshot = (row: SnapshotRow) => {
        controller.enqueue(
          encoder.encode(
            `event: snapshot\ndata: ${JSON.stringify(
              mergeSnapshotPayload(environment, row),
            )}\n\n`,
          ),
        );
      };

      const pushError = (message: string) => {
        controller.enqueue(
          encoder.encode(
            `event: error\ndata: ${JSON.stringify({ error: message })}\n\n`,
          ),
        );
      };

      void (async () => {
        const { data, error } = await supabase
          .from("dashboard_summary_snapshots")
          .select("generated_at, summary_json, performance_json, updated_at")
          .eq("environment", environment)
          .maybeSingle();

        if (error) {
          pushError(error.message);
        } else if (data) {
          pushSnapshot(data as SnapshotRow);
        } else {
          controller.enqueue(
            encoder.encode(
              `event: waiting\ndata: ${JSON.stringify({
                environment,
                status: "dashboard_summary_not_ready",
              })}\n\n`,
            ),
          );
        }

        channel = supabase
          .channel(`governance-sse-${environment}-${startedAt}`)
          .on(
            "postgres_changes",
            {
              event: "*",
              schema: "public",
              table: "dashboard_summary_snapshots",
              filter: `environment=eq.${environment}`,
            },
            (payload) => {
              const record = payload.new as SnapshotRow | null;
              if (!record || typeof record !== "object") {
                return;
              }
              pushSnapshot(record);
            },
          )
          .subscribe((status) => {
            if (status === "CHANNEL_ERROR") {
              pushError("realtime_channel_error");
            }
          });

        const abortTimer = setInterval(() => {
          if (request.signal.aborted || Date.now() - startedAt >= STREAM_MAX_MS) {
            clearInterval(abortTimer);
            closeStream();
          }
        }, 1_000);

        request.signal.addEventListener("abort", () => {
          clearInterval(abortTimer);
          closeStream();
        });
      })();
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
    },
  });
}
