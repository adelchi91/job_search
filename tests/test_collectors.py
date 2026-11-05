from __future__ import annotations

import httpx

from job_hunter import fixtures
from job_hunter.collectors import build_collectors
from job_hunter.collectors.ats import AtsCollector, watchlist_collectors
from job_hunter.collectors.base import make_client, run_collectors, strip_html
from job_hunter.collectors.boards import (
    HnCollector,
    JobspyCollector,
    WttjCollector,
    parse_hn_comment,
    parse_jobspy_records,
)
from job_hunter.config import Company


def client():
    return make_client(fixtures.transport())


def test_strip_html_keeps_text_and_breaks():
    assert strip_html("<p>Hello&nbsp;<b>world</b></p><li>x</li>").split() == [
        "Hello",
        "world",
        "-",
        "x",
    ]


def test_strip_html_accepts_wttj_lists():
    assert strip_html(["Own the demo", "", "Run <b>POCs</b>"]) == "- Own the demo\n- Run POCs"
    assert strip_html(None) == ""


def test_each_ats_parser_yields_jobs():
    cases = [
        Company("Anthropic", "greenhouse", "anthropic"),
        Company("Palantir", "lever", "palantir"),
        Company("Mistral AI", "ashby", "mistral.ai"),
        Company("Hugging Face", "workable", "huggingface"),
    ]
    with client() as c:
        for company in cases:
            jobs = AtsCollector(company).collect(c)
            assert jobs, company
            assert all(j.company == company.name and j.title and j.url for j in jobs)
            assert jobs[0].ats == company.ats


def test_greenhouse_job_carries_board_and_id_for_question_lookup():
    with client() as c:
        job = AtsCollector(Company("Anthropic", "greenhouse", "anthropic")).collect(c)[0]
    assert job.extra["board"] == "anthropic" and job.extra["job_id"]


def test_watchlist_skips_disabled_and_tokenless():
    comps = [
        Company("A", "ashby", "a"),
        Company("B", "", ""),
        Company("C", "lever", "c", enabled=False),
    ]
    assert [c.name for c in watchlist_collectors(comps)] == ["ashby:a"]


def test_wttj_drops_internships():
    with client() as c:
        jobs = WttjCollector(queries=["AI engineer"], langs=("fr",)).collect(c)
    assert jobs and all(j.source == "wttj" for j in jobs)
    assert all(j.extra.get("contract_type") != "internship" for j in jobs)


def test_run_collectors_reports_errors_without_stopping():
    class Boom:
        name = "boom"

        def collect(self, client):
            raise httpx.ConnectError("down")

    with client() as c:
        jobs, report = run_collectors(
            [Boom(), AtsCollector(Company("M", "ashby", "mistral.ai"))], c
        )
    assert jobs
    assert "down" in report["boom"]
    assert report["ashby:mistral.ai"] == len(jobs)


def test_hn_collector_empty_and_parsed():
    with client() as c:
        assert HnCollector().collect(c) == []

    def handler(request: httpx.Request):
        if "story_" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "objectID": "9",
                            "parent_id": 1,
                            "comment_text": "Acme | AI Engineer | Paris, Remote | Full-time",
                        },
                        {"objectID": "10", "parent_id": 9, "comment_text": "reply"},
                    ]
                },
            )
        return httpx.Response(200, json={"hits": [{"objectID": "1"}]})

    with make_client(httpx.MockTransport(handler)) as c:
        jobs = HnCollector().collect(c)
    assert [(j.company, j.title, j.remote) for j in jobs] == [("Acme", "AI Engineer", "full")]


def test_parse_hn_comment_needs_pipes():
    assert parse_hn_comment({"comment_text": "just chatting"}) is None


def test_jobspy_records_handle_nan():
    jobs = parse_jobspy_records(
        [
            {
                "site": "linkedin",
                "id": "li-1",
                "company": "X",
                "title": "Presales Engineer",
                "job_url": "https://linkedin.com/jobs/1",
                "job_url_direct": float("nan"),
                "location": "Paris",
                "is_remote": True,
                "description": None,
            }
        ]
    )
    assert jobs[0].source == "linkedin"
    assert jobs[0].apply_url == "https://linkedin.com/jobs/1"
    assert jobs[0].remote == "full" and jobs[0].description == ""


def test_jobspy_missing_is_skipped(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **kw):
        if name == "jobspy":
            raise ImportError
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)
    assert JobspyCollector(queries=["x"]).collect(None) == []


def test_build_collectors_searches_boards_for_companies_without_ats(settings):
    cols = build_collectors(settings)
    wttj = next(c for c in cols if isinstance(c, WttjCollector))
    assert "Google AI" in wttj.queries
    assert any(isinstance(c, HnCollector) for c in cols)
