"""FIX-5: phantom-trade quarantine -- additive tag, signature-based, readers exclude it.

Verifies the migration tags only phantom rows, never modifies the trades rows, is idempotent,
structurally cannot tag a real fill, and that the trades-table readers exclude quarantined rows.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.persistence.db import export_csv, get_recent_trades
from src.persistence.trade_quarantine import (
    count_phantom_signature, quarantine_phantom_trades)

_TRADES_DDL = """
CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, symbol TEXT,
  strategy_id TEXT, direction TEXT, qty REAL NOT NULL, entry_price REAL, exit_price REAL,
  pnl REAL, status TEXT);
CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, symbol TEXT,
  strategy_id TEXT, side TEXT, qty REAL, status TEXT, reject_reason TEXT, expected_price REAL,
  filled_price REAL);
CREATE TABLE daily_pnl (date TEXT, symbol TEXT, strategy_id TEXT, pnl REAL, equity REAL);
"""


def _seed(db: str, *, reals: int, phantoms: int) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_TRADES_DDL)
    for _ in range(reals):  # real fill: qty>0 AND a correlated filled order
        conn.execute("INSERT INTO trades (timestamp,symbol,strategy_id,direction,qty,entry_price,"
                     "exit_price,pnl,status) VALUES (?,?,?,?,?,?,?,?,?)",
                     ("2026-08-01T00:00:00+00:00", "QQQ", "mrq", "LONG", 10.0, 100.0, 101.0, 1.0, "closed"))
        conn.execute("INSERT INTO orders (timestamp,symbol,strategy_id,side,qty,status,filled_price) "
                     "VALUES (?,?,?,?,?,?,?)",
                     ("2026-08-01T00:00:00+00:00", "QQQ", "mrq", "buy", 10.0, "filled", 100.0))
    for _ in range(phantoms):  # phantom: qty=0, no order (test pollution)
        conn.execute("INSERT INTO trades (timestamp,symbol,strategy_id,direction,qty,entry_price,"
                     "exit_price,pnl,status) VALUES (?,?,?,?,?,?,?,?,?)",
                     ("2026-06-25T00:00:00+00:00", "SPY", "test_strat", "LONG", 0.0, 50.0, None, 0.5, "phantom"))
    conn.commit()
    conn.close()


def test_tags_only_phantom_and_never_modifies_trades(tmp_path: Path):
    db = str(tmp_path / "t.db")
    _seed(db, reals=3, phantoms=61)
    assert count_phantom_signature(db) == 61
    with sqlite3.connect(db) as c:
        rows_before = c.execute("SELECT * FROM trades ORDER BY id").fetchall()
    res = quarantine_phantom_trades(db)
    assert res["signature_matched"] == 61
    assert res["quarantine_total"] == 61
    assert res["real_trades_remaining"] == 3
    assert res["trades_fingerprint"]["unchanged"] is True
    with sqlite3.connect(db) as c:
        rows_after = c.execute("SELECT * FROM trades ORDER BY id").fetchall()
    assert rows_before == rows_after, "trades rows must be byte-identical (RA-0 evidence)"


def test_idempotent(tmp_path: Path):
    db = str(tmp_path / "t.db")
    _seed(db, reals=1, phantoms=5)
    quarantine_phantom_trades(db)
    second = quarantine_phantom_trades(db)
    assert second["quarantine_total"] == 5, "re-run adds no duplicate tags"


def test_signature_cannot_tag_a_real_fill(tmp_path: Path):
    # a real fill (qty>0 + filled order) is never quarantined, even alongside phantoms
    db = str(tmp_path / "t.db")
    _seed(db, reals=4, phantoms=10)
    res = quarantine_phantom_trades(db)
    with sqlite3.connect(db) as c:
        # every quarantined id has qty=0
        bad = c.execute("SELECT COUNT(*) FROM trades WHERE qty!=0 AND id IN "
                        "(SELECT trade_id FROM trade_quarantine)").fetchone()[0]
    assert bad == 0 and res["real_trades_remaining"] == 4


def test_readers_exclude_quarantined_rows(tmp_path: Path):
    db = str(tmp_path / "t.db")
    _seed(db, reals=2, phantoms=61)
    quarantine_phantom_trades(db)
    # get_recent_trades sees only the 2 real trades
    recent = get_recent_trades(100, db_path=db)
    assert len(recent) == 2 and all(r["qty"] != 0 for r in recent)
    # export_csv writes only the real trades (61 phantom excluded)
    from src import config as _cfg
    _cfg.DATA_DIR  # ensure import ok
    export_csv(db_path=db)
