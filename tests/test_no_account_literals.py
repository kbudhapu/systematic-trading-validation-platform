"""Source-lint: no account-balance-shaped literals near money keys in published source.

Closes the CLASS behind the 2026-09-06 derived-value finding: src/engine/portfolio_risk_governor.py
comments carried the live account equity (<redacted-balance>) and its rounded/derived forms (<redacted-balance>) plus
per-leg peak equities — and the literal scrub loses to every future rounding. This is a COMMIT-TIME
gate rather than a publish-time one: forbidden.tokens would QUARANTINE the whole file at publish,
but portfolio_risk_governor.py is machine we want public, so the fix is to keep the literals out of
source and enforce it here.

Rule: a numeric literal that is BALANCE-SHAPED — integer part >=5 digits (>=10,000, `_` separators
honoured so 102_395.31 counts as six) AND a significant fractional part (>=2 fractional digits after
trailing zeros are dropped, i.e. cents-or-finer precision) — appearing within PROXIMITY chars of a
money key (equity / buying_power / regt / cash / peak_equity / portfolio_value, case-insensitive) on
the same line, in tracked files under src/**, dashboard/**, tests/**.

Two anchors, both deliberate and both narrower than a bare digit count:
  * INTEGER-part anchor + `_`-awareness: a real broker equity reading has >=5 integer digits; and the
    live leak (102_395.31, 71_840.769..., 52_011.361...) is written with Python `_` separators that a
    bare \\d{5,} splits apart and a literal scrub cannot match. This is why comments+scrub alone did
    not close the class.
  * PRECISION anchor: round synthetic seeds (100_000.0, 99_750.0, 90_000.0 — the whole test suite's
    starting capital) are NOT balances and must not fire, or the opt-out list swallows the suite.
    A real reading carries cents+ (102_395.31, 51_197.655); a high-precision drawdown FRACTION
    (0.688123754736421) has a 1-digit integer part and is excluded by the first anchor.

A SECOND tier (operator-added 2026-09-06) hard-matches the known real account integers. Normalize a
literal by stripping `_` and `,`, take the integer part, and flag it if it equals one of the seven
values the live account has actually shown. The precision tier alone MISSES truncated forms like
`<redacted-balance>` — the exact rounded form that once survived scrub.map — because its one significant
fractional digit fails the cents test; the hard-match tier catches it regardless of decimals and is
what actually closes the class. After the source de-identification it should find nothing; it stays
as a permanent regression guard against any of these integers reappearing near a money key.

Genuine synthetic fixtures that still look real-shaped (e.g. a made-up 50123.45) opt out EXPLICITLY
in _ALLOW, each with a reason. Real account balances are NEVER opted out — remove them at source.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TREES = ("src/", "dashboard/", "tests/")
_KEYS = re.compile(r"(equity|buying_power|regt|cash|peak_equity|portfolio_value)", re.I)
# A number not preceded by a word-char or dot (so we anchor on the integer part, never a fraction's
# trailing digits), allowing `_` digit separators and `,` thousands groups; normalization strips both.
_NUM = re.compile(r"(?<![\w.])\d[\d_]*(?:,\d{3})*(?:\.\d+)?")
_PROXIMITY = 40  # chars between the money key and the literal, same line

# Tier 2: the exact integer parts the live account has shown (equity / buying_power / RegT / per-leg
# peaks / fossils). Normalized (no `_`/`,`), decimals ignored — catches truncated forms like <redacted-balance>.
_HARD_MATCH = {"102395", "409581", "204790", "71840", "52011", "76773", "51197"}


def _int_part(literal: str) -> str:
    return literal.partition(".")[0].replace("_", "").replace(",", "")


def _is_balance_shaped(literal: str) -> bool:
    """Tier 1 (heuristic): >=5 integer digits AND >=2 significant fractional digits (cents-or-finer)."""
    if len(_int_part(literal)) < 5:
        return False
    frac = literal.partition(".")[2]
    return len(frac.replace("_", "").rstrip("0")) >= 2


def _is_flagged(literal: str) -> bool:
    return _int_part(literal) in _HARD_MATCH or _is_balance_shaped(literal)


# Genuine-fixture opt-outs, as "relpath:literal". SEEDED EMPTY, then triaged 2026-09-06 to the ONE
# verified synthetic-but-real-shaped fixture in a file that publishes. Round synthetic seeds
# (100_000.0, 50_000.0, ...) do not reach here — the precision anchor already excludes them. Real
# account balances are NEVER opted out — they are removed at source (or their file is fenced).
_ALLOW: set[str] = {
    # Coverage would be lost without it: this made-up $50,123.45 is the money value the ntfy security
    # fence test feeds in and then asserts is STRIPPED from the public payload. Synthetic, not a real
    # balance; the value IS the test's subject, so it cannot be rounded away.
    "tests/test_ntfy_notifier.py:50123.45",
}


def _tracked_sources() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [p for p in out if p.startswith(_TREES)]


def test_no_account_balance_literals_near_money_keys():
    self_rel = str(Path(__file__).relative_to(_REPO)).replace("\\", "/")
    offenders: list[str] = []
    for rel in _tracked_sources():
        if rel == self_rel:
            continue
        try:
            text = (_REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            keys = [m.start() for m in _KEYS.finditer(line)]
            if not keys:
                continue
            for nm in _NUM.finditer(line):
                if not _is_flagged(nm.group()):
                    continue
                if any(abs(nm.start() - k) <= _PROXIMITY for k in keys):
                    tag = f"{rel}:{nm.group()}"
                    if tag in _ALLOW:
                        continue
                    offenders.append(f"{rel}:{i}: '{nm.group()}' near a money key")
    assert not offenders, (
        "account-balance-shaped literals near money keys (move them out of source, or opt-out a "
        "verified fixture in _ALLOW):\n" + "\n".join(sorted(set(offenders)))
    )
