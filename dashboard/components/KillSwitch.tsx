"use client";

import { useState } from "react";

type Action = "flatten" | "engage" | "release";

const ACTIONS: { id: Action; label: string; confirm: string; desc: string }[] = [
  {
    id: "flatten",
    label: "Flatten & halt",
    confirm: "KILL",
    desc: "Submit FLATTEN_AND_HALT — closes positions and halts trading.",
  },
  {
    id: "engage",
    label: "Engage kill switch",
    confirm: "ENGAGE",
    desc: "Engage a kill level (blocks new entries).",
  },
  {
    id: "release",
    label: "Release kill switch",
    confirm: "RELEASE",
    desc: "Release a previously engaged kill level.",
  },
];

/**
 * The ONLY live write control in dashboard v2. Posts to the existing
 * /api/governance/kill-switch route (SECURITY DEFINER RPC via
 * dashboard_api_node). The confirm-string UX is preserved: the operator must
 * type the exact word before the request is allowed.
 */
export function KillSwitch({ hasApiKey }: { hasApiKey: boolean }) {
  const [action, setAction] = useState<Action>("flatten");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<
    { ok: boolean; text: string } | null
  >(null);

  const spec = ACTIONS.find((a) => a.id === action)!;
  const armed = confirm === spec.confirm;

  async function submit() {
    if (!armed || busy) return;
    setBusy(true);
    setResult(null);
    try {
      const res = await fetch("/api/governance/kill-switch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, confirm }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        setResult({
          ok: true,
          text: `Accepted: ${body.command?.command_type ?? action} · status ${
            body.status ?? "pending"
          } · channel ${body.channel ?? "—"}`,
        });
        setConfirm("");
      } else {
        setResult({ ok: false, text: body.error ?? `HTTP ${res.status}` });
      }
    } catch (e) {
      setResult({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      {!hasApiKey && (
        <div className="rounded-md border border-[var(--warn)] bg-[var(--warn-soft)] px-3 py-2 text-xs text-[var(--warn)]">
          control writes require SUPABASE_DASHBOARD_API_KEY (dashboard_api_node) — the
          route will fall back to the VPS ingest path or error until it is configured.
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {ACTIONS.map((a) => (
          <button
            key={a.id}
            type="button"
            onClick={() => {
              setAction(a.id);
              setConfirm("");
              setResult(null);
            }}
            className={`rounded-md border px-3 py-1.5 text-xs font-medium ${
              action === a.id
                ? "border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--text)]"
                : "border-[var(--border)] text-[var(--muted)] hover:text-[var(--text)]"
            }`}
          >
            {a.label}
          </button>
        ))}
      </div>

      <p className="text-xs text-[var(--muted)]">{spec.desc}</p>

      <div className="flex flex-wrap items-center gap-2">
        <label className="text-xs text-[var(--muted)]">
          Type <span className="font-mono font-semibold text-[var(--text)]">{spec.confirm}</span> to
          confirm
        </label>
        <input
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          placeholder={spec.confirm}
          spellCheck={false}
          autoComplete="off"
          className="w-32 rounded-md border border-[var(--border)] bg-[var(--bg)] px-2 py-1 font-mono text-sm text-[var(--text)] outline-none focus:border-[var(--accent)]"
        />
        <button
          type="button"
          onClick={submit}
          disabled={!armed || busy}
          className={`rounded-md px-4 py-1.5 text-sm font-semibold transition-colors ${
            armed && !busy
              ? "bg-[var(--red)] text-white hover:opacity-90"
              : "cursor-not-allowed border border-[var(--border)] text-[var(--dim)]"
          }`}
        >
          {busy ? "Submitting…" : "Execute"}
        </button>
      </div>

      {result && (
        <div
          className="rounded-md border px-3 py-2 text-xs"
          style={{
            borderColor: result.ok ? "var(--green)" : "var(--red)",
            color: result.ok ? "var(--green)" : "var(--red)",
            background: result.ok
              ? "rgba(34,197,94,0.08)"
              : "rgba(239,68,68,0.08)",
          }}
        >
          {result.text}
        </div>
      )}

      <p className="text-[0.68rem] text-[var(--dim)]">
        Commands are enqueued on the Supabase control bus and execute only when the bot next
        polls the bus — acceptance here is not immediate execution.
      </p>
    </div>
  );
}
