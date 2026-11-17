"""SQLite store: one row per unique job (by fingerprint), plus an event log."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from job_hunter.models import OPEN_STATUSES, Job, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    location TEXT DEFAULT '',
    remote TEXT DEFAULT '',
    description TEXT DEFAULT '',
    language TEXT DEFAULT '',
    published_at TEXT DEFAULT '',
    apply_url TEXT DEFAULT '',
    ats TEXT DEFAULT '',
    salary TEXT DEFAULT '',
    extra TEXT DEFAULT '{}',
    also_seen_on TEXT DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new',
    score INTEGER,
    score_reason TEXT DEFAULT '',
    route_reason TEXT DEFAULT '',
    cv_path TEXT DEFAULT '',
    cv_diff TEXT DEFAULT '',
    cover_letter TEXT DEFAULT '',
    answers TEXT DEFAULT '{}',
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    at TEXT NOT NULL,
    in_tokens INTEGER NOT NULL DEFAULT 0,
    out_tokens INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    job_id INTEGER
);
CREATE INDEX IF NOT EXISTS llm_calls_at ON llm_calls (at);
CREATE TABLE IF NOT EXISTS llm_rate_state (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, model)
);
"""

# Columns added after the first release: (name, DDL), applied to older databases on open.
MIGRATIONS = (("ai_fallback", "ALTER TABLE jobs ADD COLUMN ai_fallback TEXT DEFAULT ''"),)

JOB_FIELDS = (
    "source source_id company title url location remote description language "
    "published_at apply_url ats salary"
).split()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for column, ddl in MIGRATIONS:
            if column not in have:
                self.conn.execute(ddl)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- writes -------------------------------------------------------------
    def upsert(self, job: Job) -> tuple[int, bool]:
        """Insert a new job, or record the extra source on a duplicate. Returns (id, is_new)."""
        row = self.conn.execute(
            "SELECT id, source, also_seen_on FROM jobs WHERE fingerprint = ?", (job.fingerprint,)
        ).fetchone()
        if row:
            seen = set(json.loads(row["also_seen_on"]))
            if job.source != row["source"] and job.source not in seen:
                seen.add(job.source)
                self.conn.execute(
                    "UPDATE jobs SET also_seen_on = ? WHERE id = ?",
                    (json.dumps(sorted(seen)), row["id"]),
                )
                self.conn.commit()
            return row["id"], False
        data = {k: getattr(job, k) for k in JOB_FIELDS}
        data.update(
            fingerprint=job.fingerprint,
            extra=json.dumps(job.extra),
            first_seen=now(),
            updated_at=now(),
        )
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        cur = self.conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(data.values()))
        self.conn.commit()
        self.log(cur.lastrowid, "collected", job.source)
        return cur.lastrowid, True

    def update(self, job_id: int, **fields) -> None:
        if "answers" in fields and not isinstance(fields["answers"], str):
            fields["answers"] = json.dumps(fields["answers"], ensure_ascii=False)
        fields["updated_at"] = now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))
        self.conn.commit()

    def set_status(self, job_id: int, status: Status, detail: str = "") -> None:
        self.update(job_id, status=str(status))
        self.log(job_id, f"status:{status}", detail)

    def log(self, job_id: int, kind: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events (job_id, at, kind, detail) VALUES (?, ?, ?, ?)",
            (job_id, now(), kind, detail),
        )
        self.conn.commit()

    def record_call(self, call: dict) -> None:
        """One LLM call: provider, model, at (UTC ISO), in/out tokens, status, user_id, job_id."""
        cols = ("user_id", "provider", "model", "at", "in_tokens", "out_tokens", "status", "job_id")
        row = {k: call.get(k) for k in cols}
        row["at"] = row["at"] or now()
        self.conn.execute(
            f"INSERT INTO llm_calls ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            tuple(row.values()),
        )
        self.conn.commit()

    def set_rate_state(self, provider: str, model: str, state: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_rate_state (provider, model, json, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (provider, model, json.dumps(state), now()),
        )
        self.conn.commit()

    # --- reads --------------------------------------------------------------
    def get(self, job_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row(row) if row else None

    def by_status(self, *statuses: Status) -> list[dict]:
        marks = ", ".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) "
            "ORDER BY COALESCE(score, -1) DESC, first_seen DESC",
            tuple(str(s) for s in statuses),
        ).fetchall()
        return [_row(r) for r in rows]

    def quota_flagged(self, *statuses: Status) -> list[dict]:
        """Jobs prepared without AI because of a quota, oldest first."""
        marks = ", ".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"SELECT * FROM jobs WHERE ai_fallback = 'quota' AND status IN ({marks}) "
            "ORDER BY updated_at",
            tuple(str(s) for s in statuses),
        ).fetchall()
        return [_row(r) for r in rows]

    def open_count(self, company: str) -> int:
        marks = ", ".join("?" for _ in OPEN_STATUSES)
        return self.conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE lower(company) = lower(?) AND status IN ({marks})",
            (company, *(str(s) for s in OPEN_STATUSES)),
        ).fetchone()[0]

    def events(self, job_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def usage_between(self, start: str, end: str) -> list[dict]:
        """LLM calls with start <= at < end (UTC ISO strings), oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM llm_calls WHERE at >= ? AND at < ? ORDER BY at", (start, end)
        ).fetchall()
        return [dict(r) for r in rows]

    def rate_states(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM llm_rate_state ORDER BY provider, model")
        return [{**dict(r), "json": json.loads(r["json"])} for r in rows.fetchall()]

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}


def _row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["extra"] = json.loads(d["extra"] or "{}")
    d["answers"] = json.loads(d["answers"] or "{}")
    d["also_seen_on"] = json.loads(d["also_seen_on"] or "[]")
    return d


def job_from_row(row: dict) -> Job:
    data = {k: row[k] for k in JOB_FIELDS}
    return Job(**data, extra=row.get("extra") or {})
