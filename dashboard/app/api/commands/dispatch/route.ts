import { NextRequest, NextResponse } from "next/server";
import { requireUser } from "@/lib/supabase/require-user";
import { dispatchControlCommand } from "@/lib/command-bus";
import { randomUUID } from "crypto";

export const dynamic = "force-dynamic";

type DispatchBody = {
  command_type?: string;
  payload?: Record<string, unknown>;
  idempotency_key?: string;
};

export async function POST(request: NextRequest) {
  const user = await requireUser();
  if (!user) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const body = (await request.json().catch(() => ({}))) as DispatchBody;
  const commandType = body.command_type?.trim();
  if (!commandType) {
    return NextResponse.json({ error: "command_type is required" }, { status: 400 });
  }

  const operator = user.email || "dashboard";
  const commandId = randomUUID();
  const idempotencyKey =
    body.idempotency_key ?? `${commandType}:${operator}:${commandId}`;

  try {
    const result = await dispatchControlCommand({
      commandType,
      payload: body.payload ?? {},
      requestedBy: operator,
      idempotencyKey,
      commandId,
    });
    return NextResponse.json(result);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
