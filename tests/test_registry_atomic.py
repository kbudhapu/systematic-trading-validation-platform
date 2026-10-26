"""T4.5 — atomic registration keeps the store and the committed view in lockstep.

The registry drifted because register_entry (store-only) and regenerate_view were never atomic,
and hand-edits to the .md never hit the store. register_and_regenerate closes the write side;
verify_registry_synced is the CI guard that catches any residual drift. Temp DB only — never
touches the production store.
"""
from __future__ import annotations

from src.persistence import registry_store as rs


def _entry(exp_id: str, ordinal: int, body: str = "body text") -> rs.Entry:
    md = f"## {exp_id} -- test entry\n\n{body}"
    return rs.Entry(exp_id=exp_id, kind="STANDING", title=f"{exp_id} -- test entry",
                    status="ACTIVE", verdict=None, registered_utc="2026-07-12",
                    content_md=md, ordinal=ordinal)


def test_atomic_register_keeps_view_and_store_in_sync(tmp_path):
    db = tmp_path / "rv.db"
    md = tmp_path / "REGISTRY.md"
    rs.register_and_regenerate(_entry("STANDING-A", 0), db, md_path=md)
    rs.register_and_regenerate(_entry("STANDING-B", 1), db, md_path=md)
    text = md.read_text(encoding="utf-8")
    assert "STANDING-A" in text and "STANDING-B" in text
    ok, msg = rs.verify_registry_synced(db, md_path=md)
    assert ok, msg


def test_verify_detects_bare_register_entry_drift(tmp_path):
    """A bare register_entry (the old drift-causing path) writes the store but not the view;
    the guard must flag it."""
    db = tmp_path / "rv.db"
    md = tmp_path / "REGISTRY.md"
    rs.register_and_regenerate(_entry("STANDING-A", 0), db, md_path=md)
    rs.register_entry(_entry("STANDING-C", 1), db)          # store-only -> drift
    ok, msg = rs.verify_registry_synced(db, md_path=md)
    assert not ok and "DRIFT" in msg


def test_verify_detects_hand_edit_drift(tmp_path):
    """A hand-edit to the .md that never hits the store must also be flagged."""
    db = tmp_path / "rv.db"
    md = tmp_path / "REGISTRY.md"
    rs.register_and_regenerate(_entry("STANDING-A", 0), db, md_path=md)
    md.write_text(md.read_text(encoding="utf-8") + "\n\n---\n\n## STANDING-HANDEDIT -- x\n\nsneaked in\n",
                  encoding="utf-8")
    ok, _ = rs.verify_registry_synced(db, md_path=md)
    assert not ok
