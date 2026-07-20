import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { safeRows, capSafeSeries, numOrNull, strOrNull, type Row } from "@/lib/dashboard-data";
import { SourcedPanel, NoFeedBody } from "@/components/SourcedPanel";
import { PageHeader, Banner, StatTile } from "@/components/atoms";
import { LegReturnCurve, type LegReturnPoint } from "@/components/LegReturnCurve";
import { signedCurrency, ts, qty } from "@/lib/format";

export const dynamic = "force-dynamic";

const LEG_CADENCE = 5 * 60_000;

export default async function LegDetailPage({
  params,
}: {
  params: Promise<{ leg: string }>;
}) {
  const { leg } = await params;
  const legName = decodeURIComponent(leg);
  const supabase = await createClient();

  // Unitized return series (L2 producer). Rendered as-is — nothing computed here.
  // capSafeSeries fetches DESC + reverses to chronological: PostgREST caps at db-max-rows (1000)
  // REGARDLESS of `.limit()`, so the prior ascending fetch would have returned the OLDEST 1000 rows
  // once this leg exceeded 1000 points (~466 now, ~1/min) — freezing the NAV curve + freshness
  // badge on stale history (the #282 portfolio bug). DESC keeps the NEWEST rows; `latest` is true.
  const seriesRes = await capSafeSeries<Row>(
    supabase
      .from("leg_return_series")
      .select("recorded_at, indexed_nav, cumulative_return_pct, dollar_pnl, capital_base, provenance")
      .eq("leg_name", legName),
    "recorded_at",
  );
  const points: LegReturnPoint[] = seriesRes.chronological
    .map((r) => ({
      t: String(r.recorded_at),
      nav: Number(r.indexed_nav),
      pnl: Number(r.dollar_pnl ?? 0),
    }))
    .filter((p) => Number.isFinite(p.nav) && p.t);
  const lastRow = seriesRes.latest;
  const lastSync = strOrNull(lastRow?.recorded_at);

  // Latest attribution snapshot for the header tiles.
  const attribRes = await safeRows<Row>(
    supabase
      .from("leg_attribution_snapshots")
      .select("symbol, realized_pnl, unrealized_pnl, position_qty, provenance, recorded_at, source_ts")
      .eq("leg_name", legName)
      .order("recorded_at", { ascending: false })
      .limit(1),
  );
  const attrib = attribRes.rows[0] ?? null;

  const back = (
    <Link href="/legs" className="text-xs text-[var(--muted)] hover:text-[var(--accent)]">
      ← all legs
    </Link>
  );

  return (
    <div className="space-y-4">
      {back}
      <PageHeader
        title={legName}
        subtitle={attrib ? `symbol ${String(attrib.symbol ?? "—")}` : "leg detail"}
      />

      {/* Soak charter: a commissioning leg's return decides NOTHING (L3.2). */}
      <Banner tone="info">
        <span className="font-semibold">COMMISSIONING.</span> This leg is in the paper soak — its
        return curve is observational plumbing verification and decides nothing.
      </Banner>

      {attrib && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatTile
            label="Cum realized"
            value={signedCurrency(numOrNull(attrib.realized_pnl))}
            sub={ts(strOrNull(attrib.source_ts) ?? strOrNull(attrib.recorded_at))}
          />
          <StatTile label="Unrealized" value={signedCurrency(numOrNull(attrib.unrealized_pnl))} />
          <StatTile label="Position" value={qty(numOrNull(attrib.position_qty))} />
          <StatTile
            label="Return (indexed)"
            value={lastRow ? `${Number(lastRow.cumulative_return_pct).toFixed(2)}%` : "—"}
            sub={lastRow ? `NAV ${Number(lastRow.indexed_nav).toFixed(3)}` : "no series yet"}
          />
        </div>
      )}

      <SourcedPanel
        title="Return curve (indexed NAV)"
        source="leg_return_series"
        lastSync={lastSync}
        provenance={points.length >= 2 ? "DERIVED" : "NO-FEED"}
        cadenceMs={LEG_CADENCE}
        note="Attribution-based, not broker-account. Unitized: reallocations issue/redeem units at the current NAV — the curve moves only on this leg's P&L."
      >
        {points.length >= 2 ? (
          <LegReturnCurve points={points} />
        ) : (
          <NoFeedBody
            reason={
              seriesRes.ok
                ? "No leg_return_series rows for this leg yet — the series begins when the L2 producer deploys (migration 021 + tagged release)."
                : "leg_return_series not reachable in the mirror (migration 021 not applied yet)."
            }
          />
        )}
      </SourcedPanel>
    </div>
  );
}
