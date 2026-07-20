const ERROR_MESSAGES: Record<string, string> = {
  config:
    "Supabase is not configured. Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY (or ANON_KEY) on Vercel, then redeploy.",
  auth: "Session expired or invalid. Sign in again.",
  credentials: "Invalid email or password.",
  missing: "Email and password are required.",
};

export function resolveLoginError(code?: string) {
  if (!code) return "";
  return ERROR_MESSAGES[code] ?? "";
}
