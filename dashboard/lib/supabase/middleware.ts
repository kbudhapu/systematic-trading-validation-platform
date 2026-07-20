import { NextResponse } from "next/server";

/** Preserve Supabase session cookies when middleware returns a redirect. */
export function withSupabaseCookies(
  supabaseResponse: NextResponse,
  response: NextResponse
): NextResponse {
  supabaseResponse.cookies.getAll().forEach(({ name, value }) => {
    response.cookies.set(name, value);
  });
  return response;
}
