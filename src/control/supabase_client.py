"""
Optional Supabase client for VPS bot sync.

Uses a scoped ``trading_bot_node`` JWT when ``SUPABASE_TRADING_BOT_KEY`` is set.
Falls back to ``SUPABASE_SERVICE_ROLE_KEY`` only during migration (logged once).
When URL and key are unset, all sync operations no-op for local SQLite + YAML dev.
"""

from __future__ import annotations

import os
import random
import time
from functools import lru_cache

import structlog

log = structlog.get_logger()

_LEGACY_SERVICE_ROLE_WARNED = False


def _normalize_supabase_url(url: str) -> str:
    """Strip quotes, trailing slashes, and accidental /rest/v1 suffix."""
    cleaned = url.strip().strip('"').strip("'").rstrip("/")
    if cleaned.endswith("/rest/v1"):
        cleaned = cleaned[: -len("/rest/v1")].rstrip("/")
        log.warning("supabase_url_normalized", hint="Remove /rest/v1 from SUPABASE_URL")
    return cleaned


def resolve_trading_bot_key() -> str:
    """Return the bot-scoped Supabase API key (trading_bot_node JWT)."""
    return (
        os.getenv("SUPABASE_TRADING_BOT_KEY", "").strip()
        or os.getenv("SUPABASE_BOT_KEY", "").strip()
    )


def resolve_service_role_key() -> str:
    return os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()


def resolve_supabase_api_key(*, prefer_bot: bool = True) -> tuple[str, str]:
    """
    Resolve REST API key and channel label.

    Returns (key, channel) where channel is ``trading_bot_node``,
    ``service_role_legacy``, or empty when unconfigured.
    """
    global _LEGACY_SERVICE_ROLE_WARNED
    if prefer_bot:
        bot_key = resolve_trading_bot_key()
        if bot_key:
            return bot_key, "trading_bot_node"
        legacy = resolve_service_role_key()
        if legacy:
            if not _LEGACY_SERVICE_ROLE_WARNED:
                # D3 / CP7 F3: the bot on legacy service_role BYPASSES all RLS. This is
                # tolerated during the operator transition window (warn, never crash), but
                # outside backtest it is LOUD, once per boot, until the scoped key lands.
                env = os.getenv("TRADING_ENVIRONMENT", "").strip().lower()
                if env == "backtest":
                    log.info(
                        "supabase_using_legacy_service_role",
                        environment=env,
                        hint="Backtest: legacy service_role acceptable.",
                    )
                else:
                    log.warning(
                        "supabase_using_legacy_service_role",
                        environment=env or "unknown",
                        hint=(
                            "Bot is authenticating with SUPABASE_SERVICE_ROLE_KEY "
                            "(BYPASSRLS) outside backtest. Mint a trading_bot_node JWT, "
                            "set SUPABASE_BOT_KEY, and remove the service key "
                            "(CP7 F3 / Gate A G-A3)."
                        ),
                    )
                _LEGACY_SERVICE_ROLE_WARNED = True
            return legacy, "service_role_legacy"
        return "", ""
    legacy = resolve_service_role_key()
    if legacy:
        return legacy, "service_role"
    return resolve_trading_bot_key(), "trading_bot_node"


@lru_cache(maxsize=1)
def get_supabase():
    """Return a bot-scoped Supabase client or None if not configured."""
    url = _normalize_supabase_url(os.getenv("SUPABASE_URL", ""))
    key, channel = resolve_supabase_api_key(prefer_bot=True)
    if not url or not key:
        return None
    try:
        from supabase import create_client

        client = create_client(url, key)
        log.info("supabase_connected", auth_channel=channel)
        return client
    except Exception as e:
        log.error("supabase_connect_failed", error=str(e))
        return None


def run_with_supabase_retry(op, *, label: str, attempts: int = 3):
    """Bug-2 (shape 1): run a Supabase op with bounded retry + client rebuild.

    ``get_supabase()`` is ``@lru_cache(maxsize=1)`` -- one long-lived client. Supabase/
    Cloudflare recycle idle HTTP/2 connections with a GOAWAY frame, surfacing as an error
    whose ``str(e)`` contains ``ConnectionTerminated`` on our LOW-VOLUME control-plane calls
    (bot_runs / heartbeat / commands / config). The cached client keeps handing out the dead
    connection, so a single retry never recovers. This helper: fetches the cached client,
    runs ``op(client)``, and on a ``ConnectionTerminated`` error clears the cache (forcing a
    fresh client next iteration) and retries with jittered backoff, up to ``attempts`` times.

    Bounded, NEVER infinite. On the last attempt or a non-terminal exception it logs a
    warning and returns ``None`` -- it NEVER raises, because every caller already catches and
    degrades. ``op`` is a callable taking the client and returning its result.
    """
    for attempt in range(attempts):
        client = get_supabase()
        if client is None:
            return None
        try:
            return op(client)
        except Exception as e:  # noqa: BLE001 -- callers already degrade; never raise
            terminal = "ConnectionTerminated" in str(e)
            last = attempt == attempts - 1
            if terminal and not last:
                # Drop the dead cached client so the next iteration rebuilds it.
                get_supabase.cache_clear()
                time.sleep(min(0.2 * 2**attempt, 2.0) + random.random() * 0.1)
                continue
            log.warning(
                "supabase_op_failed",
                label=label,
                attempt=attempt + 1,
                terminal=terminal,
                error=str(e),
            )
            return None
    return None


def is_configured() -> bool:
    """True when Supabase URL and a bot or legacy service key are present."""
    return bool(os.getenv("SUPABASE_URL")) and bool(
        resolve_trading_bot_key() or resolve_service_role_key()
    )
