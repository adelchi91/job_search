"""Short cover letter per application, grounded only in the master CV.

The model only writes the greeting and body: it never sees the candidate's name, and the
sign-off and name are appended here, locally.
"""

from __future__ import annotations

import logging
import re

import yaml

from job_hunter.llm import LLM
from job_hunter.models import Job
from job_hunter.tailor.cv import tr
from job_hunter.tailor.privacy import Pseudonymiser, Scrubber, redact_master

log = logging.getLogger("job_hunter.cover")

COVER_SYSTEM = (
    "You write concise, specific cover letters (180-250 words). Use only facts present in "
    "the CV. No clichés, no invented achievements, no placeholders. Write the greeting and "
    "the body only: no sign-off line, no signature, no name."
)

COVER_PROMPT = """Language: {lang}
CV:
{cv}

JOB: {title} at {company}
{description}

Return JSON: {{"letter": "<greeting and body, plain text, no sign-off or signature>"}}"""

SIGN_OFF = {"en": "Kind regards,", "fr": "Bien cordialement,"}

TEMPLATES = {
    "en": (
        "Dear {company} hiring team,\n\n"
        "I am applying for the {title} role. {summary}\n\n"
        "In my recent work: {highlights}\n\n"
        "I would welcome the chance to discuss how this experience can help {company}.\n\n"
        "{sign_off}\n{name}"
    ),
    "fr": (
        "Madame, Monsieur,\n\n"
        "Je vous adresse ma candidature au poste de {title}. {summary}\n\n"
        "Parmi mes réalisations récentes : {highlights}\n\n"
        "Je serais ravi d'échanger sur la manière dont cette expérience peut servir {company}.\n\n"
        "{sign_off}\n{name}"
    ),
}

# Short closing lines the model may add despite the instructions; stripped before signing.
CLOSING = re.compile(
    r"^(?:(?:kind(?:est)?|best|warm(?:est)?)\s+regards|regards|best|sincerely|"
    r"yours\s+(?:sincerely|faithfully|truly)|(?:bien\s+)?cordialement|bien\s+à\s+vous|"
    r"(?:sincères|meilleures)\s+salutations|respectueusement)\s*[,.!]?$",
    re.I,
)
PLACEHOLDER = re.compile(r"^\[[^\]]*\]$")


def template_letter(master: dict, tailored: dict, job: Job) -> str:
    lang = tailored["lang"]
    first = next((e["bullets"] for e in tailored["experiences"] if e["bullets"]), [])
    highlights = "; ".join(b["text"].rstrip(".") for b in first[:2]) + "."
    return TEMPLATES[lang].format(
        company=job.company,
        title=job.title,
        summary=tailored["summary"],
        highlights=highlights,
        sign_off=SIGN_OFF[lang],
        name=master.get("name", ""),
    )


def _is_signature(line: str, name: str) -> bool:
    line = line.strip()
    return (
        not line
        or bool(CLOSING.match(line) or PLACEHOLDER.match(line))
        or "[CANDIDATE]" in line
        or (bool(name) and line.casefold() == name.casefold())
    )


def sign(letter: str, name: str, lang: str) -> str:
    """Drop any closing/signature lines the model wrote, then add the real ones."""
    lines = letter.strip().splitlines()
    while lines and _is_signature(lines[-1], name):
        lines.pop()
    body = "\n".join(lines).strip()
    return f"{body}\n\n{SIGN_OFF.get(lang, SIGN_OFF['en'])}\n{name}".strip() if body else ""


def letter_prompt(master: dict, tailored: dict, job: Job, pseudo: Pseudonymiser | None) -> str:
    """The cover-letter prompt: no name, no contact details, optionally no employers."""
    summary = tr(redact_master(master).get("summary"), "en")
    if pseudo:
        summary, tailored = pseudo.hide(summary), pseudo.forward(tailored)
    cv = Scrubber(master)({"summary": summary, "tailored": tailored})
    return COVER_PROMPT.format(
        lang=tailored["lang"],
        cv=yaml.safe_dump(cv, allow_unicode=True)[:10000],
        title=job.title,
        company=job.company,
        description=job.description[:6000],
    )


def cover_letter(
    master: dict,
    tailored: dict,
    job: Job,
    llm: LLM | None,
    model: str,
    *,
    anonymise_employers: bool = False,
) -> str:
    if llm is not None:
        pseudo = Pseudonymiser(master) if anonymise_employers else None
        prompt = letter_prompt(master, tailored, job, pseudo)
        try:
            letter = str(llm.json(prompt, model=model, system=COVER_SYSTEM)["letter"]).strip()
            if pseudo:
                letter = pseudo.show(letter)
            letter = sign(letter, master.get("name", ""), tailored["lang"])
            if letter:
                return letter
        except Exception as exc:  # noqa: BLE001 - deterministic fallback
            log.warning("LLM cover letter failed for %s: %s", job.title, exc)
    return template_letter(master, tailored, job)
