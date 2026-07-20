"use client";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "#0a0a0a",
          color: "#f5f5f5",
          fontFamily: "system-ui, sans-serif",
          padding: "24px",
        }}
      >
        <div style={{ maxWidth: "520px" }}>
          <h1 style={{ color: "#f87171" }}>Application error</h1>
          <p style={{ color: "#a3a3a3" }}>
            Check Vercel env vars: NEXT_PUBLIC_SUPABASE_URL and
            NEXT_PUBLIC_SUPABASE_ANON_KEY (anon key, not service_role).
          </p>
          <pre
            style={{
              marginTop: "16px",
              padding: "12px",
              background: "#171717",
              borderRadius: "8px",
              overflowX: "auto",
              color: "#f87171",
              fontSize: "12px",
            }}
          >
            {error.message}
          </pre>
          {error.digest && (
            <p style={{ color: "#737373", fontSize: "12px" }}>
              Digest: {error.digest}
            </p>
          )}
          <button
            onClick={reset}
            style={{
              marginTop: "16px",
              padding: "8px 16px",
              background: "#4ade80",
              color: "#000",
              border: "none",
              borderRadius: "6px",
              cursor: "pointer",
            }}
          >
            Try again
          </button>
        </div>
      </body>
    </html>
  );
}
