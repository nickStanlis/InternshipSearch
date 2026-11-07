"""Database access for the UI.

SQLite connections can't be shared between threads, so conn() keeps one per
thread. NiceGUI runs every event handler on the same thread, so the pages share
one connection; background jobs (pipeline, rescore) open their own.
"""
from __future__ import annotations

import threading
from pathlib import Path

import db

_local = threading.local()
_db_path: str | Path | None = None  # None = db.DB_PATH (matches.db in the project root)


def use_database(path: str | Path | None) -> None:
    """Point the UI at another database file (tests use a temporary one)."""
    global _db_path
    _db_path = path


def conn():
    """This thread's connection, opened on first use."""
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != _db_path:
        if c is not None:
            c.close()
        c = db.connect(_db_path)
        _local.conn, _local.path = c, _db_path
    return c
