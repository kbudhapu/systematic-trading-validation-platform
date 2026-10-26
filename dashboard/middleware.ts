import { createServerClient, type CookieOptions } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";
import { getSupabaseEnv, getSupabaseConfigIssues } from "@/lib/supabase/env";
import { withSupabaseCookies } from "@/lib/supabase/middleware";

type CookieToSet = { name: string; value: string; options: CookieOptions };

export async function middleware(request: NextRequest) {
  const path = request.nextUrl.pathname;
  const isLogin = path.startsWith("/login");
  const PROTECTED = [
    "/dashboard",
    "/portfolio",
    "/legs",
    "/experiments",
    "/pipeline",
    "/ops",
    "/controls",
  ];
  const isDashboard = PROTECTED.some(
    (p) => path === p || path.startsWith(p + "/"),
  );
  const env = getSupabaseEnv();

  if (!env || getSupabaseConfigIssues().length > 0) {
    if (isDashboard) {
      const login = new URL("/login", request.url);
      login.searchParams.set("error", "config");
      return NextResponse.redirect(login);
    }
    return NextResponse.next();
  }

  let supabaseResponse = NextResponse.next({ request });

  try {
    const supabase = createServerClient(env.url, env.key, {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll(cookiesToSet: CookieToSet[]) {
          cookiesToSet.forEach(({ name, value }) =>
            request.cookies.set(name, value)
          );
          supabaseResponse = NextResponse.next({ request });
          cookiesToSet.forEach(({ name, value, options }) =>
            supabaseResponse.cookies.set(name, value, options)
          );
        },
      },
    });

    const {
      data: { user },
    } = await supabase.auth.getUser();

    const isAuth = !!user;

    if (!isAuth && isDashboard) {
      return withSupabaseCookies(
        supabaseResponse,
        NextResponse.redirect(new URL("/login", request.url))
      );
    }
    if (isAuth && isLogin) {
      return withSupabaseCookies(
        supabaseResponse,
        NextResponse.redirect(new URL("/portfolio", request.url))
      );
    }

    return supabaseResponse;
  } catch {
    if (isDashboard) {
      return NextResponse.redirect(new URL("/login?error=auth", request.url));
    }
    return NextResponse.next();
  }
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)",
  ],
};
