"""Authoritative leg-enablement gate (K4).

The soak's commissioning allowlist is evaluated HERE at leg-load time and is
authoritative over EVERY enablement source -- local yaml, ConfigWatcher-fetched
Supabase rows, and any future push. This closes the ConfigWatcher/Supabase finding:

- **Commissioning (soak.enabled AND environment==paper AND id in allowlist):** the
  leg force-loads from the LOCAL charter config, overriding a Supabase
  `enabled=false`. Source COMMISSIONING_GATE.
- **Any other leg during the soak:** does NOT load -- deterministically, whether
  Supabase is reachable, unreachable, or flaking mid-fetch (a network blip must not
  resurrect the dead legs whose local yaml still says enabled:true). Source
  FAIL_CLOSED. This also means a Supabase row cannot load a non-allowlisted leg the
  gate rejects.
- **Outside the soak:** legacy behaviour -- SUPABASE_ENABLEMENT when the fetch was
  authoritative, else LOCAL_DEFAULT.

`resolve_active_legs` is a pure function so the gate matrix is unit-tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import SoakConfig, StrategyConfig

# Enablement-attribution sources (K4 item C: one loud boot log line per leg).
SOURCE_COMMISSIONING = "COMMISSIONING_GATE"
SOURCE_SUPABASE = "SUPABASE_ENABLEMENT"
SOURCE_LOCAL_DEFAULT = "LOCAL_DEFAULT"
SOURCE_FAIL_CLOSED = "FAIL_CLOSED"


@dataclass(frozen=True)
class LegLoadDecision:
    strategy_id: str
    loaded: bool
    source: str
    commissioning: bool
    config: StrategyConfig | None


def resolve_active_legs(
    *,
    environment: str,
    soak: SoakConfig,
    local_strategies: list[StrategyConfig],
    watcher_strategies: list[StrategyConfig],
    supabase_authoritative: bool,
) -> list[LegLoadDecision]:
    """Return an ordered load decision per known leg. Authoritative over all sources.

    `local_strategies` are the fixed charter configs (config/strategies/*.yaml);
    `watcher_strategies` are whatever ConfigWatcher resolved (Supabase or fallback);
    `supabase_authoritative` is True only when the watcher's enablement came from a
    successful Supabase fetch (used only OUTSIDE the soak)."""
    local_by_id = {s.strategy_id: s for s in local_strategies}
    watcher_by_id = {s.strategy_id: s for s in watcher_strategies}
    commissioning_active = bool(soak.enabled) and environment == "paper"
    allowlist = set(soak.commissioning_legs) if commissioning_active else set()

    decisions: list[LegLoadDecision] = []
    for sid in sorted(set(local_by_id) | set(watcher_by_id)):
        if sid in allowlist and sid in local_by_id:
            # Force-load the LOCAL charter config; authoritative over Supabase.
            decisions.append(LegLoadDecision(
                sid, True, SOURCE_COMMISSIONING, True, local_by_id[sid]))
        elif soak.enabled:
            # Non-commissioning leg during the soak: deterministic no-load
            # (fail-closed) -- Supabase state is irrelevant, so boot is identical
            # whether Supabase is reachable, unreachable, or flaking.
            decisions.append(LegLoadDecision(
                sid, False, SOURCE_FAIL_CLOSED, False, None))
        else:
            # Outside the soak: legacy enablement from the resolved (watcher) config;
            # the source label reflects whether that resolution was Supabase-backed.
            ws = watcher_by_id.get(sid)
            loaded = ws is not None and ws.enabled
            src = SOURCE_SUPABASE if supabase_authoritative else SOURCE_LOCAL_DEFAULT
            decisions.append(LegLoadDecision(
                sid, loaded, src, False, ws if loaded else None))
    return decisions
