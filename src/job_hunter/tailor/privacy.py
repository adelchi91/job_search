"""Keep personal data out of LLM prompts, deterministically.

The master CV's fields are known, so redaction is rule-based rather than model-based:
contact fields are dropped, and the remaining free text is scrubbed for e-mail addresses,
phone numbers, the candidate's name and the literal contact values. Optionally,
`Pseudonymiser` swaps employer names and experience ids for tokens and maps the model's
reply back before it is validated.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import Any

# Removed wherever they appear in the master; `name` and `contact` only at the top level.
TOP_LEVEL_KEYS = frozenset({"name", "contact"})
CONTACT_KEYS = frozenset(
    {
        "email",
        "phone",
        "mobile",
        "address",
        "location",
        "linkedin",
        "github",
        "website",
        "url",
        "links",
    }
)
# Name parts that are titles or degrees, not identifying.
TITLES = frozenset({"phd", "dr", "mr", "mrs", "ms", "msc", "mba", "eng", "ing", "prof"})

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# International (+33 ...), national (06 ..., (0)6 ...) or 3-3-4 grouped numbers. Plain
# numbers such as years, "2019-2023" or "40%" never start with + or 0, so they stay.
PHONE = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[\s.-]?|\(?0)\(?\d[\d\s.\-()]{6,}\d"
    r"|(?<!\w)\d{3}[\s.-]\d{3}[\s.-]\d{4}(?!\w)"
)
MIN_PHONE_DIGITS = 9


def _walk(obj: Any, fn: Callable[[str], str]) -> Any:
    """Apply `fn` to every string value in a nested dict/list structure (keys untouched)."""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, dict):
        return {k: _walk(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, fn) for v in obj]
    return obj


def _drop_contact_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _drop_contact_keys(v) for k, v in obj.items() if k not in CONTACT_KEYS}
    if isinstance(obj, list):
        return [_drop_contact_keys(v) for v in obj]
    return obj


def _phone(match: re.Match) -> str:
    digits = sum(c.isdigit() for c in match.group(0))
    return "[PHONE]" if digits >= MIN_PHONE_DIGITS else match.group(0)


def scrub_pii(text: str) -> str:
    """Replace e-mail addresses and phone numbers in free text."""
    return PHONE.sub(_phone, EMAIL.sub("[EMAIL]", text))


def _literal(values: list[str]) -> re.Pattern | None:
    """One case-insensitive, whole-word pattern for `values`, longest first."""
    values = sorted({v for v in values if v}, key=len, reverse=True)
    if not values:
        return None
    alts = "|".join(re.escape(v) for v in values)
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.I)


def _name_parts(name: str) -> list[str]:
    """The full name, the name without titles ("Li Wu"), and each part of 3+ letters."""
    parts = [p for p in re.findall(r"[^\W\d_]+", name) if p.casefold() not in TITLES]
    return [name.strip(), " ".join(parts)] + [p for p in parts if len(p) >= 3]


def _contact_values(contact: Any) -> list[str]:
    if not isinstance(contact, dict):
        return []
    return [str(v).strip() for k, v in contact.items() if k != "location" and v]


class Scrubber:
    """Scrubs free text of one master CV's identity: name, contact values, e-mails, phones."""

    def __init__(self, master: dict) -> None:
        self.name = _literal(_name_parts(str(master.get("name") or "")))
        self.contact = _literal(_contact_values(master.get("contact")))

    def text(self, value: str) -> str:
        if self.contact:
            value = self.contact.sub("[CONTACT]", value)
        value = scrub_pii(value)
        if self.name:
            value = self.name.sub("[CANDIDATE]", value)
        return value

    def __call__(self, obj: Any) -> Any:
        return _walk(obj, self.text)


def redact_master(master: dict) -> dict:
    """A copy of the master that is safe to put in a prompt: no name, no contact details."""
    kept = {k: v for k, v in master.items() if k not in TOP_LEVEL_KEYS}
    return Scrubber(master)(_drop_contact_keys(copy.deepcopy(kept)))


TOKEN = re.compile(r"\[?EMPLOYER_(\d+)\]?")


class Pseudonymiser:
    """Swap employer names for [EMPLOYER_n] and experience/bullet ids for neutral ids.

    Only the master's `experience[].company` values are known employer names; another
    spelling in free text is not caught. The target job's own company is public and is
    not replaced, even when it is also a former employer.
    """

    def __init__(self, master: dict) -> None:
        experience = master.get("experience") or []
        companies = list(dict.fromkeys(str(e.get("company") or "") for e in experience))
        companies = [c for c in companies if c.strip()]
        self.tokens = {c: f"[EMPLOYER_{i}]" for i, c in enumerate(companies, 1)}
        self.names = {str(i): c for i, c in enumerate(companies, 1)}
        self.pattern = _literal(companies)
        self.ids: dict[str, str] = {}  # real experience id -> pseudo id
        self.bullet_ids: dict[tuple[str, str], str] = {}  # (real exp, real bullet) -> pseudo
        self.reverse: dict[str, str] = {}  # pseudo id -> real id
        for i, exp in enumerate(experience, 1):
            pseudo = f"exp_{i}"
            self.ids[exp["id"]] = pseudo
            self.reverse[pseudo] = exp["id"]
            for j, b in enumerate(exp.get("bullets") or [], 1):
                self.bullet_ids[(exp["id"], b["id"])] = f"{pseudo}_b{j}"
                self.reverse[f"{pseudo}_b{j}"] = b["id"]

    def hide(self, text: str) -> str:
        if not self.pattern:
            return text
        lookup = {k.casefold(): v for k, v in self.tokens.items()}
        return self.pattern.sub(lambda m: lookup[m.group(0).casefold()], text)

    def show(self, text: str) -> str:
        """Put the real employer names back into model output."""
        return TOKEN.sub(lambda m: self.names.get(m.group(1), m.group(0)), text)

    def _swap_ids(self, experiences: list, forward: bool) -> None:
        for exp in experiences:
            if not isinstance(exp, dict):
                continue
            real = exp.get("id") if forward else self.reverse.get(exp.get("id"), exp.get("id"))
            for b in exp.get("bullets") or []:
                if isinstance(b, dict) and "id" in b:
                    b["id"] = (
                        self.bullet_ids.get((real, b["id"]), b["id"])
                        if forward
                        else self.reverse.get(b["id"], b["id"])
                    )
            exp["id"] = self.ids.get(real, real) if forward else real

    def _apply(self, obj: dict, fn: Callable[[str], str], forward: bool) -> dict:
        out = _walk(copy.deepcopy(obj), fn)
        for key in ("experience", "experiences"):
            if isinstance(out.get(key), list):
                self._swap_ids(out[key], forward)
        return out

    def forward(self, obj: dict) -> dict:
        """Hide employers in a master or tailoring dict (returns a copy)."""
        return self._apply(obj, self.hide, forward=True)

    def back(self, obj: dict) -> dict:
        """Restore employers and ids in a model proposal (returns a copy)."""
        return self._apply(obj, self.show, forward=False)
