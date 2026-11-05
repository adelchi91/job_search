"""Local tracker dashboard: lanes, job detail with CV diff, one-click approve-and-send."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from job_hunter import applier
from job_hunter.config import Settings
from job_hunter.db import Store
from job_hunter.models import Status
from job_hunter.routing import AUTO_ATS

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

LANES = [
    (Status.READY, "Ready to send", "Prepared and waiting for your one-click approval"),
    (Status.NEEDS_YOU, "Needs you", "Apply by hand: board session, login or open questions"),
    (Status.APPLIED, "Applied", ""),
    (Status.INTERVIEW, "Interview", ""),
    (Status.OFFER, "Offer", ""),
]
SIDE = [
    (Status.MATCH, "Matched, not prepared yet"),
    (Status.REJECTED, "Rejected"),
    (Status.LOW_FIT, "Below fit threshold"),
    (Status.ARCHIVED, "Archived"),
]
MANUAL_MOVES = [Status.APPLIED, Status.INTERVIEW, Status.OFFER, Status.REJECTED, Status.ARCHIVED]

ApplyFn = Callable[..., applier.ApplyResult]


def _job_or_404(store: Store, job_id: int) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


def _back(job_id: int) -> RedirectResponse:
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


def _pdf(path: Path | None, what: str) -> FileResponse:
    if not path or not path.exists():
        raise HTTPException(404, f"no {what} rendered for this job")
    return FileResponse(path, media_type="application/pdf")


def _letter_path(job: dict) -> Path | None:
    return Path(job["cv_path"]).parent / "cover_letter.pdf" if job["cv_path"] else None


def _all_answered(job: dict, answers: dict) -> bool:
    return (
        job["status"] == Status.NEEDS_YOU
        and job["ats"] in AUTO_ATS
        and all(v.strip() for v in {**job["answers"], **answers}.values())
    )


def _pages(app: FastAPI, store: Store) -> None:
    @app.get("/", response_class=HTMLResponse)
    def board(request: Request):
        lanes = [(s, title, hint, store.by_status(s)) for s, title, hint in LANES]
        side = [(s, title, store.by_status(s)) for s, title in SIDE]
        return TEMPLATES.TemplateResponse(
            request, "board.html", {"lanes": lanes, "side": side, "counts": store.counts()}
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def detail(request: Request, job_id: int):
        job = _job_or_404(store, job_id)
        ctx = {"job": job, "events": store.events(job_id), "moves": MANUAL_MOVES, "Status": Status}
        return TEMPLATES.TemplateResponse(request, "job.html", ctx)

    @app.get("/jobs/{job_id}/cv.pdf")
    def cv_pdf(job_id: int):
        job = _job_or_404(store, job_id)
        return _pdf(Path(job["cv_path"]) if job["cv_path"] else None, "CV")

    @app.get("/jobs/{job_id}/cover_letter.pdf")
    def letter_pdf(job_id: int):
        return _pdf(_letter_path(_job_or_404(store, job_id)), "cover letter")


def _actions(app: FastAPI, settings: Settings, store: Store, apply_fn: ApplyFn) -> None:
    @app.post("/jobs/{job_id}/answers")
    async def save_answers(request: Request, job_id: int):
        job = _job_or_404(store, job_id)
        form = await request.form()
        answers = {k[2:]: str(v) for k, v in form.items() if k.startswith("a:")}
        letter = str(form.get("cover_letter", job["cover_letter"]))
        store.update(job_id, answers={**job["answers"], **answers}, cover_letter=letter)
        store.log(job_id, "edited", "answers / cover letter")
        if _all_answered(job, answers):
            store.set_status(job_id, Status.READY, "All questions answered")
        return _back(job_id)

    @app.post("/jobs/{job_id}/send")
    def send(job_id: int):
        job = _job_or_404(store, job_id)
        if job["status"] != Status.READY:
            raise HTTPException(409, "only jobs in Ready to send can be sent")
        letter = _letter_path(job)
        result = apply_fn(
            job["apply_url"] or job["url"],
            settings.profile,
            Path(job["cv_path"]),
            letter if letter and letter.exists() else None,
            job["answers"],
        )
        status = Status.APPLIED if result.submitted else Status.NEEDS_YOU
        store.update(job_id, route_reason=result.reason)
        store.set_status(job_id, status, result.reason)
        return _back(job_id)

    @app.post("/jobs/{job_id}/status")
    def move(job_id: int, status: str = Form(...)):
        _job_or_404(store, job_id)
        try:
            new = Status(status)
        except ValueError as exc:
            raise HTTPException(400, "unknown status") from exc
        store.set_status(job_id, new, "moved by hand")
        return _back(job_id)


def create_app(settings: Settings, store: Store, apply_fn: ApplyFn = applier.apply) -> FastAPI:
    app = FastAPI(title="job-hunter")
    _pages(app, store)
    _actions(app, settings, store, apply_fn)
    return app
