const API_BASE = "/api/control";

function parseApiError(text: string, statusText: string) {
  if (!text) return statusText;
  try {
    const body = JSON.parse(text) as { error?: string; detail?: string };
    return body.error || body.detail || text;
  } catch {
    return text;
  }
}

async function apiFetch(path: string, options: RequestInit = {}) {
  const res = await fetch(`${API_BASE}/${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers as Record<string, string>),
    },
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(parseApiError(text, res.statusText));
  }
  if (!text) return {};
  return JSON.parse(text);
}

export async function runBacktest() {
  return apiFetch("backtest", { method: "POST" });
}

export async function reloadConfig() {
  const res = await fetch("/api/commands/dispatch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command_type: "RELOAD_CONFIG" }),
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(parseApiError(text, res.statusText));
  }
  if (!text) return {};
  return JSON.parse(text);
}

export async function goLive(strategyId: string) {
  const res = await fetch("/api/commands/dispatch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      command_type: "GO_LIVE",
      payload: { strategy_id: strategyId, confirm: "GO LIVE" },
    }),
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(parseApiError(text, res.statusText));
  }
  if (!text) return {};
  return JSON.parse(text);
}

export async function killSwitch() {
  const res = await fetch("/api/governance/kill-switch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "flatten", confirm: "KILL" }),
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(parseApiError(text, res.statusText));
  }
  if (!text) return {};
  return JSON.parse(text);
}

export async function sendBodEmail() {
  return apiFetch("email/bod", { method: "POST" });
}

export async function sendEodEmail() {
  return apiFetch("email/eod", { method: "POST" });
}

export async function getHealth() {
  return apiFetch("health");
}
