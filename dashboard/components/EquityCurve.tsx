"use client";

/**
 * Portfolio equity curve + underwater (drawdown) plot — ONE series (E1).
 *
 * Pure series RENDERING: maps published equity points to pixels and derives the
 * running-peak drawdown for the underwater panel. No statistics library, no
 * inferential computation.
 *
 * - Default series = the ACTIVE portfolio session only; prior portfolio sessions
 *   are behind an explicit "include prior sessions" toggle (E1.3). Per-leg /
 *   retired sessions never reach this component (excluded server-side).
 * - Time range selector 1D · 1M · 6M · 1Y · 5Y · All, default 1M, sliced
 *   client-side from the single fetched series (E2). A range with too little
 *   data falls back to the full available span with an "only N days of history"
 *   note instead of an empty chart.
 * - Line (never filled-to-floor); y-axis auto-scales to the data range with
 *   padding — NOT zero-based — so real variation is visible. Underwater stays
 *   0-anchored (correct for drawdown). Gridlines whisper. Crosshair + tooltip
 *   on hover (E3).
 */

import { useMemo, useState } from "react";
import { currency } from "@/lib/format";
import { RangeButtons, sliceByRange, type RangeKey } from "@/components/RangeSelector";

export type EquityPoint = { t: string; equity: number; sessionId?: string };

function pathFrom(points: { x: number; y: number }[]): string {
  return points
    .map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(2)},${p.y.toFixed(2)}`)
    .join(" ");
}

export function EquityCurve({
  points,
  activeSessionId = null,
}: {
  points: EquityPoint[];
  activeSessionId?: string | null;
}) {
  // Item-2: default to the FULL span, not "1M". With a 1M default the curve appeared to reset
  // every month; combined with the active-session-only filter it could only ever reach back to
  // the current performance_session's start (~1 Sept), which read as "history was lost".
  const [range, setRange] = useState<RangeKey>("All");
  // Item-2: prior portfolio sessions ON by default, so a new performance_session does not restart
  // the curve. Still one series, still portfolio-only (per-leg/retired excluded server-side).
  const [includeHistory, setIncludeHistory] = useState(true);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  // ── E1: one series. Default = active portfolio session; toggle adds PRIOR
  //    portfolio sessions (still one concatenated line, still portfolio-only).
  const base = useMemo(() => {
    if (!activeSessionId || includeHistory) return points;
    const active = points.filter((p) => p.sessionId === activeSessionId);
    return active.length >= 2 ? active : points;
  }, [points, activeSessionId, includeHistory]);

  const sessionCount = useMemo(
    () => new Set(base.map((p) => p.sessionId ?? "?")).size,
    [base],
  );

  // ── E2: client-side range slice off the single fetched series (shared slicer).
  const { view, truncatedNote } = useMemo(() => sliceByRange(base, range), [base, range]);

  const W = 640;
  const H = 200;
  const UH = 64;
  const padX = 8;
  const padY = 10;

  const geom = useMemo(() => {
    if (view.length < 2) return null;
    const equities = view.map((p) => p.equity);
    let min = Math.min(...equities);
    let max = Math.max(...equities);
    // E3: auto-scale to the data range (not zero-based) with ~6% breathing room.
    const rawSpan = max - min || Math.abs(max) * 0.001 || 1;
    min -= rawSpan * 0.06;
    max += rawSpan * 0.06;
    const span = max - min;
    // Item-3: x is TIME-linear, not index-linear. Index spacing drew every point an equal width
    // apart, so a weekend or an outage silently compressed to the same gap as one minute — the
    // curve implied a uniform sampling rate it never had.
    const times = view.map((p) => new Date(p.t).getTime());
    const t0 = times[0];
    const tSpan = times[times.length - 1] - t0 || 1;
    const xAt = (i: number) => padX + ((times[i] - t0) / tSpan) * (W - 2 * padX);
    const xAtTime = (ms: number) => padX + ((ms - t0) / tSpan) * (W - 2 * padX);
    const yAt = (v: number) => padY + (1 - (v - min) / span) * (H - 2 * padY);
    // Item-3: interior date ticks. Evenly spaced in TIME (== evenly spaced in pixels now), so the
    // labels describe real elapsed time rather than row counts. Day-granular when the span is
    // longer than ~2 days, clock time within a single session.
    const spanDays = tSpan / 86_400_000;
    const tickFmt = (ms: number) =>
      spanDays > 2
        ? new Date(ms).toISOString().slice(5, 10)
        : new Date(ms).toISOString().slice(11, 16);
    const nTicks = 5;
    const ticks = Array.from({ length: nTicks }, (_, k) => {
      const ms = t0 + (tSpan * (k + 0.5)) / nTicks;
      return { ms, x: xAtTime(ms), label: tickFmt(ms) };
    }).filter((tk, k, all) => all.findIndex((o) => o.label === tk.label) === k);
    const nearestIndexAt = (x: number) => {
      const ms = t0 + ((x - padX) / Math.max(W - 2 * padX, 1)) * tSpan;
      let best = 0;
      let bestD = Infinity;
      for (let i = 0; i < times.length; i += 1) {
        const d = Math.abs(times[i] - ms);
        if (d < bestD) {
          bestD = d;
          best = i;
        }
      }
      return best;
    };
    // whisper gridlines at 4 even levels
    const grid = [0.25, 0.5, 0.75].map((f) => ({ v: min + f * span, y: yAt(min + f * span) }));
    // underwater: drawdown vs running peak within the DISPLAYED range (follows the slice).
    let peak = -Infinity;
    const dd = view.map((p) => {
      peak = Math.max(peak, p.equity);
      return peak > 0 ? p.equity / peak - 1 : 0;
    });
    const worst = Math.min(...dd, 0) || -1;
    const uY = (d: number) => 2 + (d / worst) * (UH - 6);
    return { min, max, xAt, yAt, grid, dd, worst, uY, ticks, nearestIndexAt };
  }, [view]);

  if (view.length < 2 || !geom) {
    return (
      <div className="nofeed flex h-[220px] items-center justify-center text-xs text-[var(--dim)]">
        insufficient portfolio equity points to plot
      </div>
    );
  }

  const { xAt, yAt, grid, dd, worst, uY, ticks, nearestIndexAt } = geom;
  const line = pathFrom(view.map((p, i) => ({ x: xAt(i), y: yAt(p.equity) })));
  const uPts = view.map((p, i) => ({ x: xAt(i), y: uY(dd[i]) }));
  const uArea =
    `M${xAt(0).toFixed(2)},2 ` +
    uPts.map((p) => `L${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" ") +
    ` L${xAt(view.length - 1).toFixed(2)},2 Z`;

  const hover = hoverIdx != null && hoverIdx >= 0 && hoverIdx < view.length ? hoverIdx : null;
  // Item-3/4: resolve the hovered point by TIME (x is no longer proportional to index), and
  // share the handler with the underwater panel so both charts drive the same crosshair.
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    setHoverIdx(nearestIndexAt(frac * W));
  };

  const fmtTick = (v: number) =>
    v >= 1000 ? `$${(v / 1000).toFixed(1)}k` : `$${v.toFixed(0)}`;
  const firstDate = new Date(view[0].t);
  const lastDate = new Date(view[view.length - 1].t);
  const sameDay = firstDate.toISOString().slice(0, 10) === lastDate.toISOString().slice(0, 10);
  const fmtEdge = (d: Date) =>
    sameDay ? d.toISOString().slice(11, 16) + " UTC" : d.toISOString().slice(0, 10);

  return (
    <div className="space-y-2">
      {/* filter row — range presets first, then the explicit history toggle (E2/E1.3) */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <RangeButtons value={range} onChange={setRange} />
        <label className="flex cursor-pointer items-center gap-1.5 text-[0.68rem] text-[var(--muted)]">
          <input
            type="checkbox"
            checked={includeHistory}
            onChange={(e) => setIncludeHistory(e.target.checked)}
            className="accent-[var(--accent)]"
          />
          include prior portfolio sessions
        </label>
        <span className="ml-auto text-[0.68rem] text-[var(--dim)] tnum">
          {sessionCount} session{sessionCount === 1 ? "" : "s"} (portfolio) · {view.length} points
          {truncatedNote ? ` · ${truncatedNote}` : ""}
        </span>
      </div>

      <div className="relative">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          className="w-full"
          role="img"
          aria-label="Portfolio equity curve"
          onMouseMove={onMove}
          onMouseLeave={() => setHoverIdx(null)}
        >
          {/* whisper gridlines + right-edge tick labels */}
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
              <text
                x={W - padX - 2}
                y={g.y - 2.5}
                textAnchor="end"
                fontSize="8"
                fill="var(--dim)"
              >
                {fmtTick(g.v)}
              </text>
            </g>
          ))}
          {/* Item-3: interior date ticks — whisper verticals + labels along the bottom */}
          {ticks.map((tk) => (
            <g key={tk.ms}>
              <line
                x1={tk.x}
                y1={padY}
                x2={tk.x}
                y2={H - padY}
                stroke="var(--border-soft)"
                strokeWidth="0.5"
                strokeDasharray="2 4"
              />
              <text x={tk.x} y={H - 1.5} textAnchor="middle" fontSize="7.5" fill="var(--dim)">
                {tk.label}
              </text>
            </g>
          ))}
          {/* session-start baseline */}
          <line
            x1={padX}
            y1={yAt(view[0].equity)}
            x2={W - padX}
            y2={yAt(view[0].equity)}
            stroke="var(--border)"
            strokeDasharray="3 4"
            strokeWidth="1"
          />
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
              <circle cx={xAt(hover)} cy={yAt(view[hover].equity)} r="3" fill="var(--accent)" stroke="var(--card)" strokeWidth="1.5" />
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
            <div className="text-[var(--text)]">{currency(view[hover].equity)}</div>
            <div className="text-[var(--dim)]">
              {new Date(view[hover].t).toISOString().slice(0, 16).replace("T", " ")} UTC
            </div>
            <div className="text-[var(--dim)]">dd {(dd[hover] * 100).toFixed(2)}%</div>
          </div>
        )}
      </div>

      <div className="text-[0.62rem] uppercase tracking-wide text-[var(--dim)]">
        underwater (drawdown vs running peak{worst < 0 ? ` · worst ${(worst * 100).toFixed(2)}%` : ""})
      </div>
      {/* Item-4: the underwater panel had NO mouse handler — it drew the crosshair from the shared
          hoverIdx but could never set it, so hovering or dragging it did nothing. Same handler as
          the equity chart above (identical viewBox width), so the two are now linked in both
          directions. */}
      <svg
        viewBox={`0 0 ${W} ${UH}`}
        className="w-full"
        role="img"
        aria-label="Underwater plot"
        onMouseMove={onMove}
        onMouseLeave={() => setHoverIdx(null)}
      >
        {/* 0-anchored — correct for drawdown (E3) */}
        <line x1={padX} y1={2} x2={W - padX} y2={2} stroke="var(--border-soft)" strokeWidth="0.75" />
        <path d={uArea} fill="rgba(239,68,68,0.16)" stroke="none" />
        <path d={pathFrom(uPts)} fill="none" stroke="var(--red)" strokeWidth="1" opacity="0.7" />
        {hover != null && (
          <line
            x1={xAt(hover)}
            y1={2}
            x2={xAt(hover)}
            y2={UH - 2}
            stroke="var(--muted)"
            strokeWidth="0.75"
            strokeDasharray="2 3"
          />
        )}
      </svg>
      <div className="flex justify-between text-[0.62rem] text-[var(--dim)] tnum">
        <span>{fmtEdge(firstDate)}</span>
        <span>{fmtEdge(lastDate)}</span>
      </div>
    </div>
  );
}
