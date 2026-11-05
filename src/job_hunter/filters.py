"""Cheap deterministic filters applied before any LLM call: role and location."""

from __future__ import annotations

import re
import unicodedata

from job_hunter.config import Settings
from job_hunter.models import Job


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " " + re.sub(r"[^a-z0-9+]+", " ", text.lower()).strip() + " "


def _contains_any(haystack: str, needles: list[str]) -> bool:
    return any(fold(n) in haystack for n in needles)


def role_matches(title: str, settings: Settings) -> bool:
    t = fold(title)
    return _contains_any(t, settings.role_keywords) and not _contains_any(
        t, settings.exclude_keywords
    )


def location_matches(job: Job, settings: Settings) -> bool:
    loc = fold(job.location)
    if not job.location.strip():
        return True  # unknown location: let scoring decide
    if _contains_any(loc, settings.locations):
        return True
    remote = job.remote in ("full", "fulltime") or " remote " in loc
    return remote and _contains_any(loc + fold(job.remote), settings.remote_regions)


def keep(job: Job, settings: Settings) -> bool:
    return role_matches(job.title, settings) and location_matches(job, settings)
