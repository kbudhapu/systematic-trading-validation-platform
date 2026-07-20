import { NextRequest, NextResponse } from "next/server";
import { resolveControlApiUrl } from "@/lib/control-proxy";
import { requireUser } from "@/lib/supabase/require-user";

const PROXY_TIMEOUT_MS = 120_000;

function fetchWithTimeout(url: string, init: RequestInit, timeoutMs: number) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, { ...init, signal: controller.signal }).finally(() =>
    clearTimeout(timer)
  );
}

async function proxyRequest(
  request: NextRequest,
  path: string[],
  method: "GET" | "POST"
) {
  const user = await requireUser();
  if (!user) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const resolved = resolveControlApiUrl();
  if (!resolved.ok) {
    return NextResponse.json(
      { error: resolved.message },
      { status: resolved.status }
    );
  }

  const target = `${resolved.url}/${path.join("/")}`;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${resolved.token}`,
  };

  let body: string | undefined;
  if (method === "POST") {
    headers["Content-Type"] = "application/json";
    body = await request.text();
  }

  try {
    const res = await fetchWithTimeout(
      target,
      {
        method,
        headers,
        body,
      },
      PROXY_TIMEOUT_MS
    );
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (e) {
    const message =
      e instanceof Error ? e.message : "Control API request failed";
    return NextResponse.json(
      { error: `Control API unreachable: ${message}` },
      { status: 502 }
    );
  }
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const { path } = await params;
  return proxyRequest(request, path, "POST");
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const { path } = await params;
  return proxyRequest(request, path, "GET");
}
