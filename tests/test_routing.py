from __future__ import annotations

import httpx
from conftest import make_job

from job_hunter import fixtures
from job_hunter.collectors.base import make_client
from job_hunter.config import Profile
from job_hunter.models import Status
from job_hunter.routing import answer_for, classify_questions, route

PROFILE = Profile(
    name="A B",
    email="a@b.c",
    linkedin="li/a",
    answers={"notice_period": "3 months", "work_authorisation": "EU citizen"},
)


def gh_job():
    return make_job(
        ats="greenhouse",
        source="greenhouse",
        company="Anthropic",
        extra={"board": "anthropic", "job_id": "5076109008"},
    )


def test_boards_and_workable_need_a_human():
    r = route(make_job(ats="", source="linkedin"), PROFILE)
    assert r.status == Status.NEEDS_YOU and "linkedin" in r.reason
    assert route(make_job(ats="", source="jobspy"), PROFILE).reason.startswith(
        "Apply on the job board"
    )


def test_missing_profile_needs_you():
    assert route(make_job(), Profile()).status == Status.NEEDS_YOU


def test_standard_form_is_ready():
    r = route(make_job(), PROFILE)
    assert r.status == Status.READY


def test_answer_for_maps_profile_and_answers():
    assert answer_for("LinkedIn Profile", PROFILE) == "li/a"
    assert answer_for("What is your notice period?", PROFILE) == "3 months"
    assert answer_for("Will you require visa sponsorship?", PROFILE) == "EU citizen"
    assert answer_for("Why us?", PROFILE) == ""


def test_classify_skips_standard_fields():
    qs = [
        {"label": "Resume", "required": True, "fields": [{"name": "resume"}]},
        {"label": "LinkedIn", "required": False, "fields": [{"name": "q1"}]},
        {"label": "Why us?", "required": True, "fields": [{"name": "q2"}]},
        {"label": "Fun fact", "required": False, "fields": [{"name": "q3"}]},
    ]
    assert classify_questions(qs, PROFILE) == ({"LinkedIn": "li/a"}, ["Why us?"])


def test_greenhouse_open_questions_route_to_needs_you():
    with make_client(fixtures.transport()) as c:
        r = route(gh_job(), PROFILE, c)
    assert r.status == Status.NEEDS_YOU and r.open_questions


def test_unreadable_form_needs_you():
    t = httpx.MockTransport(lambda req: httpx.Response(500))
    with make_client(t) as c:
        assert route(gh_job(), PROFILE, c).reason == "Could not read the application form"
