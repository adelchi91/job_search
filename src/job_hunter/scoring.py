"""Fit score 0-100 for a job against the master CV, with Claude or a keyword heuristic."""

from __future__ import annotations

import logging

from job_hunter.filters import fold
from job_hunter.llm import LLM
from job_hunter.models import Job

log = logging.getLogger("job_hunter.scoring")

SCORE_SYSTEM = (
    "You are a precise technical recruiter. Score how well the candidate fits the job, "
    "0-100, where 60+ means worth applying. Judge seniority, domain, required skills, "
    "language and location. Never assume experience the CV does not show."
)

SCORE_PROMPT = """Target roles: {roles}

CANDIDATE CV (structured):
{cv}

JOB: {title} at {company} ({location}, remote={remote})
{description}

Return JSON: {{"score": <int 0-100>, "reason": "<one sentence>", "language": "<en|fr>"}}"""


def heuristic_score(job: Job, cv_text: str, roles: list[str]) -> tuple[int, str]:
    """Keyword overlap between the posting and the CV, plus a bonus when the title contains
    one of `roles` (target role names and title keywords)."""
    title = fold(job.title)
    role_hit = any(fold(r) in title for r in roles)
    words = {w for w in fold(job.description).split() if len(w) > 3}
    cv_words = {w for w in fold(cv_text).split() if len(w) > 3}
    overlap = len(words & cv_words) / max(len(words), 1)
    score = min(100, int(40 * role_hit + 30 + 120 * overlap))
    title_note = "matches" if role_hit else "near"
    return score, f"heuristic: title {title_note} a target role, overlap {overlap:.0%}"


def detect_language(job: Job) -> str:
    if job.language in ("en", "fr"):
        return job.language
    text = fold(job.description[:2000])
    fr = sum(text.count(f" {w} ") for w in ("vous", "nous", "les", "des", "et", "pour", "avec"))
    en = sum(text.count(f" {w} ") for w in ("you", "we", "the", "and", "for", "with", "our"))
    return "fr" if fr > en else "en"


def score_job(
    job: Job,
    cv_text: str,
    roles: list[str],
    llm: LLM | None,
    model: str,
    keywords: list[str] | None = None,
) -> dict:
    """Returns {"score", "reason", "language"}."""
    lang = detect_language(job)
    if llm is not None:
        prompt = SCORE_PROMPT.format(
            roles=", ".join(roles),
            cv=cv_text[:8000],
            title=job.title,
            company=job.company,
            location=job.location,
            remote=job.remote or "unknown",
            description=job.description[:8000],
        )
        try:
            out = llm.json(prompt, model=model, system=SCORE_SYSTEM)
            score = max(0, min(100, int(out["score"])))
            return {"score": score, "reason": str(out.get("reason", "")), "language": lang}
        except Exception as exc:  # noqa: BLE001 - fall back to the heuristic
            log.warning("LLM scoring failed for %s: %s", job.title, exc)
    score, reason = heuristic_score(job, cv_text, roles + (keywords or []))
    return {"score": score, "reason": reason, "language": lang}
