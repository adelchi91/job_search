from __future__ import annotations

from pathlib import Path

import pytest

from job_hunter.cli import init
from job_hunter.config import load_settings
from job_hunter.db import Store
from job_hunter.models import Job


class FakeLLM:
    """Returns canned replies by prompt keyword; records every call."""

    def __init__(self, replies: dict[str, dict] | None = None, fail: bool = False):
        self.replies = replies or {}
        self.fail = fail
        self.calls: list[str] = []

    def json(self, prompt: str, *, model: str, system: str = "") -> dict:
        self.calls.append(prompt)
        if self.fail:
            raise RuntimeError("model down")
        for key, reply in self.replies.items():
            if key in prompt:
                return reply
        raise KeyError("no canned reply")


@pytest.fixture
def settings(tmp_path: Path):
    init(tmp_path)
    s = load_settings(tmp_path)
    s.profile.name = "Sample Candidate"
    s.profile.email = "candidate@example.com"
    return s


@pytest.fixture
def store(settings):
    s = Store(settings.db_path)
    yield s
    s.close()


def make_job(**kw) -> Job:
    base = dict(
        source="ashby",
        source_id="1",
        company="Mistral AI",
        title="Forward Deployed Engineer",
        url="https://jobs.example/1",
        location="Paris",
        description="Deploy LLM agents with enterprise customers in Python.",
        ats="ashby",
        apply_url="https://jobs.example/1/apply",
    )
    base.update(kw)
    return Job(**base)
