import { createClient } from "@/lib/supabase/server";
import { safeRows, firstRow, numOrNull, strOrNull, type Row } from "@/lib/dashboard-data";
import { SourcedPanel, NoFeedBody } from "@/components/SourcedPanel";
import { PageHeader, StatTile, Banner, HealthDot } from "@/components/atoms";
import { EquityCurve, type EquityPoint } from "@/components/EquityCurve";
import { currency, percent, ts, ago } from "@/lib/format";

export const dynamic = "force-dynamic";

const SNAP_CADENCE = 60_000; // equity snapshots ~1/min

export default async function PortfolioPage() {
  const supabase = await createClient();

  // ── E1: the PORTFOLIO session is the one the bot writes broker-truth equity to via
  //    sync_equity_snapshot(portfolio_id, …) — portfolio_id is the UUID of the `strategies`
  //    row named 'portfolio' (config_watcher.portfolio_uuid), NOT the literal string
  //    'portfolio' (the old .eq matched 0 sessions and fell back to ALL sessions interleaved
  //    — the barcode). Resolve name → uuid here; never hardcode the uuid.
  const portfolioStratRes = await safeRows<Row>(
    supabase.from("strategies").select("id, name, module").or("name.eq.portfolio,module.eq.portfolio").limit(1),
  );
  const portfolioStrategyId = strOrNull(portfolioStratRes.rows[0]?.id);

  const sessionsRes = portfolioStrategyId
    ? await safeRows<Row>(
        supabase
          .from("performance_sessions")
          .select("id, strategy_id, environment, baseline_equity, started_at, is_active")
          .eq("strategy_id", portfolioStrategyId)
          .order("started_at", { ascending: true }),
      )
    : { ok: false as const, rows: [] as Row[] };
  const portfolioSessionIds = sessionsRes.rows.map((r) => String(r.id));
  const activeSession =
    sessionsRes.rows.find((r) => Boolean(r.is_active)) ??
    sessionsRes.rows[sessionsRes.rows.length - 1] ??
    null;

  const activeSessionId = activeSession ? String(activeSession.id) : null;

  // ── Snapshots for the equity CURVE (portfolio sessions, per-leg/retired excluded — E1).
  //    ORDER BY recorded_at DESC (newest-first), then reverse to chronological below.
  //    WHY DESC: PostgREST caps a response at db-max-rows (1000 on this project) REGARDLESS
  //    of `.limit()`, so an ASC fetch returns the OLDEST 1000 rows — which are entirely the
  //    RETIRED session (its first 1000 snapshots, ending ~2026-06-24 18:23 at $99,740) and
  //    contain ZERO active-session rows. That is the staleness bug: the active session was
  //    never in the fetched set. DESC keeps the NEWEST rows, so the active session is always
  //    present (631 rows, well under the cap), plus recent prior-session history for the
  //    "include prior sessions" toggle.
  const curveRes = portfolioSessionIds.length
    ? await safeRows<Row>(
        supabase
          .from("equity_snapshots")
          .select("session_id, equity, cash, pct_return, recorded_at")
          .in("session_id", portfolioSessionIds)
          .order("recorded_at", { ascending: false })
          .limit(6000),
      )
    : { ok: false as const, rows: [] as Row[] };
  const points: EquityPoint[] = curveRes.rows
    .map((r) => ({
      t: String(r.recorded_at),
      equity: Number(r.equity),
      sessionId: String(r.session_id),
    }))
    .filter((p) => Number.isFinite(p.equity) && p.t)
    .reverse(); // DESC fetch → chronological for the chart

  // ── Account numbers + freshness: the ACTIVE session's LATEST snapshot ONLY, via a DIRECT
  //    single-row query. This is cap-immune (LIMIT 1 can never be truncated by db-max-rows)
  //    and decoupled from the curve fetch — the authoritative broker-truth source (D2).
  const latestActiveRes = activeSessionId
    ? await safeRows<Row>(
        supabase
          .from("equity_snapshots")
          .select("equity, cash, pct_return, recorded_at")
          .eq("session_id", activeSessionId)
          .order("recorded_at", { ascending: false })
          .limit(1),
      )
    : { ok: false as const, rows: [] as Row[] };
  const latestSnap = latestActiveRes.rows[0] ?? null;
  const brokerEquity = numOrNull(latestSnap?.equity);
  const brokerCash = numOrNull(latestSnap?.cash);
  const snapTs = strOrNull(latestSnap?.recorded_at);

  // ── Session return = (latest_equity − baseline_equity) / baseline_equity, computed from
  //    the ACTIVE session's own baseline (percent units, matching format.percent()). NOT the
  //    snapshot's stored pct_return, which is relative to whatever session wrote it — when the
  //    active baseline equals the latest equity this correctly reads 0.00%, not the retired
  //    row's stored non-zero return.
  const baselineEquity = numOrNull(activeSession?.baseline_equity);
  const sessionReturnPct =
    brokerEquity != null && baselineEquity != null && baselineEquity !== 0
      ? ((brokerEquity - baselineEquity) / baselineEquity) * 100
      : null;

  // ── Heartbeat: SINGLE freshness source (bot_runs.created_at) drives the header dot (A1/A3).
  //    A NULL equity column is "not reported this beat" — NOT a staleness/"no heartbeat" claim.
  const runRes = await safeRows<Row>(
    supabase
      .from("bot_runs")
      .select("halted, created_at")
      .order("created_at", { ascending: false })
      .limit(1),
  );
  const run = firstRow(runRes);
  const heartbeatTs = strOrNull(run?.created_at);

  // ── Open orders (best-effort).
  const ordersRes = await safeRows<Row>(
    supabase.from("orders").select("status, created_at").order("created_at", { ascending: false }).limit(200),
  );
  const openOrders = ordersRes.ok
    ? ordersRes.rows.filter((o) => {
        const s = String(o.status ?? "").toLowerCase();
        return s === "" || ["open", "pending", "submitted", "accepted", "new", "partially_filled"].includes(s);
      }).length
    : null;

  // ── Positions proxy: distinct legs holding a non-zero qty.
  const legRes = await safeRows<Row>(
    supabase
      .from("leg_attribution_snapshots")
      .select("strategy_id, leg_name, position_qty, recorded_at")
      .order("recorded_at", { ascending: false })
      .limit(200),
  );
  let positionCount: number | null = null;
  if (legRes.ok) {
    const seen = new Set<string>();
    let cnt = 0;
    for (const r of legRes.rows) {
      const key = `${r.strategy_id}:${r.leg_name}`;
      if (seen.has(key)) continue;
      seen.add(key);
      if (Number(r.position_qty ?? 0) !== 0) cnt += 1;
    }
    positionCount = cnt;
  }

  // ── Kill-state posture: canonical dashboard_summary_snapshots else risk_state.
  const summaryRes = await safeRows<Row>(
    supabase.from("dashboard_summary_snapshots").select("*").order("recorded_at", { ascending: false }).limit(1),
  );
  const riskRes = await safeRows<Row>(
    supabase.from("risk_state").select("halted, halt_reason, peak_equity, updated_at").limit(5),
  );
  const summary = firstRow(summaryRes);
  const anyHalted =
    (summary && Boolean(summary.halted)) ||
    riskRes.rows.some((r) => Boolean(r.halted)) ||
    Boolean(run?.halted);
  const haltReason =
    strOrNull(summary?.halt_reason) ?? strOrNull(riskRes.rows.find((r) => r.halted)?.halt_reason) ?? null;
  const postureTs = strOrNull(summary?.recorded_at) ?? strOrNull(riskRes.rows[0]?.updated_at) ?? heartbeatTs;
  const postureProv = summaryRes.ok && summary ? "BROKER" : riskRes.ok ? "DERIVED" : "NO-FEED";

  return (
    <div className="space-y-4">
      <PageHeader
        title="Portfolio"
        subtitle="Broker-truth account state and posture."
        right={<HealthDot lastSync={heartbeatTs} label="engine" />}
      />

      {/* 1 — Equity curve + underwater FIRST (A2). ONE series: the portfolio session (E1);
          session count + range live in the chart header (client), which reports what is
          actually plotted. */}
      <SourcedPanel
        title="Equity curve + underwater"
        source="equity_snapshots"
        lastSync={snapTs}
        provenance={points.length >= 2 ? "BROKER" : "NO-FEED"}
        cadenceMs={SNAP_CADENCE}
        note="Broker-truth portfolio equity (sync_equity_snapshot). Per-leg and retired sessions are excluded."
      >
        {points.length >= 2 ? (
          <EquityCurve points={points} activeSessionId={activeSessionId} />
        ) : (
          <NoFeedBody
            reason={
              portfolioStrategyId
                ? "Fewer than 2 portfolio-session equity points in the mirror."
                : "No 'portfolio' row in strategies — cannot resolve the portfolio session."
            }
          />
        )}
      </SourcedPanel>

      {/* 2 — Account numbers (A2) */}
      <SourcedPanel
        title="Account (broker truth)"
        source="equity_snapshots"
        lastSync={snapTs}
        provenance={latestActiveRes.ok && latestSnap ? "BROKER" : "NO-FEED"}
        cadenceMs={SNAP_CADENCE}
        note={`As of ${ts(snapTs)} · active session ${activeSession ? String(activeSession.id) : "unresolved"}`}
      >
        {latestActiveRes.ok && latestSnap ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatTile label="Equity" value={currency(brokerEquity)} sub={ago(snapTs)} />
            <StatTile label="Cash" value={currency(brokerCash)} sub={ago(snapTs)} />
            <StatTile
              label="Session return"
              value={percent(sessionReturnPct)}
              valueClass={
                sessionReturnPct == null || sessionReturnPct === 0
                  ? "text-[var(--text)]"
                  : sessionReturnPct > 0
                    ? "text-[var(--green)]"
                    : "text-[var(--red)]"
              }
            />
            <StatTile
              label="Open orders"
              value={openOrders == null ? "—" : String(openOrders)}
              sub={positionCount == null ? "positions —" : `${positionCount} positions`}
            />
          </div>
        ) : (
          <NoFeedBody reason="No portfolio equity_snapshots reachable in the mirror." />
        )}
      </SourcedPanel>

      {/* 3 — Kill-state posture (A2) */}
      <SourcedPanel
        title="Kill-state posture"
        source={summaryRes.ok && summary ? "dashboard_summary_snapshots" : "risk_state"}
        lastSync={postureTs}
        provenance={postureProv}
        cadenceMs={SNAP_CADENCE}
      >
        {postureProv === "NO-FEED" ? (
          <NoFeedBody reason="No kill-state mirror reachable." />
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <span
              className="rounded px-2.5 py-1 text-sm font-semibold"
              style={{
                background: anyHalted ? "rgba(239,68,68,0.12)" : "rgba(34,197,94,0.12)",
                color: anyHalted ? "var(--red)" : "var(--green)",
              }}
            >
              {anyHalted ? "HALTED" : "ACTIVE / SAFE"}
            </span>
            {haltReason && <span className="text-sm text-[var(--muted)]">reason: {haltReason}</span>}
            <span className="ml-auto text-xs text-[var(--dim)]">updated {ago(postureTs)}</span>
          </div>
        )}
      </SourcedPanel>

      {/* PAD strip — explicit NO-FEED */}
      <SourcedPanel title="PAD (position/attribution detail)" source="—" provenance="NO-FEED">
        <NoFeedBody reason="PAD strip is not mirrored. Placeholder pending PAD series." />
      </SourcedPanel>

      <Banner tone="info">
        Portfolio rows are BROKER-truth. The engine health dot (header) is the single heartbeat
        freshness source; NULL heartbeat equity means &ldquo;not reported this beat&rdquo; (see Ops),
        never $0 and never &ldquo;no heartbeat.&rdquo; Full heartbeat detail lives on Ops; alerts on
        the Alerts tab.
      </Banner>
    </div>
  );
}
