import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { requireUser } from "@/lib/supabase/require-user";

export async function GET() {
  const user = await requireUser();
  if (!user) {
    return NextResponse.json({ authenticated: false }, { status: 401 });
  }

  const cookieStore = await cookies();
  const authCookieCount = cookieStore
    .getAll()
    .filter((c) => c.name.includes("auth-token")).length;

  return NextResponse.json({
    authenticated: true,
    email: user.email ?? null,
    userId: user.id,
    authCookieCount,
  });
}
