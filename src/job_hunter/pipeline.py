"""One daily run: collect -> filter & dedupe -> score -> tailor -> route -> digest.

Nothing is ever submitted here. Sending happens only from the dashboard's Send button.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from job_hunter.collectors import build_collectors
from job_hunter.collectors.base import Collector, run_collectors
from job_hunter.config import Settings
from job_hunter.db import Store, job_from_row
from job_hunter.digest import build_digest, write_digest
from job_hunter.filters import keep
from job_hunter.llm import LLM
from job_hunter.models import Job, Status
from job_hunter.routing import route
from job_hunter.scoring import score_job
from job_hunter.tailor.cover import cover_letter
from job_hunter.tailor.cv import build_document, cv_diff, load_master, tailor
from job_hunter.tailor.render import render_cv, render_letter

log = logging.getLogger("job_hunter.pipeline")


@dataclass
class RunResult:
    report: dict
    collected: int = 0
    kept: int = 0
    new_ids: list[int] = field(default_factory=list)
    prepared: list[int] = field(default_factory=list)
    digest_path: Path | None = None
    digest: str = ""


def ingest(store: Store, jobs: list[Job], settings: Settings) -> tuple[int, list[int]]:
    kept, new_ids = 0, []
    for job in jobs:
        if not keep(job, settings):
            continue
        kept += 1
        job_id, is_new = store.upsert(job)
        if is_new:
            new_ids.append(job_id)
    return kept, new_ids


def score_new(store: Store, master: dict, settings: Settings, llm: LLM | None) -> None:
    cv_text = yaml.safe_dump(master, allow_unicode=True, sort_keys=False)
    for row in store.by_status(Status.NEW):
        out = score_job(
            job_from_row(row),
            cv_text,
            settings.roles,
            llm,
            settings.score_model,
            settings.role_keywords,
        )
        status = Status.MATCH if out["score"] >= settings.fit_threshold else Status.LOW_FIT
        store.update(
            row["id"], score=out["score"], score_reason=out["reason"], language=out["language"]
        )
        store.set_status(row["id"], status, f"fit {out['score']}")


def prepare(
    store: Store, row: dict, master: dict, settings: Settings, llm: LLM | None, client
) -> None:
    """Tailor CV + letter for one matched job, render PDFs and route it."""
    job = job_from_row(row)
    lang = row["language"] or "en"
    tailored = tailor(master, job, lang, llm, settings.tailor_model)
    doc = build_document(master, tailored)
    letter = cover_letter(master, tailored, job, llm, settings.tailor_model)
    out_dir = settings.data_dir / "applications" / f"{row['id']:05d}"
    cv_pdf = render_cv(doc, out_dir / f"CV_{_slug(master.get('name', 'cv'))}_{lang}.pdf")
    render_letter(doc, letter, job.company, job.title, out_dir / "cover_letter.pdf")
    (out_dir / "cover_letter.txt").write_text(letter, encoding="utf-8")
    r = route(job, settings.profile, client)
    answers = {**r.answers, **{q: "" for q in r.open_questions}}
    store.update(
        row["id"],
        cv_path=str(cv_pdf),
        cv_diff=cv_diff(master, tailored),
        cover_letter=letter,
        answers=answers,
        route_reason=r.reason,
    )
    store.set_status(row["id"], r.status, r.reason)


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")


def prepare_matches(
    store: Store, master: dict, settings: Settings, llm: LLM | None, client
) -> list[int]:
    done: list[int] = []
    for row in store.by_status(Status.MATCH):
        if len(done) >= settings.max_tailored_per_run:
            break
        if store.open_count(row["company"]) >= settings.max_open_per_company:
            store.update(
                row["id"],
                route_reason=f"Waiting: {settings.max_open_per_company} open at {row['company']}",
            )
            continue
        try:
            prepare(store, row, master, settings, llm, client)
        except Exception as exc:  # noqa: BLE001 - one bad job never stops the run
            log.exception("preparing job %s failed", row["id"])
            store.log(row["id"], "error", str(exc)[:300])
            continue
        done.append(row["id"])
    return done


def run(
    settings: Settings,
    store: Store,
    client: httpx.Client,
    llm: LLM | None,
    collectors: list[Collector] | None = None,
) -> RunResult:
    master = load_master(settings.master_cv_path)
    jobs, report = run_collectors(collectors or build_collectors(settings), client)
    kept, new_ids = ingest(store, jobs, settings)
    log.info("collected %d, kept %d, new %d", len(jobs), kept, len(new_ids))
    score_new(store, master, settings, llm)
    prepared = prepare_matches(store, master, settings, llm, client)
    digest = build_digest(store, report, new_ids)
    path = write_digest(digest, settings.data_dir)
    return RunResult(report, len(jobs), kept, new_ids, prepared, path, digest)
