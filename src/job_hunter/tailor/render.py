"""Render the tailored CV and cover letter to PDF with Typst (pip `typst`, no binary needed)."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import typst

TEMPLATES = Path(__file__).parent / "templates"


def _compile(template: str, data_name: str, data: dict, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        shutil.copy(TEMPLATES / template, tmpdir / template)
        (tmpdir / data_name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        typst.compile(str(tmpdir / template), output=str(out), root=str(tmpdir))
    return out


def render_cv(document: dict, out: Path) -> Path:
    return _compile("cv.typ", "cv.json", document, out)


def render_letter(document: dict, letter: str, company: str, title: str, out: Path) -> Path:
    contact = document.get("contact", {})
    data = {
        "lang": document["lang"],
        "name": document["name"],
        "contact": " | ".join(str(v) for k, v in contact.items() if v and k in ("email", "phone")),
        "company": company,
        "title": title,
        "paragraphs": [p.strip() for p in letter.split("\n\n") if p.strip()],
    }
    return _compile("letter.typ", "letter.json", data, out)
