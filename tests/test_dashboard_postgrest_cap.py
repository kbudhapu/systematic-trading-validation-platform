"""Guard against the PostgREST 1000-row-cap bug class in the dashboard (#282, #282-sweep).

PostgREST enforces db-max-rows (1000 on this project) on EVERY response REGARDLESS of `.limit(N)`.
So a query `.order(col, { ascending: true }).limit(N>1000)` silently returns the OLDEST 1000 rows,
never the newest — freezing curves and freshness badges on stale data with no error. This bit the
Portfolio panel (26-day-stale, #282) and was one cap-crossing away on the leg-return page.

This test fails the build if any dashboard query combines `ascending: true` with a numeric
`.limit(N)` where N > 1000. The sanctioned patterns instead:
  - latest / freshness / as-of / headline  → dashboard-data.latestRow (desc + limit 1), or
    a direct `.order(col, { ascending: false }).limit(1)`.
  - time-series curve                       → dashboard-data.capSafeSeries (desc + limit + reverse).
Both order DESCending so the cap keeps the NEWEST rows.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_DIRS = [REPO / "dashboard" / "app", REPO / "dashboard" / "lib"]

_LIMIT_RE = re.compile(r"\.limit\(\s*(\d+)\s*\)")
_ASC_TRUE_RE = re.compile(r"ascending\s*:\s*true")
# how many lines back a `.limit()` may sit from its `.order(...ascending:true)` in one chain
_WINDOW = 12


def _iter_files():
    for base in SCAN_DIRS:
        if not base.exists():
            continue
        for ext in ("*.ts", "*.tsx"):
            for p in base.rglob(ext):
                if "node_modules" in p.parts or ".next" in p.parts:
                    continue
                yield p


def _violations_in(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _LIMIT_RE.search(line)
        if not m or int(m.group(1)) <= 1000:
            continue
        # a numeric limit > 1000 — is it part of an ascending chain? look back within the window.
        window = lines[max(0, i - _WINDOW) : i + 1]
        if any(_ASC_TRUE_RE.search(w) for w in window):
            hits.append((i + 1, line.strip()))
    return hits


def test_no_ascending_with_limit_over_1000():
    violations: list[str] = []
    scanned = 0
    for path in _iter_files():
        scanned += 1
        for lineno, snippet in _violations_in(path):
            rel = path.relative_to(REPO).as_posix()
            violations.append(f"{rel}:{lineno}  {snippet}")
    assert scanned > 0, "no dashboard .ts/.tsx files scanned — path wrong?"
    assert not violations, (
        "PostgREST cap bug class: `ascending: true` + `.limit(>1000)` returns the OLDEST 1000 rows "
        "(stale). Use dashboard-data.latestRow / capSafeSeries (DESC) instead:\n  "
        + "\n  ".join(violations)
    )


def test_capsafe_helpers_exist():
    """The sanctioned cap-safe helpers must remain available for pages to route through."""
    src = (REPO / "dashboard" / "lib" / "dashboard-data.ts").read_text(encoding="utf-8")
    assert "export async function latestRow" in src
    assert "export async function capSafeSeries" in src
