import { requireDashboardApiClient } from "@/lib/supabase/dashboard-api";
import { randomUUID } from "crypto";

export type DispatchCommandInput = {
  commandType: string;
  /** Per-type confirm token (KILL / ENGAGE / RELEASE / GO_LIVE / RELOAD). The DB RPC
   *  enforces it authoritatively; route handlers keep a UX check for defense in depth. */
  confirm?: string;
  payload?: Record<string, unknown>;
  requestedBy?: string;
  idempotencyKey?: string;
  commandId?: string;
};

export type DispatchCommandResult = {
  status: string;
  channel: "supabase_rpc";
  command_id: string;
  command_type: string;
};

/**
 * Enqueue a control command through the SECURITY DEFINER RPC `enqueue_control_command`
 * (migration 016). SERVER-SIDE ONLY: uses the scoped `dashboard_api_node` key via
 * requireDashboardApiClient(). The DB function whitelists command_type, enforces the
 * confirm token, and validates the payload — it is the authoritative gate.
 *
 * There is deliberately NO browser-client insert path and NO VPS/SQLite fallback:
 *   - direct `control_commands` INSERT by `authenticated` was removed in migration 015 (F1);
 *   - the VPS sidecar (/commands/ingest) is not deployed (CP4) and a local-only staged
 *     command would be invisible to the dashboard (CP2 dual-bus divergence, F7).
 * On failure this THROWS so the UI surfaces it — never a silent dead-fail or hidden write.
 * Requires SUPABASE_DASHBOARD_API_KEY to be configured (else requireDashboardApiClient throws).
 */
export async function dispatchControlCommand(
  input: DispatchCommandInput,
): Promise<DispatchCommandResult> {
  const commandId = input.commandId ?? randomUUID();
  const idempotencyKey =
    input.idempotencyKey ?? `${input.commandType}:${commandId}`;
  const supabase = requireDashboardApiClient();

  const { data, error } = await supabase.rpc("enqueue_control_command", {
    p_command_type: input.commandType,
    p_payload: input.payload ?? {},
    p_confirm: input.confirm ?? null,
    p_requested_by: input.requestedBy ?? "dashboard",
    p_idempotency_key: idempotencyKey,
  });

  if (error) {
    throw new Error(`enqueue_control_command failed: ${error.message}`);
  }

  return {
    status: "pending",
    channel: "supabase_rpc",
    command_id: String(data),
    command_type: input.commandType,
  };
}
