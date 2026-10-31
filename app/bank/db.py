"""SQLite access for the mock core banking store (WAL, foreign keys on)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "novatrust.sqlite3"


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added on build day: an existing database gets them in place, so a running box keeps its data.
ADDED_COLUMNS = (("loans", "overdue_paise", "INTEGER NOT NULL DEFAULT 0"), ("loans", "overdue_since", "TEXT"),
                 ("transactions", "status", "TEXT NOT NULL DEFAULT 'posted'"))


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    for table, column, ddl in ADDED_COLUMNS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.commit()
