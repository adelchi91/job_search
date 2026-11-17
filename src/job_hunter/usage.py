"""LLM usage: where call records go, and today's / the last days' totals per provider.

The LLM object is shared by every user of a deployment, so it does not hold a database.
Whoever runs a batch for a user opens `recording(store, user_id)`; every call made inside
that block lands in that user's store. Outside any block, records are dropped.

"Today" is the Europe/Paris calendar day.

Per-user LLM usage summary: `record` / `summary` also keep one row per model call in a
`usage` table of the user's own jobs.sqlite, so each user's usage is isolated exactly like
their jobs. The admin page reads every user's file to show the totals side by side.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
AMBER_AT = 80
QUOTA_NOTE = "prepared without AI (quota reached)"
FAR_FUTURE = "9999-12-31"

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


@dataclass
class CallRecord:
    provider: str
    model: str
    status: str  # "ok" | "429" | "error"
    in_tokens: int = 0
    out_tokens: int = 0
    at: str = ""


@dataclass
class UsageSink:
    """Writes call records and the latest rate-limit headers into one user's store."""

    store: object  # job_hunter.db.Store (not imported: db must not depend on this module)
    user_id: str | None = None

    def record(self, rec: CallRecord, job_id: int | None) -> None:
        self.store.record_call(
            {**rec.__dict__, "at": rec.at or None, "user_id": self.user_id, "job_id": job_id}
        )

    def rate_state(self, provider: str, model: str, state: dict) -> None:
        self.store.set_rate_state(provider, model, state)


@dataclass
class JobScope:
    """Collects what happened to the LLM calls made for one job."""

    job_id: int | None
    quota_hits: int = 0


_SINK: ContextVar[UsageSink | None] = ContextVar("usage_sink", default=None)
_JOB: ContextVar[JobScope | None] = ContextVar("usage_job", default=None)


def current_sink() -> UsageSink | None:
    return _SINK.get()


@contextmanager
def recording(store, user_id: str | None = None) -> Iterator[UsageSink]:
    token = _SINK.set(UsageSink(store, user_id))
    try:
        yield _SINK.get()
    finally:
        _SINK.reset(token)


@contextmanager
def job_scope(job_id: int | None) -> Iterator[JobScope]:
    """Inside an open scope for the same job, reuses it so nested steps add up."""
    outer = _JOB.get()
    if outer is not None and outer.job_id == job_id:
        yield outer
        return
    token = _JOB.set(JobScope(job_id))
    try:
        yield _JOB.get()
    finally:
        _JOB.reset(token)


def emit(rec: CallRecord) -> None:
    sink, job = _SINK.get(), _JOB.get()
    if sink is not None:
        sink.record(rec, job.job_id if job else None)


def emit_rate_state(provider: str, model: str, state: dict) -> None:
    sink = _SINK.get()
    if sink is not None:
        sink.rate_state(provider, model, state)


def note_quota_fallback() -> None:
    """Every provider was out of quota: the caller is about to use its template instead."""
    job = _JOB.get()
    if job is not None:
        job.quota_hits += 1


# --- aggregation ---------------------------------------------------------------


def paris_day(at: str | datetime) -> date:
    moment = datetime.fromisoformat(at) if isinstance(at, str) else at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(PARIS).date()


def day_bounds(day: date) -> tuple[str, str]:
    """[start, end) of a Paris calendar day, as UTC ISO strings comparable with stored `at`."""
    start = datetime.combine(day, time.min, PARIS)
    end = datetime.combine(day + timedelta(days=1), time.min, PARIS)
    return _iso(start), _iso(end)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


@dataclass
class DayTotals:
    requests: int = 0
    tokens: int = 0
    rate_limited: int = 0

    def add(self, call: dict) -> None:
        self.requests += 1
        self.tokens += (call.get("in_tokens") or 0) + (call.get("out_tokens") or 0)
        self.rate_limited += call.get("status") == "429"


def totals_by_provider(calls: Iterable[dict]) -> dict[str, DayTotals]:
    out: dict[str, DayTotals] = {}
    for call in calls:
        out.setdefault(call["provider"], DayTotals()).add(call)
    return out


@dataclass
class HistoryDay:
    day: date
    providers: dict[str, DayTotals] = field(default_factory=dict)


def history(store, days: int = 7, now: datetime | None = None) -> list[HistoryDay]:
    """One entry per Paris day, oldest first, ending today; days without calls are empty."""
    today = paris_day(now or datetime.now(UTC))
    first = today - timedelta(days=days - 1)
    calls = store.usage_between(day_bounds(first)[0], day_bounds(today)[1])
    buckets = {
        first + timedelta(days=i): HistoryDay(first + timedelta(days=i)) for i in range(days)
    }
    for call in calls:
        bucket = buckets.get(paris_day(call["at"]))
        if bucket is not None:
            bucket.providers.setdefault(call["provider"], DayTotals()).add(call)
    return list(buckets.values())


def limits_for(llm_limits: dict, provider: str, model: str = "") -> dict:
    """Configured caps for a provider: its `default` row overlaid by the model's own row."""
    rows = (llm_limits or {}).get(provider) or {}
    return {**(rows.get("default") or {}), **(rows.get(model) or {})}


@dataclass
class Meter:
    label: str
    used: int
    cap: int | None

    @property
    def pct(self) -> int:
        return min(100, round(100 * self.used / self.cap)) if self.cap else 0

    @property
    def tone(self) -> str:
        if not self.cap:
            return "ok"
        return "red" if self.used >= self.cap else ("amber" if self.pct >= AMBER_AT else "ok")


@dataclass
class ProviderUsage:
    provider: str
    requests: int
    tokens: int
    rate_limited: int
    meters: list[Meter]
    resets_at: datetime | None  # Paris time

    @property
    def tone(self) -> str:
        tones = {m.tone for m in self.meters}
        return "red" if "red" in tones else ("amber" if "amber" in tones else "ok")


def today_summary(
    store, llm_limits: dict, providers: Iterable[str] = (), now: datetime | None = None
) -> list[ProviderUsage]:
    """Today's usage per provider: `providers` plus any provider called today."""
    now = now or datetime.now(UTC)
    today = paris_day(now)
    totals = totals_by_provider(store.usage_between(*day_bounds(today)))
    month = _month_tokens(store, today)
    states = {s["provider"]: s["json"] for s in store.rate_states()}
    names = list(dict.fromkeys([*providers, *totals]))
    return [
        _provider_usage(
            name, totals.get(name, DayTotals()), month.get(name, 0), llm_limits, states, now
        )
        for name in names
    ]


def _month_tokens(store, today: date) -> dict[str, int]:
    start = day_bounds(today.replace(day=1))[0]
    return {
        p: t.tokens for p, t in totals_by_provider(store.usage_between(start, FAR_FUTURE)).items()
    }


def _provider_usage(name, day, month_tokens, llm_limits, states, now) -> ProviderUsage:
    caps = limits_for(llm_limits, name)
    meters = [Meter("requests today", day.requests, caps.get("rpd"))]
    if caps.get("tpd"):
        meters.append(Meter("tokens today", day.tokens, caps["tpd"]))
    if caps.get("tpmonth"):
        meters.append(Meter("tokens this month", month_tokens, caps["tpmonth"]))
    if not caps.get("tpd") and not caps.get("tpmonth"):
        meters.append(Meter("tokens today", day.tokens, None))
    reset = _reset_time(caps, states.get(name) or {}, now)
    return ProviderUsage(name, day.requests, day.tokens, day.rate_limited, meters, reset)


def _reset_time(caps: dict, state: dict, now: datetime) -> datetime | None:
    """When the binding cap resets, in Paris time: from headers if known, else by calendar."""
    day = (state.get("windows") or {}).get("requests/day") or {}
    if day.get("reset_at"):
        return datetime.fromisoformat(day["reset_at"]).astimezone(PARIS)
    utc = now.astimezone(UTC)
    if caps.get("rpd") or caps.get("tpd"):
        return datetime.combine(utc.date() + timedelta(days=1), time.min, UTC).astimezone(PARIS)
    if caps.get("tpmonth"):
        first = (utc.date().replace(day=1) + timedelta(days=32)).replace(day=1)
        return datetime.combine(first, time.min, UTC).astimezone(PARIS)
    return None


# --- per-user usage table (record / summary) -------------------------------------


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
