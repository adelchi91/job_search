from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from job_hunter import usage
from job_hunter.db import Store

LIMITS = {
    "groq": {"default": {"rpm": 30, "rpd": 10, "tpd": 1000}, "big": {"rpd": 5}},
    "mistral": {"default": {"rps": 1, "tpmonth": 10_000}},
}
NOW = datetime(2026, 11, 16, 14, 0, tzinfo=UTC)


def call(provider="groq", at="2026-11-16T10:00:00+00:00", status="ok", tin=100, tout=50):
    return usage.CallRecord(provider, "m", status, tin, tout, at)


def test_recording_writes_into_the_user_store_with_job_id(store):
    usage.emit(call())  # outside any recording: dropped
    with usage.recording(store, "a@example.com"), usage.job_scope(7):
        usage.emit(call())
        usage.emit_rate_state("groq", "m", {"windows": {}})
    usage.emit(call())
    rows = store.usage_between("2026-11-16", "2026-11-17")
    assert len(rows) == 1
    assert rows[0]["user_id"] == "a@example.com" and rows[0]["job_id"] == 7
    assert rows[0]["in_tokens"] == 100 and rows[0]["out_tokens"] == 50
    assert store.rate_states()[0]["json"] == {"windows": {}}


def test_nested_job_scope_shares_quota_hits():
    with usage.job_scope(1) as outer:
        with usage.job_scope(1) as inner:
            usage.note_quota_fallback()
        with usage.job_scope(2) as other:
            usage.note_quota_fallback()
    assert inner is outer and outer.quota_hits == 1 and other.quota_hits == 1
    usage.note_quota_fallback()  # no scope: no-op


def test_paris_day_boundaries():
    # 23:30 UTC on Nov 16 is already Nov 17 in Paris (UTC+1 in winter).
    assert str(usage.paris_day("2026-11-16T23:30:00+00:00")) == "2026-11-17"
    assert str(usage.paris_day(datetime(2026, 11, 16, 22, 59))) == "2026-11-16"
    start, end = usage.day_bounds(usage.paris_day(NOW))
    assert start == "2026-11-15T23:00:00+00:00" and end == "2026-11-16T23:00:00+00:00"


def test_history_has_every_day_including_empty_ones(store):
    with usage.recording(store):
        usage.emit(call(at="2026-11-10T09:00:00+00:00"))
        usage.emit(call(at="2026-11-16T09:00:00+00:00", status="429", tin=0, tout=0))
        usage.emit(call("mistral", at="2026-11-16T09:00:00+00:00"))
        usage.emit(call(at="2026-11-01T09:00:00+00:00"))  # outside the window
    days = usage.history(store, 7, NOW)
    assert [str(d.day) for d in days][0] == "2026-11-10" and len(days) == 7
    assert days[0].providers["groq"].requests == 1
    assert all(not d.providers for d in days[1:6])
    today = days[-1].providers
    assert today["groq"].rate_limited == 1 and today["mistral"].tokens == 150


def test_limits_overlay_model_row():
    assert usage.limits_for(LIMITS, "groq", "big") == {"rpm": 30, "rpd": 5, "tpd": 1000}
    assert usage.limits_for(LIMITS, "nope") == {}


def test_meter_tones():
    assert usage.Meter("x", 5, None).tone == "ok"
    assert usage.Meter("x", 79, 100).tone == "ok"
    assert usage.Meter("x", 80, 100).tone == "amber"
    assert usage.Meter("x", 100, 100).tone == "red"
    assert usage.Meter("x", 150, 100).pct == 100


def test_today_summary_counts_tones_and_resets(store):
    with usage.recording(store):
        for _ in range(8):
            usage.emit(call(at="2026-11-16T10:00:00+00:00", tin=10, tout=10))
        usage.emit(call("mistral", at="2026-11-02T10:00:00+00:00", tin=9000, tout=0))
    groq, mistral = usage.today_summary(store, LIMITS, ["groq", "mistral"], NOW)
    (anthropic,) = usage.today_summary(store, {}, ["anthropic"], NOW)[-1:]
    assert groq.requests == 8 and groq.tokens == 160 and groq.tone == "amber"
    assert groq.resets_at.isoformat() == "2026-11-17T01:00:00+01:00"
    assert mistral.requests == 0 and mistral.tone == "amber"  # 9000 / 10000 this month
    assert mistral.meters[-1].label == "tokens this month"
    assert mistral.resets_at.isoformat() == "2026-12-01T01:00:00+01:00"
    assert anthropic.resets_at is None and anthropic.meters[-1].cap is None


def test_reset_time_prefers_headers(store):
    state = {"windows": {"requests/day": {"reset_at": "2026-11-16T15:30:00+00:00"}}}
    store.set_rate_state("groq", "m", state)
    (groq,) = usage.today_summary(store, LIMITS, ["groq"], NOW)
    assert groq.resets_at.strftime("%H:%M") == "16:30"


def test_old_database_gets_the_ai_fallback_column(tmp_path):
    path = tmp_path / "jobs.sqlite"
    Store(path).close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE jobs DROP COLUMN ai_fallback")
    con.commit()
    con.close()
    s = Store(path)
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(jobs)")}
    s.close()
    assert "ai_fallback" in cols
