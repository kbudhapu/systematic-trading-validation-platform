"""Static RLS least-privilege regression (CP1/CP7 F1/F2, migration 015).

Parses the CUMULATIVE effect of every supabase/migrations/*.sql (CREATE/DROP POLICY,
in file+statement order) and asserts the least-privilege invariants hold in the final
deployed state:

  (a) No surviving policy grants a WRITE (INSERT/UPDATE/DELETE, or the catch-all ALL)
      to `anon` or `authenticated` on ANY table. The browser roles are read-only.
  (b) control_commands is directly writable ONLY by `trading_bot_node` (+ `service_role`);
      the dashboard enqueues via the 016 SECURITY DEFINER RPC, which is a function owner's
      privilege, NOT a policy — so it is (correctly) invisible to this policy parse.

CI-permanent: if a future migration re-opens a browser write, this fails.
"""
from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "supabase" / "migrations"

WRITE_CMDS = {"INSERT", "UPDATE", "DELETE", "ALL"}
BROWSER_ROLES = {"anon", "authenticated"}
# control_commands direct writers allowed after least-privilege: the bot (trading_bot_node),
# service_role (BYPASSRLS), and the 016 SECURITY DEFINER RPC owner (command_enqueue_owner),
# which the dashboard reaches via EXECUTE only — never a direct browser-role write.
CONTROL_COMMANDS_WRITERS_ALLOWED = {
    "trading_bot_node",
    "service_role",
    "command_enqueue_owner",
}

_CREATE_RE = re.compile(
    r'CREATE\s+POLICY\s+"(?P<name>[^"]+)"\s+ON\s+(?P<table>\w+)(?P<body>.*?);',
    re.DOTALL | re.IGNORECASE,
)
_DROP_RE = re.compile(
    r'DROP\s+POLICY\s+(?:IF\s+EXISTS\s+)?"(?P<name>[^"]+)"\s+ON\s+(?P<table>\w+)',
    re.IGNORECASE,
)
_FOR_RE = re.compile(r'\bFOR\s+(SELECT|INSERT|UPDATE|DELETE|ALL)\b', re.IGNORECASE)
_TO_RE = re.compile(r'\bTO\s+(?P<roles>[\w\s,]+?)\s*(?:USING|WITH\s+CHECK|;|$)', re.IGNORECASE)


def _parse_roles(body: str) -> set[str]:
    m = _TO_RE.search(body)
    if not m:
        return set()
    return {r.strip() for r in m.group("roles").split(",") if r.strip()}


def _parse_cmd(body: str) -> str:
    m = _FOR_RE.search(body)
    return m.group(1).upper() if m else "ALL"  # no FOR clause ⇒ ALL


def _final_policy_state() -> dict[tuple[str, str], dict]:
    """Apply CREATE/DROP POLICY across all migrations in order → surviving policies,
    keyed by (table, policyname)."""
    state: dict[tuple[str, str], dict] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        events: list[tuple[int, str, dict]] = []
        for m in _CREATE_RE.finditer(text):
            events.append(
                (m.start(), "create", {
                    "name": m.group("name"),
                    "table": m.group("table"),
                    "cmd": _parse_cmd(m.group("body")),
                    "roles": _parse_roles(m.group("body")),
                    "migration": path.name,
                })
            )
        for m in _DROP_RE.finditer(text):
            events.append((m.start(), "drop", {"name": m.group("name"), "table": m.group("table")}))
        for _, kind, data in sorted(events, key=lambda e: e[0]):
            key = (data["table"], data["name"])
            if kind == "create":
                state[key] = data
            else:
                state.pop(key, None)
    return state


def test_migrations_dir_present_and_015_exists():
    assert MIGRATIONS_DIR.is_dir(), MIGRATIONS_DIR
    assert (MIGRATIONS_DIR / "015_least_privilege.sql").exists()


def test_no_browser_role_write_policy_survives():
    """(a) anon/authenticated hold no INSERT/UPDATE/DELETE/ALL policy on any table."""
    offenders = []
    for (table, name), pol in _final_policy_state().items():
        if pol["cmd"] in WRITE_CMDS and (pol["roles"] & BROWSER_ROLES):
            offenders.append(f"{table}.{name} [{pol['cmd']} TO {sorted(pol['roles'])}] ({pol['migration']})")
    assert not offenders, (
        "Browser-role WRITE policies survive least-privilege (F2 regression):\n  "
        + "\n  ".join(offenders)
    )


def test_control_commands_direct_write_locked_to_bot():
    """(b) Only trading_bot_node (+ service_role) may directly write control_commands."""
    offenders = []
    for (table, name), pol in _final_policy_state().items():
        if table != "control_commands":
            continue
        if pol["cmd"] in WRITE_CMDS and not (pol["roles"] <= CONTROL_COMMANDS_WRITERS_ALLOWED):
            offenders.append(f"{name} [{pol['cmd']} TO {sorted(pol['roles'])}] ({pol['migration']})")
    assert not offenders, (
        "control_commands has an unexpected direct-write policy (F1 regression):\n  "
        + "\n  ".join(offenders)
    )


def test_authenticated_retains_select_on_core_tables():
    """Positive control: least-privilege is SELECT-only, not no-access — the dashboard
    must still read. Assert a SELECT policy for authenticated survives on key tables."""
    state = _final_policy_state()
    for table in ("equity_snapshots", "trades", "risk_state", "control_commands", "strategies"):
        has_select = any(
            t == table and pol["cmd"] in {"SELECT", "ALL"} and ("authenticated" in pol["roles"])
            for (t, _), pol in state.items()
        )
        assert has_select, f"authenticated lost SELECT on {table} (over-revoked)"


# ── Migration 019: strategy-write column privileges ───────────────────────────
# A strategy `params` write must never be grantable to a non-service_role role (it has to pass
# ConfigurationParityAuditor via a DEFINER RPC, CP7 G-C2). trading_bot_node gets UPDATE on exactly
# {enabled, environment, updated_at} — the columns its flatten-disable + go-live writers touch.
ALL_COLS = "*"
_GRANT_RE = re.compile(
    r'GRANT\s+(?P<privs>.+?)\s+ON\s+(?P<table>\w+)\s+TO\s+(?P<roles>[\w\s,]+?)\s*;', re.IGNORECASE)
_REVOKE_RE = re.compile(
    r'REVOKE\s+(?P<privs>.+?)\s+ON\s+(?P<table>\w+)\s+FROM\s+(?P<roles>[\w\s,]+?)\s*;', re.IGNORECASE)
_UPDATE_COLS_RE = re.compile(r'\bUPDATE\b\s*(?:\(([^)]*)\))?', re.IGNORECASE)


def _update_columns(privs: str):
    """UPDATE column set from a GRANT/REVOKE privilege clause: None if UPDATE absent, ALL_COLS if
    UPDATE with no column list, else the explicit column set."""
    m = _UPDATE_COLS_RE.search(privs)
    if not m:
        return None
    if m.group(1) is not None:
        return {c.strip() for c in m.group(1).split(",") if c.strip()}
    return ALL_COLS


def _strategies_update_grants() -> dict[str, object]:
    """Cumulative per-role UPDATE column privilege on `strategies` across all migrations."""
    state: dict[str, object] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        events: list[tuple[int, str, object, str]] = []
        for m in _GRANT_RE.finditer(text):
            if m.group("table") != "strategies":
                continue
            cols = _update_columns(m.group("privs"))
            if cols is not None:
                events.append((m.start(), "grant", cols, m.group("roles")))
        for m in _REVOKE_RE.finditer(text):
            if m.group("table") != "strategies":
                continue
            if _update_columns(m.group("privs")) is not None:
                events.append((m.start(), "revoke", None, m.group("roles")))
        for _, kind, cols, roles in sorted(events, key=lambda e: e[0]):
            for role in (r.strip() for r in roles.split(",") if r.strip()):
                if kind == "grant":
                    state[role] = cols
                else:
                    state.pop(role, None)
    return state


def test_no_role_except_service_role_can_update_params():
    """(a) No role but service_role may UPDATE strategies.params (column-privilege gate)."""
    offenders = []
    for role, cols in _strategies_update_grants().items():
        if role == "service_role":
            continue
        if cols == ALL_COLS or (isinstance(cols, set) and "params" in cols):
            offenders.append(f"{role} → {cols}")
    assert not offenders, (
        "non-service_role holds an UPDATE grant covering strategies.params "
        "(parity bypass — must go via a DEFINER RPC):\n  " + "\n  ".join(offenders)
    )


def test_trading_bot_node_strategies_update_columns_are_exactly_three():
    """(b) trading_bot_node UPDATE on strategies is exactly {enabled, environment, updated_at}."""
    cols = _strategies_update_grants().get("trading_bot_node")
    assert cols == {"enabled", "environment", "updated_at"}, (
        f"trading_bot_node strategies UPDATE columns = {cols}, expected the three scoped columns"
    )
