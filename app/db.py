"""SQLite storage. Amounts are stored as integer paise to avoid float rounding."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sheets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_date TEXT,
    image_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',      -- draft | confirmed
    parse_error TEXT,
    raw_json TEXT,                               -- what the parser returned, for audit
    opening_balance_paise INTEGER,
    closing_balance_paise INTEGER,
    written_total_in_paise INTEGER,
    written_total_out_paise INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    entry_date TEXT,
    description TEXT NOT NULL,
    party TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('in', 'out')),
    amount_paise INTEGER NOT NULL,
    category TEXT,
    confidence TEXT
);

CREATE INDEX IF NOT EXISTS idx_entries_sheet ON entries(sheet_id);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(entry_date);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_paise(value) -> int | None:
    if value is None or value == "":
        return None
    d = Decimal(str(value).replace(",", "").strip())
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_paise(value: int | None) -> Decimal | None:
    if value is None:
        return None
    return (Decimal(value) / 100).quantize(Decimal("0.01"))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    config.ensure_dirs()
    with session() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def create_sheet(conn, image_path: str) -> int:
    cur = conn.execute(
        "INSERT INTO sheets (image_path, created_at) VALUES (?, ?)",
        (image_path, now_iso()),
    )
    return cur.lastrowid


def update_sheet_meta(conn, sheet_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE sheets SET {cols} WHERE id = ?", (*fields.values(), sheet_id))


def replace_entries(conn, sheet_id: int, entries: list[dict]) -> None:
    conn.execute("DELETE FROM entries WHERE sheet_id = ?", (sheet_id,))
    conn.executemany(
        """INSERT INTO entries
           (sheet_id, position, entry_date, description, party, kind, amount_paise, category, confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                sheet_id,
                i,
                e.get("entry_date"),
                e["description"],
                e.get("party"),
                e["kind"],
                e["amount_paise"],
                e.get("category"),
                e.get("confidence"),
            )
            for i, e in enumerate(entries)
        ],
    )


def get_sheet(conn, sheet_id: int):
    return conn.execute("SELECT * FROM sheets WHERE id = ?", (sheet_id,)).fetchone()


def get_entries(conn, sheet_id: int):
    return conn.execute(
        "SELECT * FROM entries WHERE sheet_id = ? ORDER BY position", (sheet_id,)
    ).fetchall()


def list_sheets(conn, limit: int = 100):
    return conn.execute(
        """SELECT s.*,
                  COUNT(e.id) AS entry_count,
                  COALESCE(SUM(CASE WHEN e.kind='in' THEN e.amount_paise END), 0) AS total_in,
                  COALESCE(SUM(CASE WHEN e.kind='out' THEN e.amount_paise END), 0) AS total_out
           FROM sheets s LEFT JOIN entries e ON e.sheet_id = s.id
           GROUP BY s.id
           ORDER BY COALESCE(s.sheet_date, s.created_at) DESC, s.id DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def delete_sheet(conn, sheet_id: int):
    row = get_sheet(conn, sheet_id)
    conn.execute("DELETE FROM sheets WHERE id = ?", (sheet_id,))
    return row


def query_entries(conn, date_from: str | None, date_to: str | None, q: str | None):
    """Entries from confirmed sheets only, so unreviewed parses never pollute the books."""
    sql = """SELECT e.*, s.id AS sheet_id
             FROM entries e JOIN sheets s ON s.id = e.sheet_id
             WHERE s.status = 'confirmed'"""
    args: list = []
    if date_from:
        sql += " AND e.entry_date >= ?"
        args.append(date_from)
    if date_to:
        sql += " AND e.entry_date <= ?"
        args.append(date_to)
    if q:
        sql += " AND (e.description LIKE ? OR e.party LIKE ? OR e.category LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY e.entry_date DESC, e.sheet_id DESC, e.position"
    return conn.execute(sql, args).fetchall()
