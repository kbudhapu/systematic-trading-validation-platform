import { LoginForm } from "./LoginForm";
import { resolveLoginError } from "./errors";
import { getSupabaseConfigIssues } from "@/lib/supabase/env";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const params = await searchParams;
  const configIssues = getSupabaseConfigIssues();
  const initialError = resolveLoginError(params.error);

  return (
    <LoginForm initialError={initialError} configIssues={configIssues} />
  );
}
