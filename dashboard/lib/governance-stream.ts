"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { RealtimeChannel } from "@supabase/supabase-js";
import { createClient } from "@/lib/supabase/client";
import { getTradingEnvironment } from "@/lib/trading-environment";

export type GovernanceSnapshot = {
  updated_at?: string;
  generated_at?: string;
  environment?: string;
  degradation_mode?: string;
  active_kill_switches?: Array<Record<string, unknown>>;
  maintenance_jobs?: Record<string, Record<string, unknown>>;
  ai_policy_states?: Record<string, Record<string, unknown>>;
  performance_metrics?: Record<string, unknown>;
  wal_backlog_depth?: number;
  shadow_matrix_lag_seconds?: number | null;
  tape_latch_active?: boolean;
  [key: string]: unknown;
};

type StreamStatus = "connecting" | "live" | "error" | "closed";

type SnapshotRow = {
  generated_at?: string;
  updated_at?: string;
  environment?: string;
  summary_json?: Record<string, unknown> | null;
  performance_json?: Record<string, unknown> | null;
};

function mergeSnapshotRow(row: SnapshotRow): GovernanceSnapshot {
  const summary =
    typeof row.summary_json === "object" && row.summary_json !== null
      ? row.summary_json
      : {};
  const performance =
    typeof row.performance_json === "object" && row.performance_json !== null
      ? row.performance_json
      : {};

  const walDepth =
    typeof performance.wal_backlog_depth === "number"
      ? performance.wal_backlog_depth
      : typeof summary.wal_backlog_depth === "number"
        ? summary.wal_backlog_depth
        : undefined;

  const shadowLag =
    typeof performance.shadow_matrix_lag_seconds === "number"
      ? performance.shadow_matrix_lag_seconds
      : performance.shadow_matrix_lag_seconds === null
        ? null
        : undefined;

  const tapeLatch =
    typeof performance.tape_latch_active === "boolean"
      ? performance.tape_latch_active
      : typeof summary.tape_latch_active === "boolean"
        ? summary.tape_latch_active
        : undefined;

  return {
    generated_at: row.generated_at,
    updated_at: row.updated_at,
    environment: row.environment,
    ...summary,
    performance_metrics: performance,
    wal_backlog_depth: walDepth,
    shadow_matrix_lag_seconds: shadowLag,
    tape_latch_active: tapeLatch,
  };
}

export function useGovernanceSnapshot() {
  const [snapshot, setSnapshot] = useState<GovernanceSnapshot | null>(null);
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [error, setError] = useState<string | null>(null);
  const channelRef = useRef<RealtimeChannel | null>(null);

  const connect = useCallback(() => {
    channelRef.current?.unsubscribe();
    channelRef.current = null;
    setStatus("connecting");
    setError(null);

    const supabase = createClient();
    const environment = getTradingEnvironment();

    void fetchGovernanceSnapshot()
      .then((payload) => {
        setSnapshot(payload);
        setStatus("live");
      })
      .catch((fetchError) => {
        setError(
          fetchError instanceof Error ? fetchError.message : String(fetchError),
        );
        setStatus("error");
      });

    const channel = supabase
      .channel(`dashboard-summary-${environment}`)
      .on(
        "postgres_changes",
        {
          event: "*",
          schema: "public",
          table: "dashboard_summary_snapshots",
          filter: `environment=eq.${environment}`,
        },
        (payload) => {
          const record = payload.new as Record<string, unknown> | null;
          if (!record || typeof record !== "object") {
            return;
          }
          setSnapshot(
            mergeSnapshotRow({
              generated_at: String(record.generated_at ?? ""),
              updated_at: String(record.updated_at ?? ""),
              environment: String(record.environment ?? environment),
              summary_json:
                typeof record.summary_json === "object" &&
                record.summary_json !== null
                  ? (record.summary_json as Record<string, unknown>)
                  : {},
              performance_json:
                typeof record.performance_json === "object" &&
                record.performance_json !== null
                  ? (record.performance_json as Record<string, unknown>)
                  : {},
            }),
          );
          setStatus("live");
          setError(null);
        },
      )
      .subscribe((subscriptionStatus) => {
        if (subscriptionStatus === "SUBSCRIBED") {
          setStatus("live");
        }
        if (subscriptionStatus === "CHANNEL_ERROR") {
          setStatus("error");
          setError("realtime_channel_error");
        }
        if (subscriptionStatus === "CLOSED") {
          setStatus("closed");
        }
      });

    channelRef.current = channel;
  }, []);

  useEffect(() => {
    connect();
    return () => {
      channelRef.current?.unsubscribe();
      channelRef.current = null;
    };
  }, [connect]);

  return { snapshot, status, error, reconnect: connect };
}

export async function fetchGovernanceSnapshot(): Promise<GovernanceSnapshot> {
  const response = await fetch("/api/governance/snapshot", { cache: "no-store" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof payload.error === "string" ? payload.error : response.statusText,
    );
  }
  return payload as GovernanceSnapshot;
}
