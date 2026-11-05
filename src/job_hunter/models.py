"""Core data types shared by collectors, the store, the pipeline and the dashboard."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    NEW = "new"  # collected, matches filters, not scored yet
    LOW_FIT = "low_fit"  # scored below threshold, hidden from the main lanes
    MATCH = "match"  # scored above threshold, waiting for tailoring
    READY = "ready"  # CV + letter + answers prepared, waiting for one-click approval
    NEEDS_YOU = "needs_you"  # must be applied to by hand (LinkedIn, login, open questions)
    APPLIED = "applied"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    ARCHIVED = "archived"


OPEN_STATUSES = (Status.READY, Status.NEEDS_YOU, Status.APPLIED, Status.INTERVIEW)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text.lower())
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


@dataclass
class Job:
    source: str  # e.g. "greenhouse", "wttj", "linkedin"
    source_id: str
    company: str
    title: str
    url: str
    location: str = ""
    remote: str = ""  # "", "partial", "full", "no"
    description: str = ""
    language: str = ""  # "en" / "fr" / "" (unknown)
    published_at: str = ""
    apply_url: str = ""
    ats: str = ""  # greenhouse / lever / ashby / workable / "" when applied off-platform
    salary: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Same real job seen on several boards collapses to one fingerprint."""
        city = _norm(self.location).split(" ")[0] if self.location else ""
        key = f"{_norm(self.company)}|{_norm(self.title)}|{city}"
        return hashlib.sha1(key.encode()).hexdigest()[:16]
