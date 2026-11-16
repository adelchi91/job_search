"""Per-user LLM usage: one row per model call, kept in the user's own jobs.sqlite.

Each user's usage lives in their own folder, so it is isolated exactly like their jobs. The
admin page reads every user's file to show the totals side by side.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT DEFAULT '',
    ok INTEGER NOT NULL,
    tokens INTEGER DEFAULT 0
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.executescript(SCHEMA)  # additive: existing jobs and events are untouched
    return conn


def record(db_path: Path, provider: str, model: str, ok: bool, tokens: int = 0) -> None:
    at = datetime.now(UTC).isoformat(timespec="seconds")
    with closing(_connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO usage (at, provider, model, ok, tokens) VALUES (?, ?, ?, ?, ?)",
            (at, provider, model, int(ok), int(tokens or 0)),
        )


def summary(db_path: Path) -> list[dict]:
    """One row per provider: calls, failures, tokens, last call. [] when nothing recorded."""
    if not Path(db_path).exists():
        return []
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT provider, COUNT(*), SUM(1 - ok), COALESCE(SUM(tokens), 0), MAX(at) "
            "FROM usage GROUP BY provider ORDER BY provider"
        ).fetchall()
    keys = ("provider", "calls", "failed", "tokens", "last")
    return [dict(zip(keys, r, strict=True)) for r in rows]
