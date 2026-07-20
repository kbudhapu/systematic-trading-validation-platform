"use client";

/**
 * Per-leg unitized return curve (L3) — renders leg_return_series rows as-is.
 *
 * indexed NAV (starts 100): P&L moves NAV; capital reallocations issue/redeem units at the
 * current NAV, so the curve is reallocation-neutral BY CONSTRUCTION (the producer asserts it —
 * tests/test_leg_return.py). Pure rendering here: no statistic is computed beyond pixel mapping.
 * Same range selector as the portfolio curve (shared component). Y-axis auto-scales to the data
 * (not zero-based); gridlines whisper; crosshair + tooltip on hover.
 */

import { useMemo, useState } from "react";
import { RangeButtons, sliceByRange, type RangeKey } from "@/components/RangeSelector";

export type LegReturnPoint = {
  t: string;
  nav: number; // indexed_nav
  pnl: number; // cumulative dollar_pnl (tooltip context)
};

function pathFrom(points: { x: number; y: number }[]): string {
  return points
    .map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(2)},${p.y.toFixed(2)}`)
    .join(" ");
}

export function LegReturnCurve({ points }: { points: LegReturnPoint[] }) {
  const [range, setRange] = useState<RangeKey>("1M");
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const { view, truncatedNote } = useMemo(() => sliceByRange(points, range), [points, range]);

  const W = 640;
  const H = 180;
  const padX = 8;
  const padY = 10;

  const geom = useMemo(() => {
    if (view.length < 2) return null;
    const navs = view.map((p) => p.nav);
    let min = Math.min(...navs);
    let max = Math.max(...navs);
    const rawSpan = max - min || Math.abs(max) * 0.001 || 1;
    min -= rawSpan * 0.06; // auto-scaled to data, NOT zero-based (L3.2)
    max += rawSpan * 0.06;
    const span = max - min;
    const xAt = (i: number) => padX + (i / (view.length - 1)) * (W - 2 * padX);
    const yAt = (v: number) => padY + (1 - (v - min) / span) * (H - 2 * padY);
    const grid = [0.25, 0.5, 0.75].map((f) => ({ v: min + f * span, y: yAt(min + f * span) }));
    return { xAt, yAt, grid, min, max };
  }, [view]);

  if (view.length < 2 || !geom) {
    return (
      <div className="nofeed flex h-[200px] items-center justify-center text-xs text-[var(--dim)]">
        insufficient leg return points to plot (series begins when the producer deploys)
      </div>
    );
  }

  const { xAt, yAt, grid } = geom;
  const line = pathFrom(view.map((p, i) => ({ x: xAt(i), y: yAt(p.nav) })));
  const hover = hoverIdx != null && hoverIdx >= 0 && hoverIdx < view.length ? hoverIdx : null;
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    setHoverIdx(Math.round(frac * (view.length - 1)));
  };

  const firstDate = new Date(view[0].t);
  const lastDate = new Date(view[view.length - 1].t);
  const sameDay = firstDate.toISOString().slice(0, 10) === lastDate.toISOString().slice(0, 10);
  const fmtEdge = (d: Date) =>
    sameDay ? d.toISOString().slice(11, 16) + " UTC" : d.toISOString().slice(0, 10);
  const retPct = (nav: number) => ((nav / 100 - 1) * 100).toFixed(2);

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <RangeButtons value={range} onChange={setRange} />
        <span className="ml-auto text-[0.68rem] text-[var(--dim)] tnum">
          {view.length} points{truncatedNote ? ` · ${truncatedNote}` : ""}
        </span>
      </div>

      <div className="relative">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          className="w-full"
          role="img"
          aria-label="Leg indexed NAV curve"
          onMouseMove={onMove}
          onMouseLeave={() => setHoverIdx(null)}
        >
          {grid.map((g, i) => (
            <g key={i}>
              <line
                x1={padX}
                y1={g.y}
                x2={W - padX}
                y2={g.y}
                stroke="var(--border-soft)"
                strokeWidth="0.75"
              />
              <text x={W - padX - 2} y={g.y - 2.5} textAnchor="end" fontSize="8" fill="var(--dim)">
                {g.v.toFixed(2)}
              </text>
            </g>
          ))}
          {/* NAV=100 baseline (inception) when in view */}
          {geom.min <= 100 && geom.max >= 100 && (
            <line
              x1={padX}
              y1={yAt(100)}
              x2={W - padX}
              y2={yAt(100)}
              stroke="var(--border)"
              strokeDasharray="3 4"
              strokeWidth="1"
            />
          )}
          <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.5" strokeLinejoin="round" />
          {hover != null && (
            <g>
              <line
                x1={xAt(hover)}
                y1={padY}
                x2={xAt(hover)}
                y2={H - padY}
                stroke="var(--muted)"
                strokeWidth="0.75"
                strokeDasharray="2 3"
              />
              <circle
                cx={xAt(hover)}
                cy={yAt(view[hover].nav)}
                r="3"
                fill="var(--accent)"
                stroke="var(--card)"
                strokeWidth="1.5"
              />
            </g>
          )}
        </svg>
        {hover != null && (
          <div
            className="pointer-events-none absolute top-1 rounded border border-[var(--border)] bg-[var(--card)] px-2 py-1 text-[0.65rem] shadow-sm tnum"
            style={{
              left: `${(xAt(hover) / W) * 100}%`,
              transform: xAt(hover) > W / 2 ? "translateX(calc(-100% - 6px))" : "translateX(6px)",
            }}
          >
            <div className="text-[var(--text)]">
              NAV {view[hover].nav.toFixed(3)} ({retPct(view[hover].nav)}%)
            </div>
            <div className="text-[var(--dim)]">
              {new Date(view[hover].t).toISOString().slice(0, 16).replace("T", " ")} UTC
            </div>
            <div className="text-[var(--dim)]">cum P&L ${view[hover].pnl.toFixed(2)}</div>
          </div>
        )}
      </div>

      <div className="flex justify-between text-[0.62rem] text-[var(--dim)] tnum">
        <span>{fmtEdge(firstDate)}</span>
        <span>{fmtEdge(lastDate)}</span>
      </div>

      {/* L3.3 — the method, on the chart itself */}
      <p className="text-[0.65rem] text-[var(--dim)]">
        Time-weighted return, neutral to capital reallocation — the portfolio brain moving risk
        budget between legs issues/redeems units at the current NAV and cannot move this curve;
        only the leg&rsquo;s own P&amp;L does.
      </p>
    </div>
  );
}
