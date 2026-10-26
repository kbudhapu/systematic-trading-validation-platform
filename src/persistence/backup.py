#!/usr/bin/env python3
"""Vault backup & disaster recovery (final-prep P2).

One entrypoint snapshots, into a dated archive under a configurable BACKUP_ROOT
(default OUTSIDE the repo, .env-configurable):
  (a) all SQLite vaults via the sqlite3 .backup API -- safe under WAL, no writer
      pause (a consistent online snapshot);
  (b) the SIP decade parquet cache (files + a manifest);
  (c) git-tracked docs for a self-contained restore (EXPERIMENT_REGISTRY.md,
      OVERNIGHT_QUEUE_LOG.md, docs/).
Retention keeps the last N archives (default 8) and prunes older ones.

A backup is fiction until restored: `restore_vault` + the automated restore test
prove a snapshot round-trips (row counts + a sample parquet read).

Usage:
  python scripts/backup_vault.py                       # run a backup
  python scripts/backup_vault.py --backup-root DIR --keep 8
  python scripts/backup_vault.py --restore ARCHIVE --into DIR
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # src/persistence/backup.py -> repo root
from src.config import DATA_DIR  # single data root (env-aware; test-suite redirected)

DEFAULT_DBS = [DATA_DIR / "research_vault.db", DATA_DIR / "trading.db"]
DEFAULT_PARQUET_DIR = DATA_DIR / "parquet"
DEFAULT_DOC_FILES = [REPO_ROOT / "EXPERIMENT_REGISTRY.md", REPO_ROOT / "OVERNIGHT_QUEUE_LOG.md"]
DEFAULT_DOC_DIRS = [REPO_ROOT / "docs"]


def resolve_backup_root(explicit: str | None = None) -> Path:
    """BACKUP_ROOT precedence: explicit arg > $BACKUP_ROOT > ~/mbappe_backups
    (deliberately OUTSIDE the repo so backups are never swept into git or lost with
    the working tree). Point $BACKUP_ROOT at external/cloud storage in .env."""
    root = explicit or os.environ.get("BACKUP_ROOT") or str(Path.home() / "mbappe_backups")
    return Path(root)


def _table_row_counts(db_path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    with sqlite3.connect(db_path) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in tables:
            try:
                counts[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            except sqlite3.Error:
                counts[t] = -1
    return counts


def _online_backup(src_db: Path, dst_db: Path) -> dict[str, int]:
    """Consistent online copy via sqlite3 .backup (WAL-safe, no writer pause)."""
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(src_db)
    dst = sqlite3.connect(dst_db)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return _table_row_counts(dst_db)


def backup_vault(
    *,
    backup_root: str | None = None,
    keep: int = 8,
    db_paths: list[Path] | None = None,
    parquet_dir: Path | None = None,
    doc_files: list[Path] | None = None,
    doc_dirs: list[Path] | None = None,
) -> Path:
    """Create a dated archive and prune to the last `keep`. Returns the archive dir."""
    root = resolve_backup_root(backup_root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    archive = root / f"mbappe_backup_{stamp}"
    (archive / "db").mkdir(parents=True, exist_ok=True)
    (archive / "parquet").mkdir(parents=True, exist_ok=True)
    (archive / "docs").mkdir(parents=True, exist_ok=True)

    manifest: dict = {"created_utc": datetime.now(timezone.utc).isoformat(),
                      "dbs": {}, "parquet": [], "docs": []}

    for db in (db_paths if db_paths is not None else DEFAULT_DBS):
        db = Path(db)
        if not db.exists():
            continue
        counts = _online_backup(db, archive / "db" / db.name)
        manifest["dbs"][db.name] = {"row_counts": counts, "source": str(db)}

    pq_dir = parquet_dir if parquet_dir is not None else DEFAULT_PARQUET_DIR
    if pq_dir.exists():
        for pq in sorted(pq_dir.glob("*.parquet")):
            shutil.copy2(pq, archive / "parquet" / pq.name)
            manifest["parquet"].append(pq.name)

    for f in (doc_files if doc_files is not None else DEFAULT_DOC_FILES):
        f = Path(f)
        if f.exists():
            shutil.copy2(f, archive / "docs" / f.name)
            manifest["docs"].append(f.name)
    for d in (doc_dirs if doc_dirs is not None else DEFAULT_DOC_DIRS):
        d = Path(d)
        if d.exists():
            shutil.copytree(d, archive / "docs" / d.name, dirs_exist_ok=True)
            manifest["docs"].append(f"{d.name}/")

    (archive / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _prune_old(root, keep)
    return archive


def _prune_old(root: Path, keep: int) -> list[Path]:
    archives = sorted(root.glob("mbappe_backup_*"), key=lambda p: p.name)
    pruned: list[Path] = []
    while len(archives) > max(keep, 0):
        victim = archives.pop(0)
        shutil.rmtree(victim, ignore_errors=True)
        pruned.append(victim)
    return pruned


def restore_vault(archive: str | Path, into: str | Path) -> dict:
    """Restore an archive's contents into `into`; returns the restored manifest."""
    archive = Path(archive)
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    for sub in ("db", "parquet", "docs"):
        src = archive / sub
        if src.exists():
            shutil.copytree(src, into / sub, dirs_exist_ok=True)
    manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    return manifest


def verify_restore(archive: str | Path, into: str | Path) -> dict:
    """Restore + assert each DB's row counts match the manifest snapshot. Returns a
    per-db {matched: bool} report (the restore drill)."""
    manifest = restore_vault(archive, into)
    report: dict[str, dict] = {}
    for name, meta in manifest.get("dbs", {}).items():
        restored = Path(into) / "db" / name
        counts = _table_row_counts(restored) if restored.exists() else {}
        report[name] = {"matched": counts == meta["row_counts"],
                        "expected": meta["row_counts"], "actual": counts}
    return report


def run_scheduled_backup(*, env_path: Path | None = None) -> Path | None:
    """Maintenance-hook entrypoint: run a backup only if `backup.enabled` is true in
    config/env.yaml. OFF by default -- returns None when disabled."""
    import yaml
    env_path = env_path or (REPO_ROOT / "config" / "env.yaml")
    with env_path.open(encoding="utf-8") as fh:
        cfg = (yaml.safe_load(fh) or {}).get("backup", {})
    if not cfg.get("enabled", False):
        return None
    return backup_vault(backup_root=cfg.get("backup_root") or None, keep=int(cfg.get("keep", 8)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="mbappe vault backup / restore")
    ap.add_argument("--backup-root", default=None)
    ap.add_argument("--keep", type=int, default=8)
    ap.add_argument("--restore", default=None, help="archive dir to restore")
    ap.add_argument("--into", default=None, help="restore target dir")
    args = ap.parse_args(argv)
    if args.restore:
        if not args.into:
            ap.error("--restore requires --into")
        report = verify_restore(args.restore, args.into)
        print(json.dumps(report, indent=2))
        return 0
    archive = backup_vault(backup_root=args.backup_root, keep=args.keep)
    print(f"backup complete: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
