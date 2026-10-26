"""Asset class helpers for routing stock vs crypto API calls."""

from __future__ import annotations


def infer_asset_class(symbol: str) -> str:
    """Infer ``stock`` or ``crypto`` from a symbol string."""
    if "/" in symbol:
        return "crypto"
    upper = symbol.upper()
    if upper in {"BTCUSD", "ETHUSD", "BTC", "ETH"}:
        return "crypto"
    return "stock"


def crypto_data_symbol(symbol: str) -> str:
    """Alpaca crypto bars use slash pairs (e.g. BTC/USD)."""
    if "/" in symbol:
        return symbol
    upper = symbol.upper()
    if upper.endswith("USD") and len(upper) > 3:
        base = upper[:-3]
        return f"{base}/USD"
    return symbol


def crypto_order_symbol(symbol: str) -> str:
    """Alpaca crypto orders accept BTC/USD format."""
    return crypto_data_symbol(symbol)


def crypto_position_symbol(symbol: str) -> str:
    """Positions API often returns BTCUSD without slash."""
    return symbol.upper().replace("/", "")
