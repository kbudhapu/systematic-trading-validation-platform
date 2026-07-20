"use client";

/**
 * MCPT evidence panel (RD2/RD3). RENDER-ONLY.
 *
 * present   → a TRUE histogram of the stored null_array, with the observed statistic overlaid and
 *             the 0.025 gate drawn as a config constant (optionally a BAND — knife-edge widening,
 *             default off). null_array is LAZY-LOADED from /api/research/null-array when the panel
 *             mounts, so the page payload stays light.
 * deferred  → no array exists yet: a COUNT overlay (stored_p vs gate; exceedance_k / n_perm margin)
 * /absent     with an explicit "null array pending" marker. Never a fabricated distribution.
 *
 * The authoritative numbers (stored_p, exceedance_k, n_perm, observed, null_max) come straight
 * from the mirror — the histogram bins the stored null for display and positions markers, but no
 * p-value is recomputed and no permutation is re-run. The gate CRITICAL-VALUE line is a display
 * quantile of the stored null, labelled as such.
 */

import { useEffect, useMemo, useState } from "react";
import { DASH } from "@/lib/format";
import {
  MCPT_GATE,
  gateBand,
  type EvidenceRow,
} from "@/lib/evidence";

function fmtP(n: number | null, digits = 5): string {
  return n == null ? DASH : n.toFixed(digits);
}
function fmtStat(n: number | null): string {
  return n == null ? DASH : n.toPrecision(3);
}

/** quantile of an already-sorted ascending array (display arithmetic, like binning). */
function quantileSorted(sorted: number[], q: number): number | null {
  if (sorted.length === 0) return null;
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.round(q * (sorted.length - 1))));
  return sorted[idx];
}

export function McptHistogram({
  row,
  band = false,
}: {
  row: EvidenceRow;
  /** knife-edge-widening band around the gate (VTD §7, default off). */
  band?: boolean;
}) {
  const isPresent = row.arrayState === "present";
  const [nullArray, setNullArray] = useState<number[] | null>(row.nullArray);
  const [loading, setLoading] = useState(false);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  // RD2: lazy-load null_array only when a PRESENT panel opens (and only if not already supplied).
  useEffect(() => {
    if (!isPresent || nullArray != null) return;
    let alive = true;
    setLoading(true);
    fetch(`/api/research/null-array?id=${row.sourceRowId}`)
      .then((r) => r.json())
      .then((j) => {
        if (!alive) return;
        if (Array.isArray(j.nullArray)) setNullArray(j.nullArray);
        else setLoadErr("null array unavailable");
      })
      .catch((e) => alive && setLoadErr(String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [isPresent, nullArray, row.sourceRowId]);

  const replication = (row.evidenceClass ?? "").toLowerCase() === "replication";

  // derived, honest annotations (from mirror values — no recompute of the verdict)
  const aboveAll =
    row.observedStat != null && row.nullMax != null && row.observedStat > row.nullMax;
  const gateK = row.nPerm != null ? MCPT_GATE * row.nPerm : null;
  const permMargin =
    gateK != null && row.exceedanceK != null ? gateK - row.exceedanceK : null;
  const knifeEdge = permMargin != null && Math.abs(permMargin) <= 1.0;
  const mcSeP =
    row.nPerm && row.nPerm > 0
      ? Math.sqrt((MCPT_GATE * (1 - MCPT_GATE)) / row.nPerm)
      : null;
  const mcSePerms = mcSeP != null && row.nPerm != null ? mcSeP * row.nPerm : null;

  const b = gateBand(row.nPerm, band);

  return (
    <div className="space-y-2">
      <ClassLine evidenceClass={row.evidenceClass} storedP={row.storedP} replication={replication} />

      {isPresent ? (
        <PresentHistogram
          nullArray={nullArray}
          loading={loading}
          loadErr={loadErr}
          observed={row.observedStat}
          nPerm={row.nPerm}
          gate={b}
          aboveAll={aboveAll}
        />
      ) : (
        <CountOverlay row={row} gate={b} />
      )}

      {/* authoritative numbers — verbatim from the mirror */}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-[0.68rem] sm:grid-cols-4">
        <Stat k="stored p" v={fmtP(row.storedP)} />
        <Stat k="gate" v={MCPT_GATE.toFixed(3)} />
        <Stat k="exceedance k / n" v={`${row.exceedanceK ?? DASH} / ${row.nPerm ?? DASH}`} />
        <Stat k="observed" v={fmtStat(row.observedStat)} />
        <Stat k="null max" v={fmtStat(row.nullMax)} />
        <Stat k="array" v={row.arrayState ?? DASH} />
        <Stat k="seed" v={row.seed != null ? String(row.seed) : DASH} />
        <Stat k="schema" v={row.schemaVersion ?? DASH} />
      </dl>

      {/* derived annotations — clearly framed, never overstated */}
      {aboveAll && (
        <Annotation tone="strong">
          observed {fmtStat(row.observedStat)} exceeds the entire null (null max{" "}
          {fmtStat(row.nullMax)}) — above all {row.nPerm ?? "N"} permutations.
        </Annotation>
      )}
      {knifeEdge && (
        <Annotation tone="edge">
          knife-edge: stored p {fmtP(row.storedP)} sits ~{Math.abs(permMargin!).toFixed(2)}{" "}
          permutation{Math.abs(permMargin!) === 1 ? "" : "s"} from the {MCPT_GATE} gate
          {mcSePerms != null ? ` (MC-SE ≈ ${mcSePerms.toFixed(1)} perms)` : ""} — the next
          permutation would flip it. Treat the pass as provisional.
        </Annotation>
      )}
      {band && b.halfWidth > 0 && (
        <p className="text-[0.62rem] text-[var(--dim)]">
          gate band on: ±{b.halfWidth.toFixed(4)} = max(1/n, MC-SE) around {MCPT_GATE} (VTD §7
          proposal, quarterly review).
        </p>
      )}

      {/* RD2: the caption cites the verdict's OWN checksummed null, not a recomputation. */}
      <p className="text-[0.6rem] text-[var(--dim)]">
        null checksum {row.artifactHash ?? DASH}
        {row.generatorRef ? ` · ${row.generatorRef}` : ""} — rendered, not recomputed.
      </p>
    </div>
  );
}

function PresentHistogram({
  nullArray,
  loading,
  loadErr,
  observed,
  nPerm,
  gate,
  aboveAll,
}: {
  nullArray: number[] | null;
  loading: boolean;
  loadErr: string | null;
  observed: number | null;
  nPerm: number | null;
  gate: { gate: number; lo: number; hi: number; halfWidth: number };
  aboveAll: boolean;
}) {
  const W = 420;
  const H = 150;
  const padX = 6;
  const padTop = 8;
  const padBot = 16;

  const geom = useMemo(() => {
    if (!nullArray || nullArray.length === 0) return null;
    const sorted = [...nullArray].sort((a, b) => a - b);
    const lo = sorted[0];
    const hiRaw = sorted[sorted.length - 1];
    // include observed in the axis so its marker is always on-canvas
    const hi = observed != null ? Math.max(hiRaw, observed) : hiRaw;
    const span = hi - lo || Math.abs(hi) * 0.01 || 1;
    const nBins = Math.min(48, Math.max(12, Math.round(Math.sqrt(nullArray.length))));
    const bins = new Array(nBins).fill(0);
    for (const v of nullArray) {
      let bi = Math.floor(((v - lo) / span) * nBins);
      if (bi < 0) bi = 0;
      if (bi >= nBins) bi = nBins - 1;
      bins[bi] += 1;
    }
    const maxCount = Math.max(...bins, 1);
    const xAt = (v: number) => padX + ((v - lo) / span) * (W - 2 * padX);
    const yAt = (c: number) => padTop + (1 - c / maxCount) * (H - padTop - padBot);
    // gate critical value = the (1-gate) quantile of the stored null (display quantile)
    const critLine = quantileSorted(sorted, 1 - gate.gate);
    const critLo = gate.halfWidth > 0 ? quantileSorted(sorted, 1 - gate.hi) : null;
    const critHi = gate.halfWidth > 0 ? quantileSorted(sorted, 1 - gate.lo) : null;
    return { lo, hi, span, nBins, bins, maxCount, xAt, yAt, critLine, critLo, critHi, binW: (W - 2 * padX) / nBins };
  }, [nullArray, observed, gate.gate, gate.lo, gate.hi, gate.halfWidth]);

  if (loading) {
    return <PanelStub text="loading null array…" />;
  }
  if (loadErr || !geom) {
    return <PanelStub text={loadErr ?? "null array unavailable"} />;
  }

  const { bins, xAt, yAt, critLine, critLo, critHi } = geom;
  const baseY = H - padBot;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="MCPT null histogram">
      {/* baseline */}
      <line x1={padX} y1={baseY} x2={W - padX} y2={baseY} stroke="var(--border)" strokeWidth="0.75" />
      {/* bars */}
      {bins.map((c, i) => {
        const x = padX + i * geom.binW;
        const y = yAt(c);
        return (
          <rect
            key={i}
            x={x + 0.5}
            y={y}
            width={Math.max(0.5, geom.binW - 1)}
            height={baseY - y}
            fill="var(--card-2)"
            stroke="var(--border-soft)"
            strokeWidth="0.5"
          />
        );
      })}
      {/* gate critical-value band (if on) */}
      {critLo != null && critHi != null && (
        <rect
          x={xAt(critLo)}
          y={padTop}
          width={Math.max(0, xAt(critHi) - xAt(critLo))}
          height={baseY - padTop}
          fill="var(--warn)"
          opacity="0.10"
        />
      )}
      {/* gate critical-value line (derived-for-display) */}
      {critLine != null && (
        <g>
          <line x1={xAt(critLine)} y1={padTop} x2={xAt(critLine)} y2={baseY} stroke="var(--warn)" strokeWidth="1" strokeDasharray="3 3" />
          <text x={xAt(critLine)} y={padTop + 6} fontSize="7" fill="var(--warn)" textAnchor="middle">
            {MCPT_GATE} crit
          </text>
        </g>
      )}
      {/* observed marker */}
      {observed != null && (
        <g>
          <line x1={xAt(observed)} y1={padTop} x2={xAt(observed)} y2={baseY} stroke="var(--accent)" strokeWidth="1.5" />
          <text
            x={Math.min(W - padX, xAt(observed) + 3)}
            y={padTop + 12}
            fontSize="7.5"
            fill="var(--accent)"
            textAnchor={aboveAll ? "end" : "start"}
          >
            observed{aboveAll ? " ▶ above all" : ""}
          </text>
        </g>
      )}
      <text x={padX} y={baseY + 11} fontSize="7" fill="var(--dim)">
        null statistic → ({nPerm ?? "?"} permutations)
      </text>
    </svg>
  );
}

/** deferred / absent: no distribution exists — a count overlay + pending marker, never a fake hist. */
function CountOverlay({
  row,
  gate,
}: {
  row: EvidenceRow;
  gate: { gate: number; lo: number; hi: number; halfWidth: number };
}) {
  const W = 420;
  const H = 60;
  const padX = 10;
  // p-axis 0..max(0.06, storedP, gate*1.5) so the gate + stored p are both visible
  const pMax = Math.max(0.06, (row.storedP ?? 0) * 1.4, gate.gate * 1.5);
  const xAt = (p: number) => padX + (Math.min(p, pMax) / pMax) * (W - 2 * padX);
  const y = 30;
  return (
    <div className="space-y-1">
      <div className="rounded border border-dashed border-[var(--border)] bg-[var(--card-2)] px-2 py-1 text-[0.62rem] text-[var(--dim)]">
        null array pending — deferred generation ({row.arrayState ?? "absent"}). Count overlay only;
        the full histogram renders once the array is emitted (STANDING-DiagnosticEmission).
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="MCPT count overlay">
        <line x1={padX} y1={y} x2={W - padX} y2={y} stroke="var(--border)" strokeWidth="0.75" />
        {/* gate band / line on the p-axis */}
        {gate.halfWidth > 0 && (
          <rect x={xAt(gate.lo)} y={y - 14} width={Math.max(0, xAt(gate.hi) - xAt(gate.lo))} height={28} fill="var(--warn)" opacity="0.12" />
        )}
        <line x1={xAt(gate.gate)} y1={y - 14} x2={xAt(gate.gate)} y2={y + 14} stroke="var(--warn)" strokeWidth="1" strokeDasharray="3 3" />
        <text x={xAt(gate.gate)} y={y - 16} fontSize="7" fill="var(--warn)" textAnchor="middle">gate {gate.gate}</text>
        {/* stored p marker */}
        {row.storedP != null && (
          <g>
            <circle cx={xAt(row.storedP)} cy={y} r="3.5" fill="var(--accent)" />
            <text x={xAt(row.storedP)} y={y + 13} fontSize="7.5" fill="var(--accent)" textAnchor="middle">
              stored p {row.storedP.toFixed(4)}
            </text>
          </g>
        )}
        <text x={W - padX} y={y - 16} fontSize="7" fill="var(--dim)" textAnchor="end">p-value axis</text>
      </svg>
    </div>
  );
}

function ClassLine({
  evidenceClass,
  storedP,
  replication,
}: {
  evidenceClass: string | null;
  storedP: number | null;
  replication: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-[0.62rem]">
      <span className="rounded border border-[var(--border-soft)] px-1.5 py-0.5 uppercase tracking-wide text-[var(--dim)]">
        {evidenceClass ?? "—"}
      </span>
      {/* rule 1: a replication MUST show stored_p overlaid */}
      {replication && (
        <span className="rounded border border-[var(--warn)] px-1.5 py-0.5 text-[var(--warn)]">
          replication · stored p {storedP == null ? DASH : storedP.toFixed(5)} (mandatory overlay)
        </span>
      )}
    </div>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div>
      <dt className="text-[var(--dim)]">{k}</dt>
      <dd className="tnum text-[var(--text)]">{v}</dd>
    </div>
  );
}

function Annotation({ tone, children }: { tone: "strong" | "edge"; children: React.ReactNode }) {
  const color = tone === "strong" ? "var(--accent)" : "var(--warn)";
  return (
    <p className="rounded border px-2 py-1 text-[0.66rem]" style={{ color, borderColor: color }}>
      {children}
    </p>
  );
}

function PanelStub({ text }: { text: string }) {
  return (
    <div className="flex h-[120px] items-center justify-center rounded border border-[var(--border-soft)] text-[0.68rem] text-[var(--dim)]">
      {text}
    </div>
  );
}
