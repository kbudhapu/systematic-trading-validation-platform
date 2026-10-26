import { createClient } from "@/lib/supabase/server";
import { safeRows, strOrNull, type Row } from "@/lib/dashboard-data";
import { SourcedPanel, NoFeedBody } from "@/components/SourcedPanel";
import { PageHeader, Banner } from "@/components/atoms";
import { KillSwitch } from "@/components/KillSwitch";
import { ts } from "@/lib/format";

export const dynamic = "force-dynamic";

const FROZEN_BANNER = "Frozen pending Unfreeze Gates A–D (CP7_findings.md)";

const FROZEN_CONTROLS: { title: string; desc: string }[] = [
  { title: "Strategy enable / disable", desc: "Per-strategy toggles." },
  { title: "Strategy parameters", desc: "Runtime param edits + version lock." },
  { title: "Go-Live promotion", desc: "Paper → live promotion." },
  { title: "Backtest launcher", desc: "On-demand backtest runs." },
  { title: "Email / notifier config", desc: "Digest + alert channel settings." },
];

export default async function ControlsPage() {
  const supabase = await createClient();

  const hasApiKey = Boolean(process.env.SUPABASE_DASHBOARD_API_KEY?.trim());

  const cmdRes = await safeRows<Row>(
    supabase
      .from("control_commands")
      .select("command_id, command_type, status, requested_by, created_at, processed_at")
      .order("created_at", { ascending: false })
      .limit(5),
  );
  const last = cmdRes.rows[0] ?? null;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Controls"
        subtitle="The kill switch is the only live write control. Everything else is frozen."
      />

      {/* Kill switch — LIVE */}
      <SourcedPanel
        title="Kill switch (live)"
        source="/api/governance/kill-switch → control_commands"
        lastSync={strOrNull(last?.created_at)}
        provenance="BROKER"
        cadenceMs={60 * 60_000}
      >
        <KillSwitch hasApiKey={hasApiKey} />

        <div className="mt-4 border-t border-[var(--border-soft)] pt-3">
          <div className="mb-1 text-[0.68rem] uppercase tracking-wide text-[var(--muted)]">
            Last command
          </div>
          {cmdRes.ok && last ? (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
              <span className="font-mono text-xs text-[var(--text)]">
                {String(last.command_type ?? "—")}
              </span>
              <span className="text-xs text-[var(--muted)]">
                status {String(last.status ?? "—")}
              </span>
              <span className="text-xs text-[var(--muted)]">
                by {String(last.requested_by ?? "—")}
              </span>
              <span className="text-[0.68rem] text-[var(--dim)] tnum">
                {ts(strOrNull(last.created_at))}
              </span>
            </div>
          ) : (
            <div className="text-xs text-[var(--dim)]">
              no control_commands rows in the mirror
            </div>
          )}
        </div>
      </SourcedPanel>

      {/* Frozen controls — INERT */}
      <Banner tone="frozen">{FROZEN_BANNER}</Banner>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {FROZEN_CONTROLS.map((c) => (
          <fieldset
            key={c.title}
            disabled
            aria-disabled="true"
            className="cursor-not-allowed rounded-lg border border-[var(--border)] bg-[var(--card)] p-4 opacity-60"
          >
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-[var(--text)]">{c.title}</h3>
              <span className="prov-badge">FROZEN</span>
            </div>
            <p className="mt-1 text-xs text-[var(--muted)]">{c.desc}</p>
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                disabled
                className="cursor-not-allowed rounded-md border border-[var(--border)] px-3 py-1.5 text-xs text-[var(--dim)]"
              >
                Disabled
              </button>
              <span className="self-center text-[0.68rem] text-[var(--dim)]">
                no writes — see banner
              </span>
            </div>
          </fieldset>
        ))}
      </div>

      <Banner tone="info">
        These controls render disabled with no write path — there is no hidden mutation and
        no silent dead-fail. They stay frozen until Unfreeze Gates A–D are cleared.
      </Banner>
    </div>
  );
}
