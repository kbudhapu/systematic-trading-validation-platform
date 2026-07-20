import { createServerClient, type CookieOptions } from "@supabase/ssr";
import { cookies } from "next/headers";
import { requireSupabaseEnv } from "@/lib/supabase/env";

type CookieToSet = { name: string; value: string; options: CookieOptions };

export async function createClient() {
  const { url, key } = requireSupabaseEnv();
  const cookieStore = await cookies();
  return createServerClient(url, key, {
      cookies: {
        getAll() {
          return cookieStore.getAll();
        },
        setAll(cookiesToSet: CookieToSet[]) {
          try {
            cookiesToSet.forEach(({ name, value, options }) =>
              cookieStore.set(name, value, options)
            );
          } catch {
            /* Server Component — ignore */
          }
        },
      },
    }
  );
}
