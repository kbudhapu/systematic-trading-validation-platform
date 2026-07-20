import { createClient, type SupabaseClient } from "@supabase/supabase-js";

let cachedClient: SupabaseClient | null | undefined;

/** Server-only scoped client for dashboard_api_node writes (RPC + control_commands). */
export function createDashboardApiClient(): SupabaseClient | null {
  if (cachedClient !== undefined) {
    return cachedClient;
  }

  const url = process.env.NEXT_PUBLIC_SUPABASE_URL?.trim().replace(/\/+$/, "");
  const key = process.env.SUPABASE_DASHBOARD_API_KEY?.trim();
  if (!url || !key) {
    cachedClient = null;
    return null;
  }
  if (key.startsWith("sb_secret_")) {
    throw new Error(
      "SUPABASE_DASHBOARD_API_KEY must be the dashboard_api_node JWT, not service_role.",
    );
  }

  cachedClient = createClient(url, key, {
    auth: {
      persistSession: false,
      autoRefreshToken: false,
    },
  });
  return cachedClient;
}

export function requireDashboardApiClient(): SupabaseClient {
  const client = createDashboardApiClient();
  if (!client) {
    throw new Error(
      "SUPABASE_DASHBOARD_API_KEY is not configured for dashboard_api_node mutations.",
    );
  }
  return client;
}
