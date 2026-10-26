"use client";

import { useActionState } from "react";
import { loginAction, type LoginState } from "./actions";

export function LoginForm({
  initialError = "",
  configIssues = [],
}: {
  initialError?: string;
  configIssues?: string[];
}) {
  const [state, formAction, pending] = useActionState<LoginState, FormData>(
    loginAction,
    { error: initialError }
  );
  const error = state.error ?? "";
  const blocked = configIssues.length > 0;

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <form
        action={formAction}
        className="w-full max-w-sm space-y-4 rounded-lg border border-[var(--border)] bg-[var(--card)] p-6"
      >
        <h1 className="text-xl font-semibold">Trading Bot</h1>
        <p className="text-sm text-[var(--muted)]">Sign in to your dashboard</p>

        {configIssues.length > 0 && (
          <div className="rounded border border-[var(--red)] bg-red-950/30 p-3 text-sm text-[var(--red)]">
            <p className="font-medium">Configuration error</p>
            <ul className="mt-2 list-disc space-y-1 pl-4">
              {configIssues.map((issue) => (
                <li key={issue}>{issue}</li>
              ))}
            </ul>
          </div>
        )}

        {error && <p className="text-sm text-[var(--red)]">{error}</p>}

        <input
          name="email"
          type="email"
          placeholder="Email"
          autoComplete="email"
          className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-2"
          required
          disabled={blocked || pending}
        />
        <input
          name="password"
          type="password"
          placeholder="Password"
          autoComplete="current-password"
          className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-2"
          required
          disabled={blocked || pending}
        />
        <button
          type="submit"
          disabled={pending || blocked}
          className="w-full rounded bg-[var(--green)] py-2 font-medium text-black disabled:opacity-50"
        >
          {pending ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
