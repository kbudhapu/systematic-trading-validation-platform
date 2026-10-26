"use client";

/**
 * Research equity panel (RD5). research_equity_curves.
 *
 * Renders the stored series_json (cumulative equity) + drawdown_json verbatim; labels max_drawdown.
 * RENDER-ONLY: the drawdown series is the STORED one (payload.drawdown), NOT recomputed from the
 * equity series — the two are separate mirror columns and we draw what was registered.
 *
 * Experiments with no registered curve get an honest "no registered curve definition" state
 * (M4 principled skip) — nothing is invented.
 */

import { useState } from "react";
import { DASH } from "@/lib/format";
import type { EquityCurveRow } from "@/lib/evidence";

function pathFrom(points: { x: number; y: number }[]): string {
  return points.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" ");
}

export function ResearchEquityPanel({ curves }: { curves: EquityCurveRow[] }) {
  const [sel, setSel] = useState(0);
  if (curves.length === 0) {
    return (
      <div className="flex min-h-[80px] flex-col items-center justify-center gap-1 rounded border border-dashed border-[var(--border)] py-4 text-center">
        <div className="text-sm font-medium text-[var(--dim)]">no registered curve definition</div>
        <div className="max-w-md text-xs text-[var(--dim)]">
          This experiment has no equity_curve evidence in the vault. Nothing is inferred (M4
          principled skip).
        </div>
      </div>
    );
  }
  const cur = curves[Math.min(sel, curves.length - 1)];
  return (
    <div className="space-y-2">
      {curves.length > 1 && (
        <div className="flex flex-wrap gap-1.5">
          {curves.map((c, i) => (
            <button
              key={c.sourceRowId}
              onClick={() => setSel(i)}
              className={`rounded border px-2 py-0.5 text-[0.62rem] ${
                i === sel
                  ? "border-[var(--accent)] text-[var(--accent)]"
                  : "border-[var(--border-soft)] text-[var(--dim)] hover:text-[var(--text)]"
              }`}
              title={c.trialKey}
            >
              {c.trialKey}
            </button>
          ))}
        </div>
      )}
      <CurvePlot curve={cur} />
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-[0.66rem] sm:grid-cols-4">
        <div>
          <dt className="text-[var(--dim)]">trial</dt>
          <dd className="tnum text-[var(--text)]">{cur.trialKey}</dd>
        </div>
        <div>
          <dt className="text-[var(--dim)]">max drawdown</dt>
          <dd className="tnum text-[var(--text)]">
            {cur.maxDrawdown == null ? DASH : `${(cur.maxDrawdown * 100).toFixed(2)}%`}
          </dd>
        </div>
        <div>
          <dt className="text-[var(--dim)]">n trades</dt>
          <dd className="tnum text-[var(--text)]">{cur.nTrades ?? DASH}</dd>
        </div>
        <div>
          <dt className="text-[var(--dim)]">class</dt>
          <dd className="tnum text-[var(--text)]">{cur.evidenceClass ?? DASH}</dd>
        </div>
      </dl>
      <p className="text-[0.6rem] text-[var(--dim)]">
        series + drawdown rendered verbatim{cur.generatorRef ? ` · ${cur.generatorRef}` : ""} — not
        recomputed.
      </p>
    </div>
  );
}

function CurvePlot({ curve }: { curve: EquityCurveRow }) {
  const eq = curve.series;
  const dd = curve.drawdown;
  const W = 460;
  const H = 150;
  const UH = 46;
  const padX = 8;
  const padY = 10;

  if (eq.length < 2) {
    return (
      <div className="flex h-[120px] items-center justify-center rounded border border-[var(--border-soft)] text-[0.68rem] text-[var(--dim)]">
        series too short to plot ({eq.length} point{eq.length === 1 ? "" : "s"})
      </div>
    );
  }

  let min = Math.min(...eq);
  let max = Math.max(...eq);
  const rawSpan = max - min || Math.abs(max) * 0.001 || 1;
  min -= rawSpan * 0.06;
  max += rawSpan * 0.06;
  const span = max - min;
  const xAt = (i: number) => padX + (i / (eq.length - 1)) * (W - 2 * padX);
  const yAt = (v: number) => padY + (1 - (v - min) / span) * (H - 2 * padY);
  const line = pathFrom(eq.map((v, i) => ({ x: xAt(i), y: yAt(v) })));

  // underwater: the STORED drawdown series (not recomputed). Falls back to none if absent.
  const worst = dd.length ? Math.min(...dd, 0) || -1 : -1;
  const uY = (d: number) => 2 + (worst < 0 ? (d / worst) * (UH - 6) : 0);
  const uPts = dd.map((d, i) => ({ x: xAt(Math.min(i, eq.length - 1)), y: uY(d) }));
  const uArea = dd.length
    ? `M${xAt(0).toFixed(2)},2 ` +
      uPts.map((p) => `L${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" ") +
      ` L${xAt(eq.length - 1).toFixed(2)},2 Z`
    : "";

  return (
    <div className="space-y-1">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Research equity curve">
        <line x1={padX} y1={yAt(eq[0])} x2={W - padX} y2={yAt(eq[0])} stroke="var(--border)" strokeDasharray="3 4" strokeWidth="1" />
        <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.5" strokeLinejoin="round" />
      </svg>
      {dd.length > 0 && (
        <>
          <div className="text-[0.6rem] uppercase tracking-wide text-[var(--dim)]">
            underwater (stored drawdown{worst < 0 ? ` · worst ${(worst * 100).toFixed(2)}%` : ""})
          </div>
          <svg viewBox={`0 0 ${W} ${UH}`} className="w-full" role="img" aria-label="Underwater">
            <line x1={padX} y1={2} x2={W - padX} y2={2} stroke="var(--border-soft)" strokeWidth="0.75" />
            <path d={uArea} fill="rgba(239,68,68,0.16)" stroke="none" />
            <path d={pathFrom(uPts)} fill="none" stroke="var(--red)" strokeWidth="1" opacity="0.7" />
          </svg>
        </>
      )}
    </div>
  );
}
