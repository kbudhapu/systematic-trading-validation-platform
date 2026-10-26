"""P2 backup & disaster-recovery: a backup is fiction until restored. Snapshots a
synthetic vault via the sqlite3 .backup API, restores it, and asserts row counts
match + a sample parquet reads. Plus retention pruning and the config gate."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import polars as pl
import pytest
import yaml

from src.config import CONFIG_DIR
from src.config.schema_check import SchemaValidationError, load_schema, validate_instance, validate_or_raise
from src.persistence.backup import (
    backup_vault, resolve_backup_root, restore_vault, run_scheduled_backup, verify_restore,
)


def _make_vault(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")   # backup must be WAL-safe
        conn.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO ledger (v) VALUES (?)", [(f"r{i}",) for i in range(rows)])


def _make_parquet(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"ts": [1, 2, 3], "close": [10.0, 11.0, 12.0]}).write_parquet(path)


def test_backup_then_restore_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "src"
    vault_a = src / "data" / "research_vault.db"
    vault_b = src / "data" / "trading.db"
    _make_vault(vault_a, 42)
    _make_vault(vault_b, 7)
    pq = src / "data" / "parquet" / "spy_2020.parquet"
    _make_parquet(pq)
    doc = src / "EXPERIMENT_REGISTRY.md"
    doc.write_text("# registry", encoding="utf-8")

    root = tmp_path / "backups"
    archive = backup_vault(backup_root=str(root), db_paths=[vault_a, vault_b],
                           parquet_dir=pq.parent, doc_files=[doc], doc_dirs=[])
    assert archive.exists() and (archive / "manifest.json").exists()

    # RESTORE DRILL: restore into a fresh dir and verify row counts match snapshot
    report = verify_restore(archive, tmp_path / "restored")
    print(f"\nrestore report: {report}")
    assert report["research_vault.db"]["matched"] is True
    assert report["research_vault.db"]["expected"]["ledger"] == 42
    assert report["trading.db"]["matched"] is True

    # sample parquet reads back
    restored_pq = tmp_path / "restored" / "parquet" / "spy_2020.parquet"
    assert restored_pq.exists()
    assert pl.read_parquet(restored_pq).height == 3


def test_backup_is_online_consistent_under_open_writer(tmp_path: Path) -> None:
    """The sqlite3 .backup API snapshots a live WAL DB without pausing writers."""
    src = tmp_path / "data" / "research_vault.db"
    _make_vault(src, 10)
    live = sqlite3.connect(src)   # keep a connection OPEN during the backup
    try:
        archive = backup_vault(backup_root=str(tmp_path / "bk"), db_paths=[src],
                               parquet_dir=tmp_path / "none", doc_files=[], doc_dirs=[])
        rep = verify_restore(archive, tmp_path / "r")
        assert rep["research_vault.db"]["matched"]
    finally:
        live.close()


def test_retention_prunes_to_keep(tmp_path: Path) -> None:
    from src.persistence.backup import _prune_old
    root = tmp_path / "bk"
    root.mkdir()
    # 6 dated archives (oldest first by name)
    for i in range(6):
        (root / f"mbappe_backup_2026010{i}_000000Z").mkdir()
    pruned = _prune_old(root, keep=3)
    remaining = sorted(p.name for p in root.glob("mbappe_backup_*"))
    assert len(remaining) == 3, f"retention must keep 3, found {len(remaining)}"
    assert remaining == ["mbappe_backup_20260103_000000Z", "mbappe_backup_20260104_000000Z",
                         "mbappe_backup_20260105_000000Z"], "newest 3 kept, oldest pruned"
    assert len(pruned) == 3


def test_resolve_backup_root_is_outside_repo(monkeypatch) -> None:
    monkeypatch.delenv("BACKUP_ROOT", raising=False)
    root = resolve_backup_root()
    assert "mbappe_backups" in str(root)
    monkeypatch.setenv("BACKUP_ROOT", "/external/cloud/path")
    assert str(resolve_backup_root()) == str(Path("/external/cloud/path"))


def test_run_scheduled_backup_off_by_default(tmp_path: Path) -> None:
    env = tmp_path / "env.yaml"
    env.write_text("backup:\n  enabled: false\n  backup_root: \"\"\n  keep: 8\n", encoding="utf-8")
    assert run_scheduled_backup(env_path=env) is None, "must no-op when disabled"


# ---- config parity for the backup block ----

def test_backup_block_matches_schema_and_off_by_default() -> None:
    with (CONFIG_DIR / "env.yaml").open(encoding="utf-8") as fh:
        block = yaml.safe_load(fh)["backup"]
    assert validate_instance(block, load_schema(CONFIG_DIR / "backup.schema.json")) == []
    assert block["enabled"] is False


def test_malformed_backup_block_rejected() -> None:
    with (CONFIG_DIR / "env.yaml").open(encoding="utf-8") as fh:
        block = dict(yaml.safe_load(fh)["backup"])
    schema = load_schema(CONFIG_DIR / "backup.schema.json")
    with pytest.raises(SchemaValidationError):
        validate_or_raise({**block, "keep": 0}, schema)     # minimum 1
    with pytest.raises(SchemaValidationError):
        validate_or_raise({**block, "bogus": 1}, schema)
