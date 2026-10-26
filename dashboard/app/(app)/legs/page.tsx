import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { safeRows, numOrNull, strOrNull, type Row } from "@/lib/dashboard-data";
import { SourcedPanel, NoFeedBody } from "@/components/SourcedPanel";
import { PageHeader, Banner, Chip, StatTile } from "@/components/atoms";
import { signedCurrency, currency, qty, ts, pnlClass } from "@/lib/format";

export const dynamic = "force-dynamic";

const LEG_CADENCE = 5 * 60_000; // leg attribution ~ every few minutes
// Cash-out forward runs weekdays 22:30 UTC; Fri→Mon leaves a ~72h gap, so a generous cadence
// avoids a false "stale" note over the weekend. The real health signal is the result-keyed dot.
const CF_CADENCE = 80 * 60 * 60_000;

type Leg = {
  key: string;
  strategy_id: string;
  leg_name: string;
  symbol: string;
  realized: number | null;
  unrealized: number | null;
  residual: number | null;
  position: number | null;
  provenance: string;
  asOf: string | null;
};

export default async function LegsPage() {
  const supabase = await createClient();

  // Cash-out forward module HEALTH mirror (cf_status singleton, id=1). The module writes
  // droplet-local SQLite only; this row is its SOLE dashboard-legible signal. It sat armed +
  // invisible from 2026-08-03 until this mirror landed. NO-FEED here => not yet pushed a row.
  const cfRes = await safeRows<Row>(
    supabase
      .from("cf_status")
      .select(
        "mode, last_poll_enddt, detections_count, positions_count, last_run_utc, last_run_result, parser_sha256, parity_ok, updated_at, n_events_detected, n_entered, n_no_trade, n_booked, realized_median_net_cost_1x",
      )
      .eq("id", 1)
      .limit(1),
  );
  const cf = cfRes.rows[0];
  const cfResult = strOrNull(cf?.last_run_result);
  // Dot keyed off last_run_result (per spec): ok/empty are HEALTHY (empty = ran, quiet), error
  // is DANGER. "Didn't run" surfaces separately via the SourcedPanel stale-sync note.
  const cfHealthy = cfResult === "ok" || cfResult === "empty";

  // Cash-out ATTRIBUTION rollup (from cf_status, migration 029). Its realized quantity is a MODELED
  // per-event EDGE FRACTION (median (fixed−purchase)/purchase − cost over booked events), NOT
  // broker-reconciled dollars — so it is shown labeled MODELED_EDGE and NEVER in the $ P&L column
  // unlabeled. n_booked=0 today (never traded) → an honest empty modeled-edge row.
  const cfPresent = cfRes.ok && !!cf;
  const cfNBooked = numOrNull(cf?.n_booked) ?? 0;
  const cfEntered = numOrNull(cf?.n_entered) ?? 0;
  const cfEvents = numOrNull(cf?.n_events_detected) ?? 0;
  const cfEdge = numOrNull(cf?.realized_median_net_cost_1x); // return fraction; null when none booked
  const cfEdgePct = cfEdge == null ? null : `${cfEdge >= 0 ? "+" : ""}${(cfEdge * 100).toFixed(1)}%`;

  // DDR-F1 shakedown HEALTH mirror (ddr_f1_status singleton, id=1). Same recurring gap cash-out had:
  // the runner writes local state only. NO-FEED => armed but not yet pushed a row. Shows DEPLOYED
  // reality ($25k allocation / $21,250 gross cap) — NOT the $30k mirror-spec figure (flagged drift).
  const ddrRes = await safeRows<Row>(
    supabase
      .from("ddr_f1_status")
      .select(
        "mode, kill_switch_active, last_run_result, next_session, last_session, last_run_utc, orders_submitted, fills_captured, max_deviation_bps, deviation_gate_bps, shakedown_session_count, allocation_usd, gross_cap_usd, first_live_session, registration_sha, updated_at",
      )
      .eq("id", 1)
      .limit(1),
  );
  const ddr = ddrRes.rows[0];
  const ddrPresent = ddrRes.ok && !!ddr;
  const ddrResult = strOrNull(ddr?.last_run_result);
  // Dot: ran_ok green; armed_waiting accent (healthy, waiting); ran_no_fills warn (the
  // quiet-vs-broken signal — the pre-open-pull risk manifests here); errored/halted red.
  const ddrTone =
    ddrResult === "ran_ok"
      ? "var(--green)"
      : ddrResult === "armed_waiting"
        ? "var(--accent)"
        : ddrResult === "ran_no_fills"
          ? "var(--warn)"
          : ddrResult === "errored" || ddrResult === "halted"
            ? "var(--red)"
            : "var(--dim)";
  const ddrKill = ddr?.kill_switch_active === true;

  // Tax liability estimate (tax_estimate singleton, id=1) — ADVISORY overlay on realized gains
  // (PR #440 estimator). Tracked-not-encumbered: gains compound against GROSS; this is the
  // after-tax overlay, NOT a reservation. $0 today (nothing realized) is the correct state, and
  // the panel says so plainly rather than going blank. Rate is operator-set + effective-dated.
  const taxRes = await safeRows<Row>(
    supabase
      .from("tax_estimate")
      .select(
        "tax_year, ytd_net_realized, gross_gains, gross_losses, n_lots, n_long_term, effective_rate, rate_assumption, rate_effective_date, tax_owed, gross_capital, after_tax_if_paid_now, liquid_capital, liability_level, liability_message, next_due_date, must_be_liquid_message, last_run_result, updated_at",
      )
      .eq("id", 1)
      .limit(1),
  );
  const tax = taxRes.rows[0];
  const taxPresent = taxRes.ok && !!tax;
  const taxLevel = strOrNull(tax?.liability_level);
  const taxOwed = numOrNull(tax?.tax_owed) ?? 0;
  const taxRate = numOrNull(tax?.effective_rate);
  // Dot: green when no tax owed or covered (OK); warn when approaching; red when owed > liquid.
  const taxTone =
    taxOwed <= 0 || taxLevel === "OK"
      ? "var(--green)"
      : taxLevel === "WARN"
        ? "var(--warn)"
        : taxLevel === "CRITICAL"
          ? "var(--red)"
          : "var(--dim)";

  // Latest attribution snapshot per (strategy_id, leg_name). Grouping only.
  const res = await safeRows<Row>(
    supabase
      .from("leg_attribution_snapshots")
      .select(
        "strategy_id, leg_name, symbol, realized_pnl, unrealized_pnl, unattributed_residual, position_qty, provenance, recorded_at, source_ts",
      )
      .order("recorded_at", { ascending: false })
      .limit(500),
  );

  const seen = new Set<string>();
  const legs: Leg[] = [];
  for (const r of res.rows) {
    const key = `${r.strategy_id}:${r.leg_name}`;
    if (seen.has(key)) continue; // rows are recorded_at DESC, so first = latest
    seen.add(key);
    legs.push({
      key,
      strategy_id: String(r.strategy_id ?? "—"),
      leg_name: String(r.leg_name ?? "—"),
      symbol: String(r.symbol ?? "—"),
      realized: numOrNull(r.realized_pnl),
      unrealized: numOrNull(r.unrealized_pnl),
      residual: numOrNull(r.unattributed_residual),
      position: numOrNull(r.position_qty),
      provenance: String(r.provenance ?? "DERIVED").toUpperCase(),
      asOf: strOrNull(r.source_ts) ?? strOrNull(r.recorded_at),
    });
  }
  legs.sort((a, b) => a.strategy_id.localeCompare(b.strategy_id) || a.leg_name.localeCompare(b.leg_name));

  const latestAsOf = legs.reduce<string | null>(
    (acc, l) => (l.asOf && (!acc || l.asOf > acc) ? l.asOf : acc),
    null,
  );
  // Panel provenance: REGISTRY if any leg is registry-sourced, else DERIVED.
  const panelProv = res.ok
    ? legs.some((l) => l.provenance === "REGISTRY")
      ? "REGISTRY"
      : "DERIVED"
    : "NO-FEED";

  // --- Reconciliation inputs (broker equity + inception baseline) ---
  // Item-5 fix: this block had the uuid-vs-name bug that portfolio/page.tsx (E1) already fixed.
  // It matched `strategy_id === "portfolio"` — the literal STRING — but strategy_id is the UUID of
  // the `strategies` row named 'portfolio'. That comparison matched ZERO rows and fell through to
  // `sessRes.rows[0]`, an ARBITRARY active session (a per-leg one), so `inception` was some leg's
  // baseline. It then paired that with a `brokerEquity` taken from the newest equity_snapshots row
  // across ALL sessions — per-leg rows included. Two unrelated books, subtracted: the
  // "unattributed residual" below was computed against a baseline that was not the portfolio's.
  // Resolve name -> uuid here, exactly as the portfolio page does; never hardcode the uuid.
  const portfolioStratRes = await safeRows<Row>(
    supabase
      .from("strategies")
      .select("id, name, module")
      .or("name.eq.portfolio,module.eq.portfolio")
      .limit(1),
  );
  const portfolioStrategyId = strOrNull(portfolioStratRes.rows[0]?.id);

  const sessRes = portfolioStrategyId
    ? await safeRows<Row>(
        supabase
          .from("performance_sessions")
          .select("id, strategy_id, baseline_equity, is_active")
          .eq("strategy_id", portfolioStrategyId)
          .order("started_at", { ascending: true }),
      )
    : { ok: false as const, rows: [] as Row[] };
  const portfolioSess =
    sessRes.rows.find((r) => Boolean(r.is_active)) ??
    sessRes.rows[sessRes.rows.length - 1] ??
    null;
  const portfolioSessionId = portfolioSess ? String(portfolioSess.id) : null;
  const inception = numOrNull(portfolioSess?.baseline_equity);

  // Equity must come from THAT session, not the newest row across every session. Cap-immune:
  // a LIMIT 1 on a single session can never be truncated by PostgREST's db-max-rows.
  const snapRes = portfolioSessionId
    ? await safeRows<Row>(
        supabase
          .from("equity_snapshots")
          .select("equity, recorded_at")
          .eq("session_id", portfolioSessionId)
          .order("recorded_at", { ascending: false })
          .limit(1),
      )
    : { ok: false as const, rows: [] as Row[] };
  const brokerEquity = numOrNull(snapRes.rows[0]?.equity);

  // Σ(realized + unrealized) across legs — reconciliation arithmetic, not stats.
  const sumLegPnl = legs.reduce(
    (acc, l) => acc + (l.realized ?? 0) + (l.unrealized ?? 0),
    0,
  );
  const canReconcile = brokerEquity != null && inception != null && legs.length > 0;
  const unattributed = canReconcile
    ? (brokerEquity as number) - (inception as number) - sumLegPnl
    : null;
  const residualPct =
    canReconcile && brokerEquity && brokerEquity !== 0
      ? Math.abs((unattributed as number) / (brokerEquity as number)) * 100
      : null;
  const residualWarn = residualPct != null && residualPct > 1;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Legs"
        subtitle="Per-leg attribution (broker-derived). No per-leg % until the PAD series lands."
      />

      <Banner tone="info">
        <span className="font-semibold">Soak charter.</span> Legs are in COMMISSIONING —
        attribution is broker-derived for observation only and is not a trading signal.
      </Banner>

      <SourcedPanel
        title="Cash-out forward — module health"
        source="cf_status"
        lastSync={strOrNull(cf?.last_run_utc)}
        provenance={cfRes.ok && cf ? "DERIVED" : "NO-FEED"}
        cadenceMs={CF_CADENCE}
      >
        {cfRes.ok && cf ? (
          <div className="space-y-3 text-sm">
            <div className="flex flex-wrap items-center gap-3">
              <span
                className="inline-flex items-center gap-1.5"
                title={`last run result: ${cfResult ?? "unknown"}`}
              >
                <span
                  className="h-2 w-2 shrink-0 rounded-full"
                  style={{ background: cfHealthy ? "var(--green)" : "var(--red)" }}
                  aria-hidden
                />
                <span className="text-xs text-[var(--muted)]">
                  {cfResult === "empty"
                    ? "ran — quiet (no new detections)"
                    : cfResult === "ok"
                      ? "ran — detections found"
                      : cfResult === "error"
                        ? "errored"
                        : "unknown"}
                </span>
              </span>
              <Chip>{strOrNull(cf?.mode) === "armed" ? "ARMED" : "OBSERVE"}</Chip>
              <span className="prov-badge">{cf?.parity_ok ? "PARITY OK" : "PARITY?"}</span>
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <StatTile label="Detections" value={String(numOrNull(cf?.detections_count) ?? 0)} />
              <StatTile label="Positions" value={String(numOrNull(cf?.positions_count) ?? 0)} />
              <StatTile label="Last poll" value={strOrNull(cf?.last_poll_enddt) ?? "—"} />
              <StatTile label="Last run" value={ts(strOrNull(cf?.last_run_utc))} />
            </div>
            <div className="font-mono text-[0.68rem] text-[var(--dim)]">
              parser {String(strOrNull(cf?.parser_sha256) ?? "—").slice(0, 12)}
            </div>
          </div>
        ) : (
          <NoFeedBody reason="cf_status not yet populated — module armed but has not pushed a health row (runs weekdays 22:30 UTC)." />
        )}
      </SourcedPanel>

      <SourcedPanel
        title="DDR-F1 — shakedown leg health"
        source="ddr_f1_status"
        lastSync={strOrNull(ddr?.last_run_utc)}
        provenance={ddrPresent ? "DERIVED" : "NO-FEED"}
        cadenceMs={CF_CADENCE}
      >
        {ddrPresent ? (
          <div className="space-y-3 text-sm">
            <div className="flex flex-wrap items-center gap-3">
              <span
                className="inline-flex items-center gap-1.5"
                title={`last run result: ${ddrResult ?? "unknown"}`}
              >
                <span
                  className="h-2 w-2 shrink-0 rounded-full"
                  style={{ background: ddrTone }}
                  aria-hidden
                />
                <span className="text-xs text-[var(--muted)]">
                  {ddrResult === "armed_waiting"
                    ? "armed — waiting (no session yet)"
                    : ddrResult === "ran_ok"
                      ? "ran — orders filled"
                      : ddrResult === "ran_no_fills"
                        ? "ran — NO fills (quiet-or-broken)"
                        : ddrResult === "errored"
                          ? "errored"
                          : ddrResult === "halted"
                            ? "halted (kill switch)"
                            : "unknown"}
                </span>
              </span>
              <Chip>{strOrNull(ddr?.mode) === "armed" ? "ARMED" : "DRY-RUN"}</Chip>
              {ddrKill && (
                <span className="prov-badge" style={{ color: "var(--red)" }}>
                  KILL SWITCH
                </span>
              )}
              <span className="prov-badge">SHAKEDOWN · PAPER · VERDICT-PENDING</span>
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <StatTile
                label="Shakedown sessions"
                value={String(numOrNull(ddr?.shakedown_session_count) ?? 0)}
              />
              <StatTile
                label="Orders / fills"
                value={`${numOrNull(ddr?.orders_submitted) ?? "—"} / ${numOrNull(ddr?.fills_captured) ?? "—"}`}
              />
              <StatTile
                label="Max dev / gate (bps)"
                value={
                  numOrNull(ddr?.max_deviation_bps) == null
                    ? "—"
                    : `${(numOrNull(ddr?.max_deviation_bps) as number).toFixed(1)} / ${numOrNull(ddr?.deviation_gate_bps) ?? "—"}`
                }
              />
              <StatTile label="Gross cap" value={currency(numOrNull(ddr?.gross_cap_usd))} />
              <StatTile label="Next session" value={strOrNull(ddr?.next_session) ?? "—"} />
              <StatTile label="Last session" value={strOrNull(ddr?.last_session) ?? "—"} />
              <StatTile label="Allocation" value={currency(numOrNull(ddr?.allocation_usd))} />
              <StatTile label="Last run" value={ts(strOrNull(ddr?.last_run_utc))} />
            </div>
            <div className="font-mono text-[0.68rem] text-[var(--dim)]">
              sizes vs {currency(numOrNull(ddr?.allocation_usd))} allocation (gross cap{" "}
              {currency(numOrNull(ddr?.gross_cap_usd))}) · reg{" "}
              {String(strOrNull(ddr?.registration_sha) ?? "—").slice(0, 12)}
            </div>
          </div>
        ) : (
          <NoFeedBody reason="ddr_f1_status not yet populated — shakedown armed (first session 2026-08-12 09:25 ET) but has not pushed a health row." />
        )}
      </SourcedPanel>

      <SourcedPanel
        title="Tax liability — advisory overlay"
        source="tax_estimate"
        lastSync={strOrNull(tax?.updated_at)}
        provenance={taxPresent ? "DERIVED" : "NO-FEED"}
        cadenceMs={CF_CADENCE}
      >
        {taxPresent ? (
          <div className="space-y-3 text-sm">
            <div className="flex flex-wrap items-center gap-3">
              <span
                className="inline-flex items-center gap-1.5"
                title={`liability vs liquid: ${taxLevel ?? "unknown"}`}
              >
                <span
                  className="h-2 w-2 shrink-0 rounded-full"
                  style={{ background: taxTone }}
                  aria-hidden
                />
                <span className="text-xs text-[var(--muted)]">
                  {taxOwed <= 0
                    ? "no tax owed — nothing realized yet"
                    : taxLevel === "OK"
                      ? "covered by liquid capital"
                      : taxLevel === "WARN"
                        ? "approaching liquid capital"
                        : taxLevel === "CRITICAL"
                          ? "EXCEEDS liquid capital"
                          : "unknown"}
                </span>
              </span>
              <Chip>{taxRate == null ? "—" : `${(taxRate * 100).toFixed(0)}% effective`}</Chip>
              <span className="prov-badge">ADVISORY · TRACKED-NOT-ENCUMBERED</span>
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <StatTile label="Gross capital" value={currency(numOrNull(tax?.gross_capital))} />
              <StatTile label="Tax owed (overlay)" value={currency(taxOwed)} />
              <StatTile
                label="After-tax if paid now"
                value={currency(numOrNull(tax?.after_tax_if_paid_now))}
              />
              <StatTile label="Liquid capital" value={currency(numOrNull(tax?.liquid_capital))} />
              <StatTile
                label="YTD net realized"
                value={signedCurrency(numOrNull(tax?.ytd_net_realized))}
              />
              <StatTile label="Closed lots" value={String(numOrNull(tax?.n_lots) ?? 0)} />
              <StatTile label="Long-term (flagged)" value={String(numOrNull(tax?.n_long_term) ?? 0)} />
              <StatTile label="Tax year" value={String(numOrNull(tax?.tax_year) ?? "—")} />
            </div>
            <div
              className={`rounded-md border px-3 py-1.5 text-xs ${
                taxOwed <= 0 || taxLevel === "OK"
                  ? "border-[var(--border-soft)] bg-[var(--card-2)] text-[var(--muted)]"
                  : "border-[var(--warn)] bg-[var(--warn-soft)] text-[var(--warn)]"
              }`}
            >
              {strOrNull(tax?.must_be_liquid_message) ?? "—"}
            </div>
            {taxLevel && taxLevel !== "OK" && taxOwed > 0 && (
              <p className="text-xs" style={{ color: taxTone }}>
                {strOrNull(tax?.liability_message)}
              </p>
            )}
            <p className="text-[0.68rem] leading-relaxed text-[var(--dim)]">
              <span className="prov-badge">OVERLAY</span> Gains compound against{" "}
              <span className="text-[var(--muted)]">gross</span> — the tax reserve is{" "}
              <span className="text-[var(--muted)]">tracked, not encumbered</span>;
              after-tax-if-paid-now is a visibility figure, not a reservation. Rate is operator-set
              and effective-dated forward:{" "}
              <span className="text-[var(--muted)]">{strOrNull(tax?.rate_assumption) ?? "—"}</span>{" "}
              (since {strOrNull(tax?.rate_effective_date) ?? "—"}); due{" "}
              {strOrNull(tax?.next_due_date) ?? "—"}.
            </p>
          </div>
        ) : (
          <NoFeedBody reason="tax_estimate not yet populated — the estimator computes $0 today (nothing realized) and the mirror has not pushed a row." />
        )}
      </SourcedPanel>

      <SourcedPanel
        title="Per-leg attribution"
        source="leg_attribution_snapshots + cf_status (cash-out, modeled)"
        lastSync={latestAsOf}
        provenance={panelProv}
        cadenceMs={LEG_CADENCE}
      >
        {res.ok && (legs.length > 0 || cfPresent) ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="text-left text-[0.68rem] uppercase tracking-wide text-[var(--muted)]">
                  <th className="py-1.5 pr-3 font-medium">Strategy / leg</th>
                  <th className="py-1.5 pr-3 font-medium">Symbol</th>
                  <th className="py-1.5 pr-3 text-right font-medium">Cum P&amp;L</th>
                  <th className="py-1.5 pr-3 text-right font-medium">Unrealized</th>
                  <th className="py-1.5 pr-3 text-right font-medium">Position</th>
                  {/* NO per-leg % column: blocked-on-PAD-series (per-leg return
                      needs the PAD equity series which is not mirrored yet). */}
                  <th className="py-1.5 pr-3 font-medium">Prov.</th>
                  <th className="py-1.5 font-medium">As of</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-soft)]">
                {legs.map((l) => (
                  <tr key={l.key}>
                    <td className="py-2 pr-3">
                      <Link
                        href={`/legs/${encodeURIComponent(l.leg_name)}`}
                        className="text-[var(--text)] hover:text-[var(--accent)]"
                      >
                        {l.leg_name}
                      </Link>
                      <div className="text-[0.68rem] text-[var(--dim)]">{l.strategy_id}</div>
                    </td>
                    <td className="py-2 pr-3 text-[var(--muted)]">{l.symbol}</td>
                    <td className={`py-2 pr-3 text-right tnum ${pnlClass(l.realized)}`}>
                      {signedCurrency(l.realized)}
                    </td>
                    <td className={`py-2 pr-3 text-right tnum ${pnlClass(l.unrealized)}`}>
                      {signedCurrency(l.unrealized)}
                    </td>
                    <td className="py-2 pr-3 text-right tnum text-[var(--text)]">
                      {qty(l.position)}
                    </td>
                    <td className="py-2 pr-3">
                      <span className="prov-badge">{l.provenance}</span>
                    </td>
                    <td className="py-2 text-[0.68rem] text-[var(--dim)] tnum">{ts(l.asOf)}</td>
                  </tr>
                ))}
                {/* Cash-out: SAME row layout, HONEST content. Its realized quantity is a MODELED
                    per-event edge fraction (not $), so the Cum P&L cell carries the edge% + n_booked
                    with an explicit modeled marker, the $-typed Unrealized cell is n/a, and the
                    provenance is MODELED_EDGE. NEVER a $ figure in the Cum P&L column. */}
                {cfPresent && (
                  <tr key="cashout_forward" className="border-t-2 border-[var(--border)]">
                    <td className="py-2 pr-3">
                      <span className="text-[var(--text)]">cashout_reverse_splits</span>
                      <div className="text-[0.68rem] text-[var(--dim)]">cashout_forward · fixed arm</div>
                    </td>
                    <td className="py-2 pr-3 text-[var(--muted)]">SC 13E-3</td>
                    <td
                      className="py-2 pr-3 text-right tnum text-[var(--muted)]"
                      title="MODELED median per-event edge fraction = (fixed − purchase)/purchase − cost over booked cash-outs. NOT broker-reconciled dollars."
                    >
                      {cfNBooked > 0 && cfEdgePct ? `${cfEdgePct} edge` : "— edge"}
                      <span className="ml-1 rounded bg-[var(--card-2)] px-1 text-[0.58rem] uppercase tracking-wide text-[var(--dim)]">
                        modeled
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-right tnum text-[var(--dim)]" title="Cash-out has no unrealized dollar P&L — it books at the fixed cash-out price.">
                      n/a
                    </td>
                    <td className="py-2 pr-3 text-right tnum text-[var(--text)]">{cfNBooked} booked</td>
                    <td className="py-2 pr-3">
                      <span className="prov-badge" title="Modeled per-event edge — not broker-reconciled dollars">
                        MODELED_EDGE
                      </span>
                    </td>
                    <td className="py-2 text-[0.68rem] text-[var(--dim)] tnum">{ts(strOrNull(cf?.last_run_utc))}</td>
                  </tr>
                )}
              </tbody>
            </table>
            {cfPresent && (
              <p className="mt-2 text-[0.68rem] leading-relaxed text-[var(--dim)]">
                <span className="prov-badge">MODELED_EDGE</span>{" "}
                Cash-out shows a <span className="text-[var(--muted)]">modeled per-event edge fraction</span> (median
                of (fixed − purchase)/purchase − cost over {cfNBooked} booked cash-out{cfNBooked === 1 ? "" : "s"}) —{" "}
                <span className="text-[var(--muted)]">not broker-reconciled dollars</span>; the $ columns do not apply to it.
                n_booked=0 = never traded (detected {cfEvents} event{cfEvents === 1 ? "" : "s"}, entered {cfEntered}).
              </p>
            )}
          </div>
        ) : (
          <NoFeedBody reason="leg_attribution_snapshots not reachable in the mirror." />
        )}
      </SourcedPanel>

      {/* Reconciliation footer */}
      <SourcedPanel
        title="Reconciliation"
        source="leg_attribution_snapshots × equity_snapshots × performance_sessions"
        lastSync={latestAsOf}
        provenance={canReconcile ? "DERIVED" : "NO-FEED"}
        cadenceMs={LEG_CADENCE}
      >
        {canReconcile ? (
          <div className="space-y-2 text-sm">
            <div className="font-mono text-xs text-[var(--muted)]">
              Σ leg P&amp;L ({signedCurrency(sumLegPnl)}) + inception ({currency(inception)})
              + unattributed ({signedCurrency(unattributed)}) = broker {currency(brokerEquity)}
            </div>
            <div
              className={`inline-flex items-center gap-2 rounded-md border px-3 py-1.5 ${
                residualWarn
                  ? "border-[var(--warn)] bg-[var(--warn-soft)] text-[var(--warn)]"
                  : "border-[var(--border-soft)] bg-[var(--card-2)] text-[var(--muted)]"
              }`}
            >
              <span className="text-xs">unattributed residual</span>
              <span className="tnum font-semibold">{signedCurrency(unattributed)}</span>
              <span className="text-xs">
                ({residualPct == null ? "—" : residualPct.toFixed(2)}% of broker equity)
              </span>
            </div>
            {residualWarn && (
              <p className="text-xs text-[var(--warn)]">
                Residual exceeds 1% of broker equity — attribution is incomplete
                (warning, not an error: unattributed P&amp;L is expected during commissioning).
              </p>
            )}
          </div>
        ) : (
          <NoFeedBody reason="Missing broker equity, inception baseline, or leg rows — cannot reconcile." />
        )}
      </SourcedPanel>

      {/* Leg lineage / detail */}
      <SourcedPanel title="Leg detail — hash-chain lineage" source="—" provenance="NO-FEED">
        <NoFeedBody reason="Hash-chain lineage is not mirrored to Supabase. Placeholder pending lineage mirror." />
      </SourcedPanel>

      <SourcedPanel title="Demotion gauges" source="—" provenance="NO-FEED">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {["Consistency", "Coverage", "Drift"].map((g) => (
            <div
              key={g}
              className="nofeed flex flex-col items-center justify-center gap-1 py-5 text-center"
            >
              <Chip>{g}</Chip>
              <span className="text-xs text-[var(--dim)]">gauge — NO FEED</span>
            </div>
          ))}
        </div>
      </SourcedPanel>
    </div>
  );
}
