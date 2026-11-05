"""Tailor the structured master CV to one job, without ever inventing experience.

The model may only pick, reorder and reword bullets that already exist in the master CV.
`validate` enforces that: unknown experiences or bullets are dropped, and a rewritten
bullet may not contain a number that its original did not contain.
"""

from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path

import yaml

from job_hunter.filters import fold
from job_hunter.llm import LLM
from job_hunter.models import Job

log = logging.getLogger("job_hunter.tailor")

MAX_BULLETS = 4

TAILOR_SYSTEM = (
    "You tailor CVs. You may ONLY select, reorder and lightly reword bullets that exist in "
    "the master CV, and reorder skills. Never add an employer, title, date, skill, tool, "
    "metric or achievement that is not already in the master. Keep every number unchanged."
)

TAILOR_PROMPT = """Write the CV in language: {lang}.

MASTER CV (YAML, bullets carry ids):
{master}

JOB: {title} at {company}
{description}

Return JSON:
{{"headline": "<short headline for this role, from the master's own experience>",
  "summary": "<2-3 sentence summary, only facts from the master>",
  "skills_first": ["<skills from the master to list first>"],
  "experiences": [{{"id": "<experience id>",
                   "bullets": [{{"id": "<bullet id>", "text": "<reworded bullet>"}}]}}]}}
Keep at most {max_bullets} bullets per experience, most relevant first."""


def tr(value, lang: str) -> str:
    """Bilingual fields are either a plain string or {en: ..., fr: ...}."""
    if isinstance(value, dict):
        return value.get(lang) or value.get("en") or next(iter(value.values()), "")
    return "" if value is None else str(value)


def load_master(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in NUM.findall(text or "")}


def _bullet_index(master: dict) -> dict[str, dict[str, dict]]:
    return {e["id"]: {b["id"]: b for b in e.get("bullets", [])} for e in master["experience"]}


def _check_bullet(original: dict, text: str, lang: str) -> str:
    """Keep the rewrite only if it adds no number; otherwise fall back to the original."""
    allowed = _numbers(tr(original, "en")) | _numbers(tr(original, "fr"))
    text = (text or "").strip()
    if not text or not _numbers(text) <= allowed:
        return tr(original, lang)
    return text


def validate(master: dict, proposal: dict, lang: str) -> dict:
    """Turn a model proposal into a safe tailored CV dict (same shape as `default_tailoring`)."""
    index = _bullet_index(master)
    chosen: dict[str, list[dict]] = {}
    for exp in proposal.get("experiences", []):
        bullets = index.get(exp.get("id"))
        if bullets is None:
            continue
        kept = []
        for b in exp.get("bullets", [])[:MAX_BULLETS]:
            if b.get("id") in bullets:
                kept.append(
                    {"id": b["id"], "text": _check_bullet(bullets[b["id"]], b.get("text"), lang)}
                )
        chosen[exp["id"]] = kept
    base = default_tailoring(master, lang)
    for exp in base["experiences"]:
        if chosen.get(exp["id"]):
            exp["bullets"] = chosen[exp["id"]]
    master_skills = {fold(s): s for s in all_skills(master, lang)}
    first = [
        master_skills[fold(s)] for s in proposal.get("skills_first", []) if fold(s) in master_skills
    ]
    base["skills_first"] = list(dict.fromkeys(first))
    base["headline"] = proposal.get("headline") or base["headline"]
    base["summary"] = proposal.get("summary") or base["summary"]
    return base


def all_skills(master: dict, lang: str) -> list[str]:
    return [tr(item, lang) for g in master.get("skills", []) for item in g.get("items", [])]


def default_tailoring(master: dict, lang: str) -> dict:
    return {
        "lang": lang,
        "headline": tr(master.get("headline"), lang),
        "summary": tr(master.get("summary"), lang),
        "skills_first": [],
        "experiences": [
            {
                "id": e["id"],
                "bullets": [
                    {"id": b["id"], "text": tr(b, lang)} for b in e.get("bullets", [])[:MAX_BULLETS]
                ],
            }
            for e in master["experience"]
        ],
    }


def heuristic_tailoring(master: dict, job: Job, lang: str) -> dict:
    """No LLM: rank each experience's bullets by word overlap with the posting."""
    words = {w for w in fold(job.description + " " + job.title).split() if len(w) > 3}

    def overlap(text: str) -> int:
        return len(words & set(fold(text).split()))

    out = default_tailoring(master, lang)
    for exp, src in zip(out["experiences"], master["experience"], strict=True):
        ranked = sorted(src.get("bullets", []), key=lambda b: -overlap(tr(b, lang)))
        exp["bullets"] = [{"id": b["id"], "text": tr(b, lang)} for b in ranked[:MAX_BULLETS]]
    skills = all_skills(master, lang)
    out["skills_first"] = [
        s for s in skills if fold(s).strip() and fold(s) in fold(job.description)
    ]
    return out


def tailor(master: dict, job: Job, lang: str, llm: LLM | None, model: str) -> dict:
    if llm is not None:
        prompt = TAILOR_PROMPT.format(
            lang=lang,
            master=yaml.safe_dump(master, allow_unicode=True, sort_keys=False)[:15000],
            title=job.title,
            company=job.company,
            description=job.description[:8000],
            max_bullets=MAX_BULLETS,
        )
        try:
            return validate(master, llm.json(prompt, model=model, system=TAILOR_SYSTEM), lang)
        except Exception as exc:  # noqa: BLE001 - deterministic fallback
            log.warning("LLM tailoring failed for %s: %s", job.title, exc)
    return heuristic_tailoring(master, job, lang)


def _dates(start: str, end: str) -> str:
    return start if not end or end == start else f"{start} – {end}"


def build_document(master: dict, tailored: dict) -> dict:
    """Merge master + tailoring into the flat document the Typst template renders."""
    lang = tailored["lang"]
    bullets = {e["id"]: e["bullets"] for e in tailored["experiences"]}
    first = tailored.get("skills_first", [])
    return {
        "lang": lang,
        "name": master.get("name", ""),
        "headline": tailored["headline"],
        "contact": master.get("contact", {}),
        "summary": tailored["summary"],
        "skills": [
            {
                "group": tr(g.get("group"), lang),
                "items": sorted(
                    (tr(i, lang) for i in g.get("items", [])),
                    key=lambda s: (s not in first, first.index(s) if s in first else 0),
                ),
            }
            for g in master.get("skills", [])
        ],
        "experience": [
            {
                "role": tr(e.get("role"), lang),
                "company": e.get("company", ""),
                "location": tr(e.get("location"), lang),
                "dates": _dates(tr(e.get("start"), lang), tr(e.get("end"), lang)),
                "bullets": [b["text"] for b in bullets.get(e["id"], [])],
            }
            for e in master["experience"]
        ],
        "education": [
            {
                "school": ed.get("school", ""),
                "degree": tr(ed.get("degree"), lang),
                "year": str(ed.get("year", "")),
            }
            for ed in master.get("education", [])
        ],
        "languages": [tr(x, lang) for x in master.get("languages", [])],
        "sections": [
            {"title": tr(s.get("title"), lang), "items": [tr(i, lang) for i in s.get("items", [])]}
            for s in master.get("sections", [])
        ],
        "labels": LABELS[lang],
    }


LABELS = {
    "en": {
        "summary": "Summary",
        "experience": "Experience",
        "skills": "Skills",
        "education": "Education",
        "languages": "Languages",
    },
    "fr": {
        "summary": "Profil",
        "experience": "Expérience",
        "skills": "Compétences",
        "education": "Formation",
        "languages": "Langues",
    },
}


def to_text(doc: dict) -> str:
    lines = [doc["name"], doc["headline"], "", doc["summary"], ""]
    for s in doc["skills"]:
        lines.append(f"{s['group']}: {', '.join(s['items'])}")
    for e in doc["experience"]:
        lines += ["", f"{e['role']} — {e['company']} ({e['dates']})"]
        lines += [f"  • {b}" for b in e["bullets"]]
    return "\n".join(lines)


def cv_diff(master: dict, tailored: dict) -> str:
    """Unified diff of the tailored CV against the untailored master in the same language."""
    full = build_document(master, default_tailoring(master, tailored["lang"]))
    for e, src in zip(full["experience"], master["experience"], strict=True):
        e["bullets"] = [tr(b, tailored["lang"]) for b in src.get("bullets", [])]
    new = build_document(master, tailored)
    return "\n".join(
        difflib.unified_diff(
            to_text(full).splitlines(), to_text(new).splitlines(), "master", "tailored", lineterm=""
        )
    )
