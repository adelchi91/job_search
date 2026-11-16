"""429 -> retry once -> other provider -> template, and the jobs that went through it."""

from __future__ import annotations

import json
import re

import httpx
import pytest
from conftest import FakeLLM, make_job
from fastapi.testclient import TestClient

from job_hunter import cli, fixtures, usage
from job_hunter.collectors.base import make_client
from job_hunter.llm import OpenAICompatLLM, quota_guard
from job_hunter.models import Status
from job_hunter.pipeline import rerun_ai, run
from job_hunter.ratelimit import Limiter, QuotaExceeded, QuotaGuard, RateLimited, Slot
from job_hunter.web.app import create_app
from job_hunter.web.auth import AuthConfig

NO_THROTTLE = {"groq": {"default": {"rpd": 1000}}, "mistral": {"default": {"rpd": 1000}}}
REPLIES = {"Target roles": {"score": 90, "reason": "fit"}, "Language": {"letter": "Dear team"}}


class ListCollector:
    name = "list"

    def __init__(self, jobs):
        self.jobs = jobs

    def collect(self, client):
        return self.jobs


class ChatAPI:
    """A fake OpenAI-compatible endpoint per host: canned replies, or 429s."""

    def __init__(self, limited: set[str] = frozenset(), headers: dict | None = None):
        self.limited, self.headers = limited, headers or {}
        self.hits: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host.split(".")[1]  # api.groq.com -> groq
        self.hits[host] = self.hits.get(host, 0) + 1
        if host in self.limited:
            return httpx.Response(429, headers={"retry-after": "1", **self.headers}, json={})
        prompt = json.loads(request.content)["messages"][-1]["content"]
        reply = next((v for k, v in REPLIES.items() if k in prompt), {})
        body = {
            "choices": [{"message": {"content": json.dumps(reply)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
        return httpx.Response(200, json=body, headers={"x-ratelimit-remaining-requests": "990"})

    def total(self) -> int:
        return sum(self.hits.values())


def guard_for(api: ChatAPI, monkeypatch, *providers: str) -> QuotaGuard:
    for name in providers:
        monkeypatch.setenv(f"{name.upper()}_API_KEY", "test-key")
    client = httpx.Client(transport=httpx.MockTransport(api))
    return quota_guard(NO_THROTTLE, client, sleep=lambda s: None)


class Scripted:
    """A provider client that plays back outcomes (a dict, or an exception to raise)."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0
        self.last_state, self.last_tokens = None, 0

    def json(self, prompt, *, model, system=""):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def slot(name, llm):
    return Slot(name, llm, "m", Limiter())


def test_429_is_retried_once_after_retry_after():
    slept = []
    groq = Scripted(RateLimited("groq", 3), {"ok": 1})
    guard = QuotaGuard([slot("groq", groq)], sleep=slept.append)
    assert guard.json("p", model="ignored") == {"ok": 1}
    assert groq.calls == 2 and slept == [3]


def test_two_429s_fail_over_to_the_other_provider():
    groq = Scripted(RateLimited("groq", None), RateLimited("groq", 99))
    mistral = Scripted({"from": "mistral"})
    slept = []
    guard = QuotaGuard([slot("groq", groq), slot("mistral", mistral)], sleep=slept.append)
    assert guard.json("p", model="x") == {"from": "mistral"}
    assert groq.calls == 2 and slept == [2.0]  # default wait when retry-after is missing
    # retry-after above 30 s blocks groq: the next call goes straight to mistral
    mistral.outcomes.append({"again": 1})
    assert guard.json("p", model="x") == {"again": 1} and groq.calls == 2


def test_every_provider_out_raises_quota_exceeded_and_marks_the_scope():
    guard = QuotaGuard(
        [slot("groq", Scripted(RateLimited("groq", 1), RateLimited("groq", 1)))],
        sleep=lambda s: None,
    )
    with usage.job_scope(5) as scope, pytest.raises(QuotaExceeded):
        guard.json("p", model="x")
    assert scope.quota_hits == 1 and guard.fallbacks == 1


def test_daily_quota_used_up_stops_http_calls(monkeypatch, store):
    api = ChatAPI(
        limited={"groq"},
        headers={"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "5h"},
    )
    guard = guard_for(api, monkeypatch, "groq")
    with usage.recording(store):
        for _ in range(3):
            with pytest.raises(QuotaExceeded):
                guard.json("p", model="x")
    assert api.hits == {"groq": 1}  # blocked until the reset, no retry, no further call
    assert store.rate_states()[0]["json"]["windows"]["requests/day"]["remaining"] == 0
    (groq,) = usage.today_summary(store, NO_THROTTLE, ["groq"])
    assert groq.rate_limited == 1


def test_success_records_tokens_and_rate_state(monkeypatch, store):
    guard = guard_for(ChatAPI(), monkeypatch, "groq", "mistral")
    assert [s.name for s in guard.slots] == ["groq", "mistral"]
    with usage.recording(store, "me@example.com"):
        assert guard.json("Target roles: x", model="x") == REPLIES["Target roles"]
    (row,) = store.usage_between("2000", "9999")
    assert (row["provider"], row["in_tokens"], row["out_tokens"]) == ("groq", 100, 20)
    assert row["model"] == "openai/gpt-oss-120b" and row["user_id"] == "me@example.com"


def test_http_errors_are_recorded():
    def boom(request):
        if request.url.host == "down":
            raise httpx.ConnectError("down")
        return httpx.Response(500, json={})

    client = httpx.Client(transport=httpx.MockTransport(boom))
    recorded = []
    sink = type("Sink", (), {"record": lambda s, r, j: recorded.append(r.status)})()
    token = usage._SINK.set(sink)
    try:
        with pytest.raises(httpx.ConnectError):
            OpenAICompatLLM("https://down", "k", provider="groq", client=client).json(
                "p", model="m"
            )
        with pytest.raises(httpx.HTTPStatusError):
            OpenAICompatLLM("https://up", "k", provider="groq", client=client).json("p", model="m")
    finally:
        usage._SINK.reset(token)
    assert recorded == ["error", "error"]


def batch(settings, store, llm, n=5):
    jobs = [make_job(source_id=str(i), company=f"Co {i}") for i in range(n)]
    settings.max_tailored_per_run = n
    with make_client(fixtures.transport()) as c:
        return run(settings, store, c, llm, [ListCollector(jobs)], user_id="me@example.com")


def test_quota_reached_falls_back_to_templates_and_no_job_fails(monkeypatch, settings, store):
    api = ChatAPI(limited={"groq"})
    result = batch(settings, store, guard_for(api, monkeypatch, "groq"))
    prepared = store.by_status(Status.READY, Status.NEEDS_YOU)
    assert len(result.prepared) == 5 and len(prepared) == 5
    assert all(j["ai_fallback"] == "quota" for j in prepared)
    assert all(j["score_reason"].startswith("heuristic") for j in prepared)
    assert any(e["detail"] == usage.QUOTA_NOTE for e in store.events(prepared[0]["id"]))
    assert not any(e["kind"] == "error" for j in prepared for e in store.events(j["id"]))

    # The next day's run re-runs the flagged jobs first, now that quota is back.
    rerun = guard_for(ChatAPI(), monkeypatch, "groq")
    batch(settings, store, rerun)
    assert not store.quota_flagged(*Status)
    job = store.get(prepared[0]["id"])
    assert job["score"] == 90 and job["cover_letter"].startswith("Dear team")


def test_widget_counts_match_a_batch_run(monkeypatch, settings, store, tmp_path):
    api = ChatAPI()
    batch(settings, store, guard_for(api, monkeypatch, "groq", "mistral"))
    assert api.total() == len(store.usage_between("2000", "9999")) > 0
    c = TestClient(create_app(tmp_path, AuthConfig(), llm_factory=lambda: None))
    board = c.get("/").text
    assert re.search(rf"\b{api.hits['groq']} requests · {api.total() * 120:,} tokens today", board)
    assert "Mistral" in board and "Resets" in board
    page = c.get("/usage").text
    assert "Last 7 days" in page and "<td>990</td>" in page
    assert "requests/day" in page  # Groq's x-ratelimit-*-requests is a per-day window
    assert api.hits == {"groq": api.total()}  # Mistral is only the fallback


def flagged_ready_job(settings, store, tmp_path):
    job_id, _ = store.upsert(make_job())
    store.update(job_id, score=70, score_reason="heuristic: x", ai_fallback="quota")
    store.update(job_id, cover_letter="template", language="en")
    store.set_status(job_id, Status.READY, "ok")
    return job_id


def test_rerun_ai_keeps_flag_while_quota_is_out(settings, store, tmp_path):
    from job_hunter.tailor.cv import load_master

    job_id = flagged_ready_job(settings, store, tmp_path)
    master = load_master(settings.master_cv_path)
    out = QuotaGuard([slot("groq", Scripted(*[RateLimited("groq", 99)] * 9))])
    assert rerun_ai(store, job_id, master, settings, out, None) is False
    assert store.get(job_id)["ai_fallback"] == "quota"
    assert rerun_ai(store, job_id, master, settings, None, None) is False
    assert rerun_ai(store, job_id, master, settings, FakeLLM(REPLIES), None) is True
    job = store.get(job_id)
    assert (
        job["ai_fallback"] == ""
        and job["score"] == 90
        and job["cover_letter"].startswith("Dear team")
    )


def test_web_rerun_button(settings, store, tmp_path):
    job_id = flagged_ready_job(settings, store, tmp_path)
    c = TestClient(create_app(tmp_path, AuthConfig(), llm_factory=lambda: None))
    page = c.get(f"/jobs/{job_id}").text
    assert "Prepared without AI (quota reached)" in page and "Re-run with AI" in page
    assert "no AI · quota" in c.get("/").text
    r = c.post(f"/jobs/{job_id}/rerun-ai", follow_redirects=True)
    assert "No AI provider is configured" in r.text
    assert c.post("/jobs/999/rerun-ai").status_code == 404

    c.app.state.llm_factory = lambda: QuotaGuard([slot("g", Scripted(RateLimited("g", 99)))])
    r = c.post(f"/jobs/{job_id}/rerun-ai", follow_redirects=True)
    assert "Quota still exhausted" in r.text

    c.app.state.llm_factory = lambda: FakeLLM(REPLIES)
    r = c.post(f"/jobs/{job_id}/rerun-ai", follow_redirects=True)
    assert "Re-run with AI: CV" in r.text and "Re-run with AI</button>" not in r.text


def test_cli_rerun_ai(monkeypatch, settings, store, tmp_path, capsys):
    job_id = flagged_ready_job(settings, store, tmp_path)
    data = ["--data", str(tmp_path)]
    monkeypatch.setattr(cli, "get_llm", lambda limits=None: None)
    assert cli.main([*data, "rerun-ai", "--all"]) == 1
    monkeypatch.setattr(cli, "get_llm", lambda limits=None: FakeLLM(REPLIES))
    assert cli.main([*data, "rerun-ai"]) == 0
    assert "1 job(s) flagged" in capsys.readouterr().out
    assert cli.main([*data, "rerun-ai", str(job_id)]) == 0
    assert "Re-ran 1 of 1" in capsys.readouterr().out
