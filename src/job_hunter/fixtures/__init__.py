"""Recorded API responses (trimmed, real data) used by tests and `run --fixtures`.

One edit to the real data: the first Anthropic job is relocated to Paris so the fixture run
exercises the "open questions -> Needs you" route.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent

ROUTES = [
    ("boards-api.greenhouse.io/v1/boards/anthropic/jobs/", "greenhouse_question.json"),
    ("boards-api.greenhouse.io/v1/boards/anthropic/jobs", "greenhouse_anthropic.json"),
    ("api.lever.co/v0/postings/palantir", "lever_palantir.json"),
    ("api.ashbyhq.com/posting-api/job-board/mistral.ai", "ashby_mistral.json"),
    ("apply.workable.com/api/v1/widget/accounts/huggingface", "workable_huggingface.json"),
    ("algolia.net/1/indexes/wttj_jobs_production_fr", "wttj.json"),
]

EMPTY = {"api.lever.co": []}


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    for pattern, name in ROUTES:
        if pattern in url:
            return httpx.Response(200, json=load(name))
    for host, body in EMPTY.items():
        if host in url:
            return httpx.Response(200, json=body)
    if "hn.algolia.com" in url:
        return httpx.Response(200, json={"hits": []})
    return httpx.Response(200, json={"jobs": [], "hits": []})


def transport() -> httpx.MockTransport:
    return httpx.MockTransport(_handler)
