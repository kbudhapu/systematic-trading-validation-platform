"""
Dual-write operational data to Supabase for the dashboard.

SQLite remains the local cache; Supabase is the remote control plane.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from src.control.leg_return import LegNavState, cumulative_return_pct, next_nav_state
from src.control.session_manager import SessionManager
from src.control.supabase_client import get_supabase, run_with_supabase_retry
from src.engine.leg_performance import attribute_leg
from src.models import Order, OrderResult, Position

log = structlog.get_logger()


class SupabaseSync:
    """Push trades, orders, bot runs, equity snapshots, and events."""

    def __init__(self, session_manager: SessionManager) -> None:
        self.sessions = session_manager
        # L2: unitization state per leg for leg_return_series. In-memory cache; a cold start
        # (restart) re-seeds from the latest mirrored row so NAV continues, never resets.
        self._leg_nav: dict[str, LegNavState | None] = {}

    def log_system_event(
        self,
        event_type: str,
        message: str,
        severity: str = "info",
        metadata: dict | None = None,
    ) -> None:
        client = get_supabase()
        if client is None:
            return
        try:
            client.table("system_events").insert(
                {
                    "event_type": event_type,
                    "severity": severity,
                    "message": message,
                    "metadata": metadata or {},
                }
            ).execute()
        except Exception as e:
            log.error("system_event_sync_failed", error=str(e))

    def sync_order_submitted(
        self,
        strategy_id: str | None,
        order: Order,
        expected_price: float,
    ) -> str | None:
        client = get_supabase()
        if client is None:
            return None
        try:
            resp = client.table("orders").insert(
                {
                    "strategy_id": strategy_id,
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "qty": order.qty,
                    "order_type": order.order_type,
                    "status": "pending",
                    "expected_price": expected_price,
                }
            ).execute()
            return resp.data[0]["id"] if resp.data else None
        except Exception as e:
            log.error("order_sync_failed", error=str(e))
            return None

    def sync_order_filled(
        self,
        order_id: str | None,
        result: OrderResult,
        strategy_id: str | None,
    ) -> None:
        client = get_supabase()
        if client is None:
            return
        slippage_bps = None
        try:
            if order_id:
                row = (
                    client.table("orders")
                    .select("expected_price")
                    .eq("id", order_id)
                    .limit(1)
                    .execute()
                )
                if row.data and row.data[0].get("expected_price"):
                    exp = float(row.data[0]["expected_price"])
                    if exp > 0:
                        slippage_bps = (result.filled_price - exp) / exp * 10000

                client.table("orders").update(
                    {
                        "status": result.status,
                        "filled_price": result.filled_price,
                        "filled_at": result.filled_at.isoformat(),
                        "slippage_bps": slippage_bps,
                    }
                ).eq("id", order_id).execute()
            else:
                client.table("orders").insert(
                    {
                        "strategy_id": strategy_id,
                        "symbol": result.symbol,
                        "side": result.side.value,
                        "qty": result.qty,
                        "status": result.status,
                        "filled_price": result.filled_price,
                        "filled_at": result.filled_at.isoformat(),
                    }
                ).execute()

            client.table("trades").insert(
                {
                    "strategy_id": strategy_id,
                    "timestamp": result.filled_at.isoformat(),
                    "symbol": result.symbol,
                    "direction": "long" if result.side.value == "buy" else "short",
                    "qty": result.qty,
                    "entry_price": result.filled_price,
                    "status": "filled",
                }
            ).execute()
        except Exception as e:
            log.error("fill_sync_failed", error=str(e))

    def sync_order_rejected(
        self,
        strategy_id: str | None,
        order: Order,
        reason: str,
    ) -> None:
        client = get_supabase()
        if client is None:
            return
        try:
            client.table("orders").insert(
                {
                    "strategy_id": strategy_id,
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "qty": order.qty,
                    "status": "rejected",
                    "reject_reason": reason,
                }
            ).execute()
            self.log_system_event(
                "order_rejected",
                f"{order.symbol} {order.side.value} rejected: {reason}",
                severity="warning",
            )
        except Exception as e:
            log.error("reject_sync_failed", error=str(e))

    def sync_bot_run(
        self,
        strategy_id: str | None,
        environment: str,
        status: str,
        message: str = "",
        cycle_ms: int | None = None,
        equity: float | None = None,
        drawdown_pct: float | None = None,
        halted: bool = False,
    ) -> None:
        try:
            run_with_supabase_retry(
                lambda c: c.table("bot_runs").insert(
                    {
                        "strategy_id": strategy_id,
                        "environment": environment,
                        "status": status,
                        "message": message,
                        "cycle_ms": cycle_ms,
                        "equity": equity,
                        "drawdown_pct": drawdown_pct,
                        "halted": halted,
                    }
                ).execute(),
                label="sync_bot_run",
            )
        except Exception as e:
            log.error("bot_run_sync_failed", error=str(e))

    def sync_equity_snapshot(
        self,
        strategy_id: str | None,
        environment: str,
        equity: float,
        cash: float,
    ) -> None:
        if strategy_id is None:
            return
        session = self.sessions.ensure_session(strategy_id, environment, equity)
        if session is None:
            return

        baseline = session.baseline_equity
        pct = ((equity - baseline) / baseline * 100) if baseline > 0 else 0.0

        client = get_supabase()
        if client is None:
            return
        try:
            client.table("equity_snapshots").insert(
                {
                    "session_id": session.session_id,
                    "equity": equity,
                    "cash": cash,
                    "pct_return": pct,
                }
            ).execute()
        except Exception as e:
            log.error("equity_snapshot_failed", error=str(e))

    def _leg_realized_from_ledger(self, strategy_keys: list[str]) -> float:
        """Realized P&L for a leg = SUM(pnl) from live_attribution_ledger (NEVER the trades
        table). Keyed by either the UUID or the string strategy id (the ledger may use
        either), so we sum over both without guessing which."""
        client = get_supabase()
        if client is None:
            return 0.0
        keys = [k for k in strategy_keys if k]
        if not keys:
            return 0.0
        try:
            resp = (
                client.table("live_attribution_ledger")
                .select("pnl")
                .in_("strategy_id", keys)
                .execute()
            )
            return float(sum((row.get("pnl") or 0.0) for row in (resp.data or [])))
        except Exception as e:
            log.error("leg_realized_ledger_read_failed", error=str(e))
            return 0.0

    def sync_leg_attribution(
        self,
        strategy_id: str | None,
        leg_name: str,
        symbol: str,
        positions: list[Position],
        *,
        symbol_uniquely_owned: bool,
        capital_base: float | None = None,
    ) -> None:
        """Record honest per-leg attribution into leg_attribution_snapshots (D4 / migration
        017), replacing the old sync_leg_equity_snapshot which wrote DERIVED leg "equity"
        into the broker-truth equity_snapshots table (dashboard audit A1/F4 MIXED-table hazard).

        realized_pnl  = SUM(pnl) from live_attribution_ledger (never trades).
        unrealized_pnl= matched broker position unrealized, attributed ONLY when this leg is
                        the UNIQUE owner of the symbol (client-order-id is a one-way hash, so
                        attribution is symbol-keyed); an ambiguous symbol's unrealized goes to
                        unattributed_residual — never guessed into a leg.
        capital_base  = budgets[leg] × portfolio equity at this tick (L2). When provided, the
                        unitized leg_return_series row is appended alongside the attribution.
        """
        if strategy_id is None:
            return
        client = get_supabase()
        if client is None:
            return

        realized = self._leg_realized_from_ledger([strategy_id, leg_name])
        attrib = attribute_leg(
            realized, symbol, positions, symbol_uniquely_owned=symbol_uniquely_owned
        )
        try:
            client.table("leg_attribution_snapshots").insert(
                {
                    "strategy_id": strategy_id,
                    "leg_name": leg_name,
                    "symbol": symbol,
                    "realized_pnl": attrib.realized_pnl,
                    "unrealized_pnl": attrib.unrealized_pnl,
                    "unattributed_residual": attrib.unattributed_residual,
                    "position_qty": attrib.position_qty,
                    "provenance": attrib.provenance,
                    "source_ts": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()
        except Exception as e:
            log.error("leg_attribution_sync_failed", leg=leg_name, error=str(e))

        if capital_base is not None:
            self.sync_leg_return(
                strategy_id,
                leg_name,
                pnl_cum=attrib.realized_pnl + attrib.unrealized_pnl,
                capital_base=capital_base,
            )

    def _load_leg_nav_state(self, client, leg_name: str) -> LegNavState | None:
        """Cold-start: continue the unitization from the latest mirrored row (never reset NAV)."""
        try:
            resp = (
                client.table("leg_return_series")
                .select("indexed_nav, dollar_pnl, units, capital_base")
                .eq("leg_name", leg_name)
                .order("recorded_at", desc=True)
                .limit(1)
                .execute()
            )
            if resp.data:
                row = resp.data[0]
                return LegNavState(
                    units=float(row["units"]),
                    nav=float(row["indexed_nav"]),
                    pnl_cum=float(row["dollar_pnl"]),
                    capital_base=float(row["capital_base"]),
                )
        except Exception as e:
            log.warning("leg_return_state_load_failed", leg=leg_name, error=str(e))
        return None

    def sync_leg_return(
        self,
        strategy_id: str | None,
        leg_name: str,
        *,
        pnl_cum: float,
        capital_base: float,
    ) -> None:
        """L2 producer: append the unitized indexed-NAV row for one leg (best-effort mirror).

        Unitization (src/control/leg_return.py): P&L moves NAV; capital reallocations
        issue/redeem units at the CURRENT NAV so the curve is reallocation-neutral by
        construction. Never gates trading; failures log and drop the tick.
        """
        client = get_supabase()
        if client is None:
            return
        if leg_name not in self._leg_nav:
            self._leg_nav[leg_name] = self._load_leg_nav_state(client, leg_name)
        state = next_nav_state(self._leg_nav[leg_name], pnl_cum, capital_base)
        self._leg_nav[leg_name] = state
        try:
            client.table("leg_return_series").insert(
                {
                    "strategy_id": strategy_id,
                    "leg_name": leg_name,
                    "indexed_nav": state.nav,
                    "cumulative_return_pct": cumulative_return_pct(state),
                    "dollar_pnl": state.pnl_cum,
                    "units": state.units,
                    "capital_base": state.capital_base,
                    "provenance": "DERIVED",
                }
            ).execute()
        except Exception as e:
            log.error("leg_return_sync_failed", leg=leg_name, error=str(e))

    def sync_risk_state(
        self,
        strategy_id: str | None,
        peak_equity: float,
        halted: bool,
        halt_reason: str = "",
    ) -> None:
        if strategy_id is None:
            return
        client = get_supabase()
        if client is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            existing = (
                client.table("risk_state")
                .select("id")
                .eq("strategy_id", strategy_id)
                .limit(1)
                .execute()
            )
            payload = {
                "peak_equity": peak_equity,
                "halted": halted,
                "halt_reason": halt_reason or None,
                "updated_at": now,
            }
            if existing.data:
                client.table("risk_state").update(payload).eq(
                    "strategy_id", strategy_id
                ).execute()
            else:
                client.table("risk_state").insert(
                    {"strategy_id": strategy_id, **payload}
                ).execute()

            if halted:
                self.log_system_event(
                    "halt",
                    halt_reason or "Circuit breaker triggered",
                    severity="critical",
                    metadata={"peak_equity": peak_equity},
                )
        except Exception as e:
            log.error("risk_state_sync_failed", error=str(e))

    def sync_backtest_result(
        self,
        strategy_id: str | None,
        params: dict,
        result_dict: dict,
    ) -> str | None:
        client = get_supabase()
        if client is None:
            return None
        try:
            curve = result_dict.get("equity_curve", [])
            downsampled = curve[:: max(1, len(curve) // 200)] if curve else []
            resp = client.table("backtest_runs").insert(
                {
                    "strategy_id": strategy_id,
                    "params": params,
                    "sharpe": result_dict.get("sharpe"),
                    "max_drawdown": result_dict.get("max_drawdown"),
                    "total_return": result_dict.get("total_return"),
                    "total_trades": result_dict.get("total_trades"),
                    "win_rate": result_dict.get("win_rate"),
                    "profit_factor": result_dict.get("profit_factor"),
                    "equity_curve": downsampled,
                    "passed": result_dict.get("passed", False),
                    "flags": result_dict.get("flags", []),
                }
            ).execute()
            return resp.data[0]["id"] if resp.data else None
        except Exception as e:
            log.error("backtest_sync_failed", error=str(e))
            return None

    def sync_heartbeat_ping(self, *, environment: str, timestamp: str) -> None:
        """Lightweight remote liveness ping for dashboard dead-man monitoring."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            run_with_supabase_retry(
                lambda c: c.table("engine_heartbeat").upsert(
                    {
                        "environment": environment,
                        "last_successful_cycle_at": timestamp,
                        "updated_at": now,
                    }
                ).execute(),
                label="sync_heartbeat_ping",
            )
        except Exception as e:
            log.error("heartbeat_ping_failed", error=str(e))
