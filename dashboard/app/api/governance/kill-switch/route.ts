import { NextRequest, NextResponse } from "next/server";
import { requireUser } from "@/lib/supabase/require-user";
import { dispatchControlCommand } from "@/lib/command-bus";
import { randomUUID } from "crypto";

export const dynamic = "force-dynamic";

type KillSwitchBody = {
  action?: "flatten" | "engage" | "release";
  confirm?: string;
  // Canonical control-command key (migration 020; kill_level retired). One of the
  // RiskEscalationLevel values: ENTRY_GATE_HALT | STRATEGY_LIQUIDATE | GLOBAL_FLATTEN_AND_HALT | NOMINAL.
  escalation_level?: string;
  scope_key?: string;
  operator?: string;
  rationale?: string;
};

export async function POST(request: NextRequest) {
  const user = await requireUser();
  if (!user) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const body = (await request.json().catch(() => ({}))) as KillSwitchBody;
  const action = body.action ?? "flatten";
  const operator = body.operator?.trim() || user.email || "dashboard";

  let commandType: string;
  let payload: Record<string, unknown>;
  let confirmRequired: string;

  if (action === "flatten") {
    if (body.confirm !== "KILL") {
      return NextResponse.json(
        { error: 'Confirmation must be exactly "KILL"' },
        { status: 400 },
      );
    }
    commandType = "FLATTEN_AND_HALT";
    payload = { source: "dashboard_kill_switch" };
    confirmRequired = "KILL";
  } else if (action === "engage") {
    if (body.confirm !== "ENGAGE") {
      return NextResponse.json(
        { error: 'Confirmation must be exactly "ENGAGE"' },
        { status: 400 },
      );
    }
    const escalationLevel = (body.escalation_level ?? "ENTRY_GATE_HALT").toUpperCase();
    commandType = "ENGAGE_KILL_SWITCH";
    payload = {
      escalation_level: escalationLevel,
      scope_key: body.scope_key ?? "GLOBAL",
      operator,
      rationale: body.rationale ?? "Dashboard governance engage",
    };
    confirmRequired = "ENGAGE";
  } else if (action === "release") {
    if (body.confirm !== "RELEASE") {
      return NextResponse.json(
        { error: 'Confirmation must be exactly "RELEASE"' },
        { status: 400 },
      );
    }
    const escalationLevel = (body.escalation_level ?? "ENTRY_GATE_HALT").toUpperCase();
    commandType = "RELEASE_KILL_SWITCH";
    payload = {
      escalation_level: escalationLevel,
      scope_key: body.scope_key ?? "GLOBAL",
      operator,
      rationale: body.rationale ?? "Dashboard governance release",
    };
    confirmRequired = "RELEASE";
  } else {
    return NextResponse.json({ error: "Unknown action" }, { status: 400 });
  }

  const commandId = randomUUID();
  const idempotencyKey = `${action}:${operator}:${commandId}`;

  try {
    const result = await dispatchControlCommand({
      commandType,
      confirm: confirmRequired,
      payload,
      requestedBy: operator,
      idempotencyKey,
      commandId,
    });
    return NextResponse.json({
      status: result.status,
      channel: result.channel,
      command: {
        command_id: result.command_id,
        command_type: result.command_type,
        status: "pending",
        created_at: new Date().toISOString(),
      },
      action,
      confirm: confirmRequired,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
