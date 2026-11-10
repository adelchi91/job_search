"""Draft master_cv.yaml from an uploaded CV PDF, for the user to review before it is used.

The draft is written next to the real file as master_cv.draft.yaml. Nothing changes until
the user presses Apply, which saves it through the same validation (and history) as an edit.
"""

from __future__ import annotations

import io
from pathlib import Path

import yaml

from job_hunter.config import DEFAULTS_DIR
from job_hunter.llm import LLM
from job_hunter.userfiles import FileError, save_yaml, validate_yaml

DRAFT = "master_cv.draft.yaml"
MAX_CHARS = 30000

SYSTEM = (
    "You convert a CV into structured YAML data. Copy facts exactly as written: never invent "
    "an employer, date, number, skill or achievement. Translate each text field so it has "
    "both an English (en) and a French (fr) version."
)

PROMPT = """Convert this CV into the JSON equivalent of the YAML schema shown in the example.
Keep the same keys. Give every experience and bullet a short unique id.

EXAMPLE (schema only, its content is a placeholder):
{example}

CV TEXT:
{text}

Return one JSON object with the same structure as the example."""


def pdf_text(content: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PdfReadError, ValueError) as exc:
        raise FileError("that PDF could not be read") from exc
    if not text.strip():
        raise FileError("no text found in that PDF (is it a scan?)")
    return text[:MAX_CHARS]


def draft_master(text: str, llm: LLM | None, model: str) -> str:
    """Ask the model for a master CV; return it as YAML text that passes validation."""
    if llm is None:
        raise FileError("no LLM is configured on the server (set ANTHROPIC_API_KEY)")
    example = (DEFAULTS_DIR / "master_cv.yaml").read_text(encoding="utf-8")
    data = llm.json(PROMPT.format(example=example, text=text), model=model, system=SYSTEM)
    out = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
    validate_yaml("master_cv.yaml", out)
    return "# Drafted from your PDF. Check every line, then press Apply.\n" + out


def make_draft(data_dir: Path, content: bytes, llm: LLM | None, model: str) -> Path:
    path = data_dir / DRAFT
    path.write_text(draft_master(pdf_text(content), llm, model), encoding="utf-8")
    return path


def read_draft(data_dir: Path) -> str:
    path = data_dir / DRAFT
    return path.read_text(encoding="utf-8") if path.exists() else ""


def apply_draft(data_dir: Path, text: str | None = None) -> None:
    """Save the (possibly edited) draft as the real master CV and drop the draft."""
    save_yaml(data_dir, "master_cv.yaml", text if text is not None else read_draft(data_dir))
    discard_draft(data_dir)


def discard_draft(data_dir: Path) -> None:
    (data_dir / DRAFT).unlink(missing_ok=True)
