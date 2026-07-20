import { Fragment } from "react";
import {
  type Artifact,
  type Num,
  type Trial,
  type TrialStats,
  type PsdBudget,
} from "@/lib/experiment";
import { DASH } from "@/lib/format";
import { StatTile } from "@/components/atoms";

function fmt(n: Num, digits = 3): string {
  return n == null ? DASH : n.toFixed(digits);
}

/**
 * Verdict chip. Carries REGISTRY provenance. Colour convention: green/red are RESERVED for
 * danger/safe + P&L sign — a research verdict is neither, so we tint with accent (PASS) / warn
 * (PASS-FRAGILE) / dim (REJECTED, SHELVED, unverdicted), never green/red.
 */
export function VerdictChip({
  verdict,
  detail,
}: {
  verdict: string | undefined;
  detail?: string | null;
}) {
  const v = (verdict ?? "").toUpperCase();
  const label = v || "UNVERDICTED";
  const tone =
    v === "PASS"
      ? { color: "var(--accent)", border: "var(--accent)", bg: "var(--accent-soft)" }
      : v === "PASS-FRAGILE"
        ? { color: "var(--warn)", border: "var(--warn)", bg: "transparent" }
        : { color: "var(--dim)", border: "var(--border)", bg: "var(--card-2)" };
  return (
    <span
      title={detail ? `registry verdict: ${detail}` : undefined}
      className="inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-xs font-semibold"
      style={{ color: tone.color, border: `1px solid ${tone.border}`, background: tone.bg }}
    >
      <span className="prov-badge" style={{ padding: "0 0.3rem" }}>
        REGISTRY
      </span>
      {label}
    </span>
  );
}

/** Small triage-verdict tag for a single trial (neutral). */
function TriageTag({ v }: { v?: string }) {
  if (!v) return null;
  const fragile = /fragile/i.test(v);
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[0.6rem] uppercase tracking-wide"
      style={{
        color: fragile ? "var(--warn)" : "var(--dim)",
        border: `1px solid ${fragile ? "var(--warn)" : "var(--border-soft)"}`,
      }}
    >
      {v}
    </span>
  );
}

/**
 * Cost-stress ladder sparkline (1x → 2x → 4x). Rendering only — positions come from published
 * values; the axis min/max just scale the line. Neutral stroke (not a P&L signal).
 */
function LadderPlot({ pts }: { pts: Num[] }) {
  const vals = pts.filter((x): x is number => x != null);
  if (vals.length < 2) return null;
  const lo = Math.min(...vals, 0);
  const hi = Math.max(...vals, 0);
  const span = hi - lo || 1;
  const W = 160;
  const H = 40;
  const pad = 6;
  const xs = pts.map((_, i) => pad + (i / (pts.length - 1)) * (W - 2 * pad));
  const yAt = (v: number) => pad + (1 - (v - lo) / span) * (H - 2 * pad);
  const zeroY = yAt(0);
  const d = pts
    .map((p, i) => (p == null ? "" : `${i === 0 ? "M" : "L"}${xs[i].toFixed(1)},${yAt(p).toFixed(1)}`))
    .join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full max-w-[160px]" role="img">
      {lo < 0 && hi > 0 && (
        <line x1={pad} y1={zeroY} x2={W - pad} y2={zeroY} stroke="var(--border)" strokeWidth="0.75" strokeDasharray="2 2" />
      )}
      <path d={d} fill="none" stroke="var(--muted)" strokeWidth="1.5" />
      {pts.map((p, i) =>
        p == null ? null : <circle key={i} cx={xs[i]} cy={yAt(p)} r="2.5" fill="var(--muted)" />,
      )}
    </svg>
  );
}

/** T1 — HONEST coverage-gap state: a missing field says so, never blank, never zero. */
function Gap({ label }: { label: string }) {
  return (
    <div className="rounded border border-dashed border-[var(--border-soft)] px-2 py-1.5 text-[0.65rem] italic text-[var(--dim)]">
      {label}
    </div>
  );
}

/** T1 — Sharpe-with-CI gauge: point + published CI band, zero marked. Rendering only. */
function SharpeCIGauge({ s }: { s: TrialStats }) {
  if (s.sharpe1x == null || s.ci == null || s.ci[0] == null || s.ci[1] == null) {
    return <Gap label="CI not computed for this experiment" />;
  }
  const lo = Math.min(s.ci[0], 0) - 0.1;
  const hi = Math.max(s.ci[1], 0) + 0.1;
  const span = hi - lo || 1;
  const W = 240;
  const H = 30;
  const x = (v: number) => 8 + ((v - lo) / span) * (W - 16);
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full max-w-[240px]" role="img" aria-label="Sharpe with CI">
        <line x1={8} y1={H / 2} x2={W - 8} y2={H / 2} stroke="var(--border-soft)" strokeWidth="1" />
        <line x1={x(0)} y1={4} x2={x(0)} y2={H - 4} stroke="var(--muted)" strokeWidth="1" strokeDasharray="2 3" />
        <rect x={x(s.ci[0])} y={H / 2 - 4} width={Math.max(1, x(s.ci[1]) - x(s.ci[0]))} height={8}
          fill="var(--accent-soft)" stroke="var(--accent)" strokeWidth="0.75" />
        <circle cx={x(s.sharpe1x)} cy={H / 2} r="3" fill="var(--accent)" />
      </svg>
      <div className="text-[0.62rem] text-[var(--dim)] tnum">
        {fmt(s.sharpe1x)} · CI95{s.ciKind ? ` (${s.ciKind})` : ""} [{fmt(s.ci[0])}, {fmt(s.ci[1])}] · zero marked
      </div>
    </div>
  );
}

/** T1 — MCPT kill-gate gauge on a 0-1 scale, 0.05 threshold drawn. NOT a verdict. */
function McptGauge({ s }: { s: TrialStats }) {
  if (s.mcptP == null) {
    return <Gap label="MCPT not computed for this experiment" />;
  }
  const W = 240;
  const H = 26;
  const x = (v: number) => 8 + Math.min(Math.max(v, 0), 1) * (W - 16);
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full max-w-[240px]" role="img" aria-label="MCPT p-value">
        <line x1={8} y1={H / 2} x2={W - 8} y2={H / 2} stroke="var(--border-soft)" strokeWidth="1" />
        <line x1={x(0.05)} y1={3} x2={x(0.05)} y2={H - 3} stroke="var(--warn)" strokeWidth="1" strokeDasharray="3 3" />
        <circle cx={x(s.mcptP)} cy={H / 2} r="3" fill="var(--accent)" />
        <text x={x(0.05)} y={H - 1} textAnchor="middle" fontSize="6.5" fill="var(--dim)">0.05</text>
      </svg>
      <div className="text-[0.62rem] text-[var(--dim)] tnum">
        p = {fmt(s.mcptP, 4)}{s.mcptPKey ? ` (${s.mcptPKey})` : ""}
      </div>
      <div className="text-[0.6rem] text-[var(--warn)]">
        MCPT kill-gate (weak evidence — clears exploration only, NOT leg evidence)
      </div>
    </div>
  );
}

/** One trial's tearsheet — cost-stress ladder, Sharpe+CI gauge, MCPT gauge, context. Published
 * values only; every absent field renders its honest gap state. */
function StatsPanel({ s, nEvents, instrument }: { s: TrialStats; nEvents?: Num; instrument?: string }) {
  const hasSharpeLadder = s.sharpe2x != null || s.sharpe4x != null;
  const hasMeanStress = s.stress2x != null || s.stress4x != null;
  const hasAnyLadder = hasSharpeLadder || hasMeanStress;
  const ladderKind = hasSharpeLadder ? "Sharpe" : "mean net";

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-3">
        {/* cost-stress ladder (the slippage cliff) */}
        <div>
          <div className="mb-1 text-[0.62rem] uppercase tracking-wide text-[var(--muted)]">
            cost-stress ladder {hasAnyLadder ? `(${ladderKind})` : ""}
          </div>
          {s.sharpe1x == null ? (
            <Gap label="not computed for this experiment" />
          ) : hasAnyLadder ? (
            <div>
              <div className="flex flex-wrap gap-2 text-xs tnum">
                <span className="rounded border border-[var(--border-soft)] bg-[var(--card-2)] px-2 py-1">
                  <span className="text-[var(--dim)]">1×</span> {fmt(s.sharpe1x)}
                </span>
                <span className="rounded border border-[var(--border-soft)] bg-[var(--card-2)] px-2 py-1">
                  <span className="text-[var(--dim)]">2×</span> {fmt(s.sharpe2x ?? s.stress2x)}
                </span>
                <span className="rounded border border-[var(--border-soft)] bg-[var(--card-2)] px-2 py-1">
                  <span className="text-[var(--dim)]">4×</span> {fmt(s.sharpe4x ?? s.stress4x)}
                </span>
              </div>
              <LadderPlot pts={[s.sharpe1x, s.sharpe2x ?? s.stress2x, s.sharpe4x ?? s.stress4x]} />
            </div>
          ) : (
            <div className="space-y-1">
              <span className="inline-block rounded border border-[var(--border-soft)] bg-[var(--card-2)] px-2 py-1 text-xs tnum">
                <span className="text-[var(--dim)]">1×</span> {fmt(s.sharpe1x)}
              </span>
              <Gap label="cost-stress not run (1× only)" />
            </div>
          )}
        </div>

        {/* Sharpe-with-CI gauge */}
        <div>
          <div className="mb-1 text-[0.62rem] uppercase tracking-wide text-[var(--muted)]">
            point estimate + CI95
          </div>
          <SharpeCIGauge s={s} />
        </div>

        {/* MCPT kill-gate gauge */}
        <div>
          <div className="mb-1 text-[0.62rem] uppercase tracking-wide text-[var(--muted)]">
            MCPT kill-gate
          </div>
          <McptGauge s={s} />
        </div>
      </div>

      {/* context line */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-[var(--border-soft)] pt-2 text-[0.68rem] text-[var(--dim)] tnum">
        {instrument && <span>{instrument}</span>}
        <span>{nEvents != null ? `n_events = ${nEvents}` : "n_events not recorded"}</span>
        <span>
          {s.nSessions != null ? `n_sessions = ${s.nSessions}` : "n_sessions not recorded for this experiment"}
        </span>
        {s.dsr != null ? <span>DSR = {fmt(s.dsr)}</span> : <span>DSR not computed for this experiment</span>}
      </div>
    </div>
  );
}

/** Full trials list with per-trial statistical panels. */
export function TrialsPanel({ trials }: { trials: Trial[] }) {
  if (trials.length === 0) {
    return (
      <div className="py-4 text-center text-xs text-[var(--dim)]">
        no wave-1 trials recorded for this experiment
      </div>
    );
  }
  return (
    <ul className="space-y-3">
      {trials.map((t, i) => (
        <li
          key={t.trialKey ?? i}
          className="rounded-md border border-[var(--border-soft)] bg-[var(--card)] p-3"
        >
          <div className="mb-2 flex flex-wrap items-center gap-2">
            {t.family && (
              <span className="rounded bg-[var(--card-2)] px-1.5 py-0.5 text-[0.6rem] font-medium uppercase tracking-wide text-[var(--muted)]">
                {t.family}
              </span>
            )}
            <span className="text-sm font-medium text-[var(--text)]">
              {t.hypothesis ?? t.trialKey ?? `trial ${i + 1}`}
            </span>
            <TriageTag v={t.triageVerdict} />
            <span className="ml-auto text-[0.62rem] text-[var(--dim)] tnum">
              {t.pValue != null ? `p=${fmt(t.pValue, 4)}` : ""}
            </span>
          </div>
          <StatsPanel s={t.stats} nEvents={t.nEvents} instrument={t.instrument} />
        </li>
      ))}
    </ul>
  );
}

/** PSD trial-budget / alpha-spend view (counts only — no metric plateau in the vault). */
export function PsdBudgetPanel({ psd }: { psd: PsdBudget | null }) {
  if (!psd) {
    return <div className="text-xs text-[var(--dim)]">no trial-budget entries recorded</div>;
  }
  const v = (n: Num) => (n == null ? DASH : String(n));
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
      <StatTile label="Grid points" value={v(psd.gridPoints)} />
      <StatTile label="Timeframes" value={v(psd.timeframes)} />
      <StatTile label="Objective variants" value={v(psd.objectiveVariants)} />
      <StatTile label="Trials (this queue)" value={v(psd.nTrials)} />
      <StatTile label="Ledger entries" value={v(psd.entries)} sub="alpha-spend rows" />
    </div>
  );
}

/**
 * Zero-dependency, injection-safe markdown-lite renderer for criteria_md. Emits ONLY React text
 * nodes (no dangerouslySetInnerHTML) — headings, bullet/numbered lists, and paragraphs. Inline
 * **bold** and `code` are surfaced as light emphasis; everything else renders as plain text.
 */
function inline(text: string, keyBase: string) {
  // split on **bold** and `code`, keep delimiters
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return parts.map((p, i) => {
    if (p.startsWith("**") && p.endsWith("**")) {
      return (
        <strong key={`${keyBase}-${i}`} className="font-semibold text-[var(--text)]">
          {p.slice(2, -2)}
        </strong>
      );
    }
    if (p.startsWith("`") && p.endsWith("`")) {
      return (
        <code
          key={`${keyBase}-${i}`}
          className="rounded bg-[var(--card-2)] px-1 py-0.5 text-[0.85em] tnum text-[var(--muted)]"
        >
          {p.slice(1, -1)}
        </code>
      );
    }
    return <Fragment key={`${keyBase}-${i}`}>{p}</Fragment>;
  });
}

export function CriteriaMarkdown({ md }: { md: string }) {
  const lines = (md ?? "").replace(/\r\n/g, "\n").split("\n");
  const blocks: React.ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;

  const flush = () => {
    if (!list) return;
    const L = list;
    blocks.push(
      L.ordered ? (
        <ol key={`b${blocks.length}`} className="ml-5 list-decimal space-y-1 text-sm text-[var(--muted)]">
          {L.items.map((it, i) => (
            <li key={i}>{inline(it, `ol${blocks.length}-${i}`)}</li>
          ))}
        </ol>
      ) : (
        <ul key={`b${blocks.length}`} className="ml-5 list-disc space-y-1 text-sm text-[var(--muted)]">
          {L.items.map((it, i) => (
            <li key={i}>{inline(it, `ul${blocks.length}-${i}`)}</li>
          ))}
        </ul>
      ),
    );
    list = null;
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (h) {
      flush();
      const level = h[1].length;
      const size = level <= 1 ? "text-base" : level === 2 ? "text-sm" : "text-xs";
      blocks.push(
        <div key={`b${blocks.length}`} className={`mt-3 font-semibold ${size} text-[var(--text)]`}>
          {inline(h[2], `h${blocks.length}`)}
        </div>,
      );
    } else if (bullet) {
      if (!list || list.ordered) flush(), (list = { ordered: false, items: [] });
      list.items.push(bullet[1]);
    } else if (ordered) {
      if (!list || !list.ordered) flush(), (list = { ordered: true, items: [] });
      list.items.push(ordered[1]);
    } else if (line.trim() === "") {
      flush();
    } else {
      flush();
      blocks.push(
        <p key={`b${blocks.length}`} className="text-sm leading-relaxed text-[var(--muted)]">
          {inline(line, `p${blocks.length}`)}
        </p>,
      );
    }
  }
  flush();
  return <div className="space-y-1.5">{blocks}</div>;
}

/** Compact summary tiles for the detail header. */
export function ExperimentSummary({ art }: { art: Artifact }) {
  const passTrials = art.trials.filter((t) => /^pass/i.test(t.triageVerdict ?? "")).length;
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      <StatTile label="Kind" value={art.kind ?? DASH} />
      <StatTile label="Status" value={art.status ?? DASH} />
      <StatTile label="Trials" value={String(art.trials.length)} sub={`${passTrials} passed triage`} />
      <StatTile
        label="Trial budget"
        value={art.psd?.nTrials != null ? String(art.psd.nTrials) : DASH}
        sub="alpha-spend"
      />
    </div>
  );
}
