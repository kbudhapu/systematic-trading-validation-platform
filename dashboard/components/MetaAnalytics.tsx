"use client";

/**
 * T2 — the cross-experiment meta-research view. EVERY number here was computed in Python by the
 * publisher (_META-ANALYTICS artifact: medians, pass rates, histogram bins, ladder slopes) — this
 * file only maps stored values to pixels and sorts rendered rows. Each panel states its N.
 */

import { useMemo, useState } from "react";
import type { MetaAnalytics, FamilyScoreRow } from "@/lib/experiment";
import { DASH } from "@/lib/format";

function fmt(n: number | null | undefined, d = 3): string {
  return n == null ? DASH : n.toFixed(d);
}

// ── Family scorecard (sortable — sorting rendered rows is interaction, not statistics) ──
type SortKey = "family" | "nTrials" | "passRate" | "medianSharpe1x" | "medianMcptP";

export function FamilyScorecard({ rows, nTotal }: { rows: FamilyScoreRow[]; nTotal: number }) {
  const [sortKey, setSortKey] = useState<SortKey>("medianSharpe1x");
  const [desc, setDesc] = useState(true);

  const sorted = useMemo(() => {
    const copy = [...rows];
    copy.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      if (av == null && bv == null) return 0;
      if (av == null) return 1; // nulls last, regardless of direction
      if (bv == null) return -1;
      const cmp = typeof av === "string" ? av.localeCompare(String(bv)) : Number(av) - Number(bv);
      return desc ? -cmp : cmp;
    });
    return copy;
  }, [rows, sortKey, desc]);

  const TH = ({ k, label, align = "right" }: { k: SortKey; label: string; align?: string }) => (
    <th
      className={`cursor-pointer py-1.5 pr-3 font-medium text-${align === "left" ? "left" : "right"} hover:text-[var(--text)]`}
      onClick={() => (sortKey === k ? setDesc(!desc) : (setSortKey(k), setDesc(true)))}
    >
      {label}
      {sortKey === k ? (desc ? " ↓" : " ↑") : ""}
    </th>
  );

  return (
    <div className="overflow-x-auto">
      <div className="mb-1 text-[0.68rem] text-[var(--dim)]">N = {nTotal} trials, grouped by family. Medians computed by the publisher.</div>
      <table className="w-full min-w-[560px] text-xs">
        <thead>
          <tr className="text-[0.65rem] uppercase tracking-wide text-[var(--muted)]">
            <TH k="family" label="Family" align="left" />
            <TH k="nTrials" label="Trials" />
            <TH k="passRate" label="Pass rate" />
            <TH k="medianSharpe1x" label="Median Sharpe (1×)" />
            <TH k="medianMcptP" label="Median MCPT p" />
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--border-soft)]">
          {sorted.map((r) => (
            <tr key={r.family}>
              <td className="py-1.5 pr-3 text-[var(--text)]">{r.family}</td>
              <td className="py-1.5 pr-3 text-right tnum">{r.nTrials}</td>
              <td className="py-1.5 pr-3 text-right tnum">
                {r.passRate == null ? DASH : `${(r.passRate * 100).toFixed(0)}%`}{" "}
                <span className="text-[var(--dim)]">({r.nPass}/{r.nTrials})</span>
              </td>
              <td className="py-1.5 pr-3 text-right tnum">
                {fmt(r.medianSharpe1x)} <span className="text-[var(--dim)]">(n={r.sharpeN})</span>
              </td>
              <td className="py-1.5 pr-3 text-right tnum">
                {fmt(r.medianMcptP, 4)} <span className="text-[var(--dim)]">(n={r.mcptN})</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── p-value calibration histogram ──
export function PHistogram({ h }: { h: MetaAnalytics["pHistogram"] }) {
  if (!h.bins.length || h.n === 0) {
    return <div className="text-xs italic text-[var(--dim)]">no MCPT p-values published</div>;
  }
  const W = 640;
  const H = 150;
  const padX = 10;
  const padY = 12;
  const maxBin = Math.max(...h.bins, 1);
  const bw = (W - 2 * padX) / h.bins.length;
  const y = (v: number) => padY + (1 - v / maxBin) * (H - 2 * padY);
  return (
    <div>
      <div className="mb-1 text-[0.68rem] text-[var(--dim)]">N = {h.n} trials with an MCPT p-value.</div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="MCPT p-value histogram">
        {/* uniform reference line (publisher-computed) */}
        {h.uniformRef != null && (
          <line x1={padX} y1={y(h.uniformRef)} x2={W - padX} y2={y(h.uniformRef)}
            stroke="var(--muted)" strokeWidth="1" strokeDasharray="4 4" />
        )}
        {h.bins.map((b, i) => (
          <rect key={i} x={padX + i * bw + 1} y={y(b)} width={Math.max(1, bw - 2)} height={H - padY - y(b)}
            fill="var(--accent-soft)" stroke="var(--accent)" strokeWidth="0.75" rx="1.5" />
        ))}
        {/* kill-gate marker at 0.05 */}
        {h.killGate != null && (
          <line x1={padX + (h.killGate / (h.binWidth * h.bins.length)) * (W - 2 * padX)} y1={4}
            x2={padX + (h.killGate / (h.binWidth * h.bins.length)) * (W - 2 * padX)} y2={H - padY}
            stroke="var(--warn)" strokeWidth="1" strokeDasharray="3 3" />
        )}
        <text x={padX} y={H - 2} fontSize="8" fill="var(--dim)">0</text>
        <text x={W - padX} y={H - 2} textAnchor="end" fontSize="8" fill="var(--dim)">1</text>
        {h.uniformRef != null && (
          <text x={W - padX} y={y(h.uniformRef) - 3} textAnchor="end" fontSize="7.5" fill="var(--dim)">
            uniform ≈ {h.uniformRef.toFixed(2)}/bin
          </text>
        )}
      </svg>
      <p className="mt-1 text-[0.65rem] text-[var(--dim)]">
        If the research process were only ever testing true nulls honestly, p-values would sit near
        the dashed uniform line. Mass clustered just UNDER the 0.05 kill-gate (amber dashed) is a
        self-audit flag — it can indicate selection pressure toward the gate, not real edges.
        Mass far left can also simply reflect genuinely strong effects; read jointly with the
        family scorecard.
      </p>
    </div>
  );
}

// ── Sharpe vs cost-decay scatter (+ honest 1×-only strip) ──
export function CostDecayScatter({ s }: { s: MetaAnalytics["scatter"] }) {
  const pts = s.withLadder.filter((p) => p.sharpe1x != null && p.ladderSlope != null);
  const strip = s.oneXOnly.filter((p) => p.sharpe1x != null);
  const W = 640;
  const H = 190;
  const pad = 26;
  const xs = pts.map((p) => p.sharpe1x as number).concat(strip.map((p) => p.sharpe1x as number));
  const ys = pts.map((p) => p.ladderSlope as number);
  const xLo = Math.min(...xs, 0) - 0.1;
  const xHi = Math.max(...xs, 0) + 0.1;
  const yLo = ys.length ? Math.min(...ys, 0) - 0.05 : -0.1;
  const yHi = ys.length ? Math.max(...ys, 0) + 0.05 : 0.1;
  const x = (v: number) => pad + ((v - xLo) / (xHi - xLo || 1)) * (W - 2 * pad);
  const y = (v: number) => pad + (1 - (v - yLo) / (yHi - yLo || 1)) * (H - 2 * pad - 26);
  return (
    <div>
      <div className="mb-1 text-[0.68rem] text-[var(--dim)]">
        N = {pts.length} trials with a full Sharpe ladder (slope computed by the publisher);
        {" "}{strip.length} trials are 1×-only and shown in the strip below — not dropped.
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Sharpe vs cost decay">
        <line x1={pad} y1={y(0)} x2={W - pad} y2={y(0)} stroke="var(--border-soft)" strokeWidth="1" />
        <line x1={x(0)} y1={pad} x2={x(0)} y2={H - pad - 26} stroke="var(--border-soft)" strokeWidth="1" />
        {pts.map((p, i) => (
          <circle key={i} cx={x(p.sharpe1x as number)} cy={y(p.ladderSlope as number)} r="4"
            fill="var(--accent)" opacity="0.85">
            <title>{`${p.experimentId} ${p.trialKey}: 1x ${fmt(p.sharpe1x)} slope ${fmt(p.ladderSlope, 4)}/×`}</title>
          </circle>
        ))}
        <text x={W - pad} y={y(0) - 4} textAnchor="end" fontSize="8" fill="var(--dim)">slope 0 (no cost decay)</text>
        <text x={W - pad} y={H - pad - 30} textAnchor="end" fontSize="8" fill="var(--dim)">ann. net Sharpe (1×) →</text>
        {/* 1×-only strip */}
        <line x1={pad} y1={H - 18} x2={W - pad} y2={H - 18} stroke="var(--border-soft)" strokeWidth="1" />
        <text x={pad} y={H - 24} fontSize="7.5" fill="var(--dim)">1×-only trials (cost-stress not run):</text>
        {strip.map((p, i) => (
          <circle key={i} cx={x(p.sharpe1x as number)} cy={H - 18} r="3" fill="var(--muted)" opacity="0.7">
            <title>{`${p.experimentId} ${p.trialKey}: 1x ${fmt(p.sharpe1x)}`}</title>
          </circle>
        ))}
      </svg>
      <p className="mt-1 text-[0.65rem] text-[var(--dim)]">
        Slope = (Sharpe 4× − Sharpe 1×) / 3 per cost multiple, computed by the publisher. More
        negative = the edge dies faster under slippage stress.
      </p>
    </div>
  );
}

// ── Effect-size funnel: Sharpe vs n_events ──
export function EffectSizeFunnel({ f }: { f: MetaAnalytics["funnel"] }) {
  const pts = f.points.filter((p) => p.sharpe1x != null && p.nEvents != null);
  if (!pts.length) return <div className="text-xs italic text-[var(--dim)]">no funnel points published</div>;
  const W = 640;
  const H = 190;
  const pad = 26;
  const xs = pts.map((p) => p.nEvents as number);
  const ys = pts.map((p) => p.sharpe1x as number);
  const xHi = Math.max(...xs) * 1.05;
  const yLo = Math.min(...ys, 0) - 0.1;
  const yHi = Math.max(...ys, 0) + 0.1;
  const x = (v: number) => pad + (v / (xHi || 1)) * (W - 2 * pad);
  const y = (v: number) => pad + (1 - (v - yLo) / (yHi - yLo || 1)) * (H - 2 * pad);
  return (
    <div>
      <div className="mb-1 text-[0.68rem] text-[var(--dim)]">
        N = {pts.length} trials. Axis is n_events (present for all trials); n_sessions is recorded
        for only {f.nSessionsCoverage} trial{f.nSessionsCoverage === 1 ? "" : "s"} and is NOT used.
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Effect size funnel">
        <line x1={pad} y1={y(0)} x2={W - pad} y2={y(0)} stroke="var(--border-soft)" strokeWidth="1" />
        {pts.map((p, i) => {
          const pass = (p.triageVerdict ?? "").toUpperCase().startsWith("PASS");
          return (
            <circle key={i} cx={x(p.nEvents as number)} cy={y(p.sharpe1x as number)} r={pass ? 4.5 : 3}
              fill={pass ? "var(--accent)" : "var(--muted)"} opacity={pass ? 0.95 : 0.55}>
              <title>{`${p.experimentId} ${p.trialKey}: sharpe ${fmt(p.sharpe1x)} n=${p.nEvents}${pass ? " (triage PASS)" : ""}`}</title>
            </circle>
          );
        })}
        <text x={W - pad} y={H - 8} textAnchor="end" fontSize="8" fill="var(--dim)">n_events →</text>
        <text x={pad} y={pad - 6} fontSize="8" fill="var(--dim)">ann. net Sharpe (1×)</text>
      </svg>
      <p className="mt-1 text-[0.65rem] text-[var(--dim)]">
        Accent points passed triage. Wins concentrated at LOW n (left side) are the fragile
        pattern — small samples produce large accidental effects.
      </p>
    </div>
  );
}
