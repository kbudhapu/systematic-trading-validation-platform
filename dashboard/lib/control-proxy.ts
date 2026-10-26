/** Server-only control API URL (never exposed to the browser). */
export function resolveControlApiUrl():
  | { ok: true; url: string; token: string }
  | { ok: false; status: number; message: string } {
  const url = (
    process.env.CONTROL_API_URL?.trim() ||
    process.env.NEXT_PUBLIC_CONTROL_API_URL?.trim() ||
    ""
  ).replace(/\/+$/, "");

  const token = process.env.CONTROL_API_TOKEN?.trim() ?? "";

  if (!url) {
    if (process.env.NODE_ENV === "development") {
      return { ok: true, url: "http://localhost:8000", token };
    }
    return {
      ok: false,
      status: 503,
      message:
        "Control API is not configured. Set CONTROL_API_URL on Vercel to your droplet HTTPS endpoint after trading-api is exposed.",
    };
  }

  if (!token) {
    return {
      ok: false,
      status: 503,
      message: "CONTROL_API_TOKEN is not configured on Vercel.",
    };
  }

  if (url.includes("localhost") && process.env.VERCEL === "1") {
    return {
      ok: false,
      status: 503,
      message:
        "CONTROL_API_URL cannot be localhost on Vercel. Point it at your droplet API (HTTPS).",
    };
  }

  return { ok: true, url, token };
}

export function safeRedirectPath(next: string | null, fallback = "/dashboard") {
  if (!next || !next.startsWith("/") || next.startsWith("//")) {
    return fallback;
  }
  return next;
}
