"""Paper/live endpoint cross-contamination guard (final-prep P3).

The project uses one Alpaca key pair (`ALPACA_API_KEY` / `ALPACA_SECRET_KEY`) plus
`ALPACA_BASE_URL` to select paper vs live, rather than distinct paper/live variable
names. That makes a copy-paste error (paper keys against the live endpoint, or
vice-versa) possible. This guard closes the gap: it asserts the resolved endpoint
matches the declared `environment`, so a mismatch fails fast at config load rather
than silently trading in the wrong place.
"""

from __future__ import annotations

PAPER_ENDPOINT_TOKEN = "paper-api"   # https://paper-api.alpaca.markets


class EnvironmentEndpointMismatch(ValueError):
    """Raised when ALPACA_BASE_URL does not match the declared environment."""


def assert_endpoint_matches_environment(environment: str, base_url: str) -> None:
    """Fail fast on a paper/live endpoint mismatch. `backtest` is unconstrained
    (no live endpoint is used). An empty base_url is not checked (unconfigured)."""
    env = str(environment).strip().lower()
    url = str(base_url).strip().lower()
    if not url or env not in {"paper", "live"}:
        return
    is_paper_url = PAPER_ENDPOINT_TOKEN in url
    if env == "paper" and not is_paper_url:
        raise EnvironmentEndpointMismatch(
            f"environment=paper but ALPACA_BASE_URL={base_url!r} is not the paper "
            f"endpoint (expected a '{PAPER_ENDPOINT_TOKEN}' host). Refusing to trade "
            f"paper config against a live endpoint.")
    if env == "live" and is_paper_url:
        raise EnvironmentEndpointMismatch(
            f"environment=live but ALPACA_BASE_URL={base_url!r} is the PAPER endpoint. "
            f"Refusing to run live config against the paper endpoint.")
