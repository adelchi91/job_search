"""Decide where a matched job goes: "Ready to send" (one-click approval) or "Needs you".

Only standard ATS forms we can fill end to end (Greenhouse, Lever, Ashby) are routed to
Ready. Boards that need Adelchi's own session (LinkedIn, Indeed, WTTJ...) and any form with
a required question we cannot answer from the profile go to Needs you, with the answers we
could prefill attached.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from job_hunter.config import Profile
from job_hunter.models import Job, Status

log = logging.getLogger("job_hunter.routing")

AUTO_ATS = {"greenhouse", "lever", "ashby"}

STANDARD_FIELDS = {
    "first_name",
    "last_name",
    "email",
    "phone",
    "resume",
    "resume_text",
    "cover_letter",
    "cover_letter_text",
}

# question-label keyword -> (profile attribute or answers key)
ANSWER_KEYS = [
    (("linkedin",), "linkedin"),
    (("github", "portfolio", "website"), "github"),
    (("notice", "préavis", "start date", "earliest"), "notice_period"),
    (("salary", "compensation", "rémunération", "salaire"), "salary_expectation"),
    (("authori", "visa", "sponsor", "right to work", "permit"), "work_authorisation"),
    (("where are you based", "current location", "city"), "location"),
]


@dataclass
class Route:
    status: Status
    reason: str
    answers: dict = field(default_factory=dict)
    open_questions: list[str] = field(default_factory=list)


def answer_for(label: str, profile: Profile) -> str:
    low = label.lower()
    for keywords, key in ANSWER_KEYS:
        if any(k in low for k in keywords):
            value = getattr(profile, key, "") or profile.answers.get(key, "")
            if value:
                return str(value)
    return ""


def greenhouse_questions(job: Job, client: httpx.Client) -> list[dict]:
    host = (
        "boards-api.eu.greenhouse.io"
        if job.extra.get("region") == "eu"
        else "boards-api.greenhouse.io"
    )
    url = f"https://{host}/v1/boards/{job.extra['board']}/jobs/{job.extra['job_id']}?questions=true"
    resp = client.get(url)
    resp.raise_for_status()
    return resp.json().get("questions", [])


def classify_questions(questions: list[dict], profile: Profile) -> tuple[dict, list[str]]:
    answers: dict[str, str] = {}
    unanswered: list[str] = []
    for q in questions:
        names = {f.get("name", "") for f in q.get("fields", [])}
        if names & STANDARD_FIELDS:
            continue
        label = q.get("label", "")
        value = answer_for(label, profile)
        if value:
            answers[label] = value
        elif q.get("required"):
            unanswered.append(label)
    return answers, unanswered


def route(job: Job, profile: Profile, client: httpx.Client | None = None) -> Route:
    if job.ats not in AUTO_ATS:
        where = job.source if job.source != "jobspy" else "the job board"
        return Route(Status.NEEDS_YOU, f"Apply on {where}: needs your own session")
    if not (profile.name and profile.email):
        return Route(Status.NEEDS_YOU, "Profile name/email missing: fill data/config.yaml")
    answers: dict[str, str] = {}
    unanswered: list[str] = []
    if job.ats == "greenhouse" and client is not None:
        try:
            answers, unanswered = classify_questions(greenhouse_questions(job, client), profile)
        except Exception as exc:  # noqa: BLE001 - unknown form: let a human look
            log.warning("could not read questions for %s: %s", job.url, exc)
            return Route(Status.NEEDS_YOU, "Could not read the application form")
    if unanswered:
        return Route(
            Status.NEEDS_YOU,
            f"{len(unanswered)} open question(s) need your answer",
            answers,
            unanswered,
        )
    return Route(Status.READY, f"Standard {job.ats} form: ready for one-click send", answers)
