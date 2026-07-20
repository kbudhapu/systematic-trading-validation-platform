"use client";

import { useEffect } from "react";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("dashboard_error", error);
  }, [error]);

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <div className="w-full max-w-lg space-y-4 rounded-lg border border-[var(--border)] bg-[var(--card)] p-6">
        <h1 className="text-xl font-semibold text-[var(--red)]">
          Something went wrong
        </h1>
        <p className="text-sm text-[var(--muted)]">
          The dashboard hit a server error. Common causes:
        </p>
        <ul className="list-disc space-y-1 pl-5 text-sm text-[var(--muted)]">
          <li>
            Vercel env vars must be{" "}
            <code className="text-white">NEXT_PUBLIC_SUPABASE_URL</code> and{" "}
            <code className="text-white">NEXT_PUBLIC_SUPABASE_ANON_KEY</code>
          </li>
          <li>Do not use the service_role key as the anon key</li>
          <li>Redeploy after changing environment variables</li>
        </ul>
        <pre className="overflow-x-auto rounded border border-[var(--border)] bg-[var(--bg)] p-3 text-xs text-[var(--red)] whitespace-pre-wrap break-words">
          {error.message || "Unknown server error"}
        </pre>
        {error.digest && (
          <p className="text-xs text-[var(--muted)]">Digest: {error.digest}</p>
        )}
        <div className="flex gap-3">
          <button
            onClick={reset}
            className="rounded bg-[var(--green)] px-4 py-2 text-sm font-medium text-black"
          >
            Try again
          </button>
          <a
            href="/login"
            className="rounded border border-[var(--border)] px-4 py-2 text-sm"
          >
            Back to login
          </a>
        </div>
      </div>
    </div>
  );
}
