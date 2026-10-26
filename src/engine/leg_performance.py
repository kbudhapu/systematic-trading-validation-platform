"""Per-leg performance attribution — independent of portfolio reallocation."""

from __future__ import annotations

from dataclasses import dataclass

from src.ingestor.assets import crypto_position_symbol
from src.models import Position


def _normalize_symbol(symbol: str) -> str:
    return crypto_position_symbol(symbol)


def match_position(symbol: str, positions: list[Position]) -> Position | None:
    """Find the open position for a leg's symbol (handles BTC/USD vs BTCUSD)."""
    target = _normalize_symbol(symbol)
    for pos in positions:
        if _normalize_symbol(pos.symbol) == target:
            return pos
    return None


# compute_leg_equity was DELETED (E5). Its "leg equity = session baseline + realized + unrealized"
# fabricated a per-leg equity from a portfolio-level base (account.equity / n_enabled), which made
# a leg's risk decision depend on how many OTHER legs were enabled. The order-path consumer
# (portfolio_risk_governor) now uses broker_equity × the coordinator's intended allocation
# fraction; the dashboard-telemetry consumer uses attribute_leg (D4). match_position is retained.


@dataclass(frozen=True)
class LegAttribution:
    """Honest per-leg attribution (D4). Money conserved:
    realized_pnl + unrealized_pnl + unattributed_residual == realized + matched-position
    unrealized, always — the ambiguous case simply moves the unrealized from
    ``unrealized_pnl`` to ``unattributed_residual`` rather than guessing it into the leg."""

    realized_pnl: float
    unrealized_pnl: float
    unattributed_residual: float
    position_qty: float
    provenance: str


def attribute_leg(
    realized_pnl: float,
    symbol: str,
    positions: list[Position],
    *,
    symbol_uniquely_owned: bool,
) -> LegAttribution:
    """Attribute realized + unrealized to a leg.

    Realized is supplied by the caller (from live_attribution_ledger, never trades).
    Unrealized is SYMBOL-keyed: the client-order-id is a one-way hash so the strategy is
    NOT decodable from it (idempotent_execution.py:45-56). A broker position's unrealized
    is attributed to a leg ONLY when that leg is the UNIQUE enabled owner of the symbol; a
    symbol claimed by >1 enabled leg is AMBIGUOUS and its unrealized goes to
    ``unattributed_residual`` — never guessed into a leg.
    """
    pos = match_position(symbol, positions)
    pos_unrealized = pos.unrealized_pl if pos else 0.0
    pos_qty = pos.qty if pos else 0.0

    if pos is None:
        return LegAttribution(realized_pnl, 0.0, 0.0, 0.0, "LEDGER_REALIZED+NO_POSITION")
    if symbol_uniquely_owned:
        return LegAttribution(
            realized_pnl, pos_unrealized, 0.0, pos_qty,
            "LEDGER_REALIZED+BROKER_UNREALIZED",
        )
    return LegAttribution(
        realized_pnl, 0.0, pos_unrealized, 0.0,
        "LEDGER_REALIZED+AMBIGUOUS_RESIDUAL",
    )
