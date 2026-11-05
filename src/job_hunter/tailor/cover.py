"""Short cover letter per application, grounded only in the master CV."""

from __future__ import annotations

import logging

import yaml

from job_hunter.llm import LLM
from job_hunter.models import Job
from job_hunter.tailor.cv import tr

log = logging.getLogger("job_hunter.cover")

COVER_SYSTEM = (
    "You write concise, specific cover letters (180-250 words). Use only facts present in "
    "the CV. No clichés, no invented achievements, no placeholders."
)

COVER_PROMPT = """Language: {lang}
CV:
{cv}

JOB: {title} at {company}
{description}

Return JSON: {{"letter": "<the letter body, greeting to sign-off, plain text>"}}"""

TEMPLATES = {
    "en": (
        "Dear {company} hiring team,\n\n"
        "I am applying for the {title} role. {summary}\n\n"
        "In my recent work: {highlights}\n\n"
        "I would welcome the chance to discuss how this experience can help {company}.\n\n"
        "Kind regards,\n{name}"
    ),
    "fr": (
        "Madame, Monsieur,\n\n"
        "Je vous adresse ma candidature au poste de {title}. {summary}\n\n"
        "Parmi mes réalisations récentes : {highlights}\n\n"
        "Je serais ravi d'échanger sur la manière dont cette expérience peut servir {company}.\n\n"
        "Bien cordialement,\n{name}"
    ),
}


def template_letter(master: dict, tailored: dict, job: Job) -> str:
    lang = tailored["lang"]
    first = next((e["bullets"] for e in tailored["experiences"] if e["bullets"]), [])
    highlights = "; ".join(b["text"].rstrip(".") for b in first[:2]) + "."
    return TEMPLATES[lang].format(
        company=job.company,
        title=job.title,
        summary=tailored["summary"],
        highlights=highlights,
        name=master.get("name", ""),
    )


def cover_letter(master: dict, tailored: dict, job: Job, llm: LLM | None, model: str) -> str:
    if llm is not None:
        prompt = COVER_PROMPT.format(
            lang=tailored["lang"],
            cv=yaml.safe_dump(
                {
                    "name": master.get("name"),
                    "summary": tr(master.get("summary"), "en"),
                    "tailored": tailored,
                },
                allow_unicode=True,
            )[:10000],
            title=job.title,
            company=job.company,
            description=job.description[:6000],
        )
        try:
            letter = str(llm.json(prompt, model=model, system=COVER_SYSTEM)["letter"]).strip()
            if letter:
                return letter
        except Exception as exc:  # noqa: BLE001 - deterministic fallback
            log.warning("LLM cover letter failed for %s: %s", job.title, exc)
    return template_letter(master, tailored, job)
