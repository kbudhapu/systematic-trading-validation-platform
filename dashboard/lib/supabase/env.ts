/** Resolve Supabase URL + client key from supported env var names. */
export function getSupabaseEnv() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL?.trim().replace(/\/+$/, "");
  const key =
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY?.trim() ||
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY?.trim();

  if (!url || !key) {
    return null;
  }
  if (url.includes("/rest/v1")) {
    return null;
  }
  return { url, key };
}

export function getSupabaseConfigIssues(): string[] {
  const issues: string[] = [];
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL?.trim();
  const key =
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY?.trim() ||
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY?.trim();

  if (!url) {
    issues.push(
      "Missing NEXT_PUBLIC_SUPABASE_URL (or NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY companion)."
    );
  } else if (url.includes("/rest/v1")) {
    issues.push("NEXT_PUBLIC_SUPABASE_URL must not include /rest/v1.");
  }

  if (!key) {
    issues.push(
      "Missing NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY or NEXT_PUBLIC_SUPABASE_ANON_KEY."
    );
  } else if (key.startsWith("sb_secret_")) {
    issues.push(
      "Client key is a service_role secret. Use the publishable or anon public key."
    );
  }

  return issues;
}

export function requireSupabaseEnv() {
  const issues = getSupabaseConfigIssues();
  if (issues.length > 0) {
    throw new Error(issues.join(" "));
  }
  return getSupabaseEnv()!;
}
