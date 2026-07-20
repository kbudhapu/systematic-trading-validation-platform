"""A4 — quarantine a corrupt registry entry: preserve as evidence, exclude from the view.

Append-only throughout: the corrupt row is never deleted or edited; a tombstone excludes it
from the render; the exact corrupt bytes are copied into registry_quarantine. Temp DB only.
"""
from __future__ import annotations

import sqlite3

from src.persistence import registry_store as rs


def _entry(exp_id, ordinal, body):
    return rs.Entry(exp_id=exp_id, kind="STANDING", title=f"{exp_id} -- x", status="ACTIVE",
                    verdict=None, registered_utc="2026-07-12", content_md=body, ordinal=ordinal)


def test_quarantine_preserves_evidence_and_excludes_from_view(tmp_path):
    db = tmp_path / "rv.db"
    md = tmp_path / "REG.md"
    corrupt = "## SLO-Z -- corrupt � header\n\nlocked criteria intact"
    rs.register_and_regenerate(_entry("STANDING-KEEP", 0, "## STANDING-KEEP -- keep\n\nbody"), db, md_path=md)
    rs.register_entry(_entry("SLO-Z", 1, corrupt), db)   # simulate the ad-hoc corrupt write

    res = rs.quarantine_registry_entry("SLO-Z", db, reason="corrupt duplicate; canonical governs",
                                       md_path=md)
    assert res["exp_id"] == "SLO-Z"

    # evidence preserved verbatim (corrupt bytes) in the quarantine store
    with sqlite3.connect(db) as c:
        q = c.execute("SELECT corrupt_content_md, reason FROM registry_quarantine").fetchone()
        assert q[0] == corrupt and "canonical" in q[1]
        # original corrupt row is NOT deleted (append-only)
        assert c.execute("SELECT COUNT(*) FROM experiment_registry WHERE exp_id='SLO-Z'").fetchone()[0] >= 2

    # the view no longer renders the quarantined entry, but keeps the good one
    view = md.read_text(encoding="utf-8")
    assert "SLO-Z" not in view and "STANDING-KEEP" in view
    ok, _ = rs.verify_registry_synced(db, md_path=md)
    assert ok
