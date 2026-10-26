"""Publish-side enforcement: the sensitive doc trees must NEVER reach the public showcase.

Same shape as tests/test_mirror_gated_exclusion.py (the sip_gated wall): it converts
"excluded because no include glob happened to catch it" into "excluded by enforcement."
It drives the REAL selector (tools/publish/select_public.py — the same code path publish.sh
runs), so a future edit that adds a broad include, or drops a fence line, fails here.

Fenced trees: docs/audit/** and docs/audits/** (audit findings / exploit enumeration) and
docs/edge-research/** (objective function, cost structure, deployable capital, the intake
graveyard reasoning, the hypothesis queue).
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_ALLOWLIST = _REPO / "tools" / "publish" / "public.allowlist"

_SPEC = importlib.util.spec_from_file_location(
    "select_public", _REPO / "tools" / "publish" / "select_public.py")
select_public = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(select_public)

# The trees that must never be path-selected into the public tree.
FENCED_PREFIXES = ("docs/audit/", "docs/audits/", "docs/edge-research/")
# The exact exclude lines that must be present in the allowlist (enforced, not incidental).
REQUIRED_FENCE_LINES = (
    "!docs/audit/**", "!docs/audits/**", "!docs/edge-research/**", "!docs/*AUDIT*", "!docs/*audit*",
    "!docs/PAD_FIX_RECONCILIATION*",
    # Recovered hypothesis-registry snapshot at docs/ ROOT — excluded by enforcement, not circumstance.
    "!docs/edge_hypothesis_registry*", "!docs/**/edge_hypothesis_registry*",
    # In-flight quarantine/burn-check records + search-findings ledger — default-deny-only, no
    # content-quarantine backstop; enforced-deny here.
    "!docs/quarantine/**", "!docs/search/**",
    # Repo-ROOT audit fence — every other audit fence is docs-scoped (asymmetry from the sweep).
    "!*AUDIT*", "!*audit*")
# docs-ROOT audit docs (PAD_AUDIT, RISK_AUDIT, ENABLEMENT_AUTHORITY_AUDIT, risk_governor_input_audit)
# live outside docs/audits/ and must be path-fenced, not left to content-quarantine.
_ROOT_AUDIT_RE = re.compile(r"docs/[^/]*audit", re.IGNORECASE)
# Third excluded-by-circumstance class: hypothesis/registry/ledger/verdict/burn/quarantine docs at
# docs/ ROOT (e.g. edge_hypothesis_registry_RECOVERED.md) were surviving on default-deny, not a fence.
_ROOT_SENSITIVE_RE = re.compile(
    r"docs/[^/]*(registry|hypothesis|ledger|verdict|burn|quarantine)", re.IGNORECASE)


def test_fence_lines_are_explicit_excludes_in_the_allowlist():
    lines = {ln.strip() for ln in _ALLOWLIST.read_text(encoding="utf-8").splitlines()}
    for required in REQUIRED_FENCE_LINES:
        assert required in lines, f"missing enforced-deny fence line: {required}"


def test_no_fenced_doc_reaches_the_public_set():
    # Drive the REAL selector against the REAL repo + allowlist.
    selected = select_public.selected_from_repo(str(_REPO), str(_ALLOWLIST))
    leaked = [p for p in selected if p.startswith(FENCED_PREFIXES)]
    assert not leaked, f"fenced docs reached the public set: {leaked[:10]}"


def test_no_docs_root_audit_doc_reaches_the_public_set():
    # PAD_AUDIT / RISK_AUDIT etc. were path-selected and saved only by content-quarantine;
    # now they must be excluded by PATH. Covers both AUDIT and audit casings.
    selected = select_public.selected_from_repo(str(_REPO), str(_ALLOWLIST))
    leaked = [p for p in selected if _ROOT_AUDIT_RE.match(p)]
    assert not leaked, f"docs-root audit docs reached the public set: {leaked[:10]}"


def test_no_docs_root_sensitive_registry_doc_reaches_the_public_set():
    # Companion to test_no_docs_root_audit_doc_reaches_the_public_set. edge_hypothesis_registry_
    # RECOVERED.md sat at docs/ ROOT and stayed out only because no include happened to select it
    # (default-deny), NOT by enforcement — the third such doc, each caught by a different accident.
    # Guard the CLASS (registry/hypothesis/ledger/verdict/burn/quarantine) by NAME, and drive a
    # canary that does not exist yet through the REAL include/exclude rules so the guard covers the
    # class, not just today's instances: if a future include ever selects such a doc, this fails.
    import subprocess

    tracked = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    canaries = [
        "docs/edge_hypothesis_registry_CANARY_does_not_exist_yet.md",
        "docs/H99_burn_ledger_canary_does_not_exist.md",
        "docs/new_hypothesis_verdict_canary.md",
    ]
    inc, exc = select_public.parse_allowlist(str(_ALLOWLIST))
    selected = select_public.select(tracked + canaries, inc, exc)
    leaked = [p for p in selected if _ROOT_SENSITIVE_RE.match(p)]
    assert not leaked, f"docs-root sensitive registry/hypothesis/ledger docs are selectable: {leaked[:10]}"


def test_pad_ruling_fix_reconciliation_fenced_amendment_drafts_public():
    # Operator ruling 2026-07-30. The FIX_RECONCILIATION doc is filename-keyed-invisible
    # (no "audit" in its name) and discloses a live-posture leak (PB-1) -> explicit path fence.
    selected = select_public.selected_from_repo(str(_REPO), str(_ALLOWLIST))
    assert not [p for p in selected if "PAD_FIX_RECONCILIATION" in p], "FIX_RECONCILIATION must be fenced"
    # AMENDMENT_DRAFTS is forward-looking methodology -> stays public.
    assert any("PAD_AMENDMENT_DRAFTS" in p for p in selected), "AMENDMENT_DRAFTS should remain public"


def test_exclude_wins_even_against_a_broad_include():
    # Defence against a future over-broad include (e.g. someone adds `docs/**`): the fence
    # excludes must still win, because selected = matches-include AND matches-NO-exclude.
    inc = [select_public.glob_to_re("docs/**")]  # deliberately over-broad hypothetical include
    _, exc = select_public.parse_allowlist(str(_ALLOWLIST))
    tracked = [
        "docs/edge-research/research_OBJECTIVE_FUNCTION.md",
        "docs/audits/H19_TAXLOSS_INTAKE_DISPOSITION_20260730.md",
        "docs/audit/control_plane/CP7_findings.md",
        "src/engine/ok.py",
        "docs/strategy_factory_doctrine.md",  # a non-fenced doc: allowed through the broad include
    ]
    selected = select_public.select(tracked, inc, exc)
    assert not [p for p in selected if p.startswith(FENCED_PREFIXES)]
    # positive lock: the non-fenced paths under the broad include still pass
    assert "docs/strategy_factory_doctrine.md" in selected
    assert "src/engine/ok.py" not in selected  # not matched by the docs/** include -> not selected
