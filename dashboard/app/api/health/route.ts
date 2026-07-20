import { NextResponse } from "next/server";
import { getSupabaseConfigIssues, getSupabaseEnv } from "@/lib/supabase/env";
import { resolveControlApiUrl } from "@/lib/control-proxy";
import { requireUser } from "@/lib/supabase/require-user";

export async function GET() {
  const issues = getSupabaseConfigIssues();
  const env = getSupabaseEnv();
  const control = resolveControlApiUrl();
  const user = await requireUser();

  return NextResponse.json({
    ok: issues.length === 0,
    supabase: {
      configured: Boolean(env),
      issues,
    },
    controlApi: {
      configured: control.ok,
      message: control.ok ? "ready" : control.message,
    },
    session: {
      authenticated: Boolean(user),
    },
  });
}
