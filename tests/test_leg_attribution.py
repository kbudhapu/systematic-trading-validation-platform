"""D4 — honest per-leg attribution (leg_performance.attribute_leg).

Contract: money is conserved (realized + unrealized + residual == realized + matched
position unrealized); ambiguous symbols never guess unrealized into a leg; an empty ledger
(realized=0) with no position yields all-zero.
"""
from __future__ import annotations

from src.engine.leg_performance import attribute_leg
from src.models import Position


def _pos(symbol="QQQ", qty=10.0, unrealized=25.0):
    return Position(symbol=symbol, qty=qty, side="long", avg_entry_price=100.0,
                    unrealized_pl=unrealized)


def test_unique_owner_attributes_unrealized():
    a = attribute_leg(100.0, "QQQ", [_pos(unrealized=25.0)], symbol_uniquely_owned=True)
    assert a.realized_pnl == 100.0
    assert a.unrealized_pnl == 25.0
    assert a.unattributed_residual == 0.0
    assert a.position_qty == 10.0
    assert a.provenance == "LEDGER_REALIZED+BROKER_UNREALIZED"


def test_ambiguous_symbol_excludes_unrealized_to_residual():
    """A symbol owned by >1 leg: unrealized must NOT be guessed into the leg."""
    a = attribute_leg(100.0, "QQQ", [_pos(unrealized=25.0)], symbol_uniquely_owned=False)
    assert a.unrealized_pnl == 0.0                 # never guessed into the leg
    assert a.unattributed_residual == 25.0         # kept separate
    assert a.position_qty == 0.0
    assert a.provenance == "LEDGER_REALIZED+AMBIGUOUS_RESIDUAL"


def test_money_conserved_both_cases():
    for unique in (True, False):
        a = attribute_leg(100.0, "QQQ", [_pos(unrealized=25.0)], symbol_uniquely_owned=unique)
        assert a.realized_pnl + a.unrealized_pnl + a.unattributed_residual == 125.0


def test_empty_ledger_no_position_is_zero():
    a = attribute_leg(0.0, "QQQ", [], symbol_uniquely_owned=True)
    assert (a.realized_pnl, a.unrealized_pnl, a.unattributed_residual, a.position_qty) == (
        0.0, 0.0, 0.0, 0.0,
    )
    assert a.provenance == "LEDGER_REALIZED+NO_POSITION"


def test_no_matching_position_keeps_realized_only():
    a = attribute_leg(42.0, "SPY", [_pos(symbol="QQQ")], symbol_uniquely_owned=True)
    assert a.realized_pnl == 42.0
    assert a.unrealized_pnl == 0.0
    assert a.unattributed_residual == 0.0
    assert a.provenance == "LEDGER_REALIZED+NO_POSITION"
