"""Tracker dashboard: lanes, job detail with CV diff, one-click approve-and-send.

Multi-user: every route works inside the signed-in user's own folder (see workspace.py).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from job_hunter import applier
from job_hunter.llm import get_llm
from job_hunter.models import Status
from job_hunter.routing import AUTO_ATS
from job_hunter.userfiles import resolve_in
from job_hunter.web.auth import SESSION_DAYS, AuthConfig, install_auth, make_oauth
from job_hunter.web.workspace import Stores, Workspace, render, workspace

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
SCREENSHOT = "send_failure.png"

ApplyFn = Callable[..., applier.ApplyResult]
WS = Depends(workspace)


def _job_or_404(ws: Workspace, job_id: int) -> dict:
    job = ws.store.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


def _back(job_id: int) -> RedirectResponse:
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


def _cv(ws: Workspace, job: dict) -> Path | None:
    return resolve_in(ws.data_dir, job["cv_path"])


def _sibling(ws: Workspace, job: dict, name: str) -> Path | None:
    cv = _cv(ws, job)
    path = cv.parent / name if cv else None
    return path if path and path.exists() else None


def _file(path: Path | None, what: str, media_type: str = "application/pdf") -> FileResponse:
    if not path:
        raise HTTPException(404, f"no {what} for this job")
    return FileResponse(path, media_type=media_type)


def _all_answered(job: dict, answers: dict) -> bool:
    return (
        job["status"] == Status.NEEDS_YOU
        and job["ats"] in AUTO_ATS
        and all(v.strip() for v in {**job["answers"], **answers}.values())
    )


def _kpis(counts: dict) -> list[tuple[str, int, str]]:
    in_play = sum(counts.get(s, 0) for s in (Status.READY, Status.NEEDS_YOU, Status.MATCH))
    return [
        ("Ready to send", counts.get(Status.READY, 0), "ready"),
        ("Needs you", counts.get(Status.NEEDS_YOU, 0), "needs"),
        ("In play", in_play, "play"),
        ("Applied", counts.get(Status.APPLIED, 0), "applied"),
        ("Interviews", counts.get(Status.INTERVIEW, 0), "int"),
    ]


def _pages(app: FastAPI) -> None:
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/")
    def board(request: Request, ws: Workspace = WS):
        lanes = [(s, title, hint, ws.store.by_status(s)) for s, title, hint in LANES]
        side = [(s, title, ws.store.by_status(s)) for s, title in SIDE]
        counts = ws.store.counts()
        ctx = {"lanes": lanes, "side": side, "counts": counts, "kpis": _kpis(counts)}
        return render(request, "board.html", ctx)

    @app.get("/jobs/{job_id}")
    def detail(request: Request, job_id: int, ws: Workspace = WS):
        job = _job_or_404(ws, job_id)
        ctx = {
            "job": job,
            "events": ws.store.events(job_id),
            "moves": MANUAL_MOVES,
            "Status": Status,
            "has_cv": _cv(ws, job) is not None,
            "has_shot": _sibling(ws, job, SCREENSHOT) is not None,
        }
        return render(request, "job.html", ctx)

    @app.get("/jobs/{job_id}/cv.pdf")
    def cv_pdf(job_id: int, ws: Workspace = WS):
        return _file(_cv(ws, _job_or_404(ws, job_id)), "CV")

    @app.get("/jobs/{job_id}/cover_letter.pdf")
    def letter_pdf(job_id: int, ws: Workspace = WS):
        return _file(_sibling(ws, _job_or_404(ws, job_id), "cover_letter.pdf"), "cover letter")

    @app.get("/jobs/{job_id}/screenshot.png")
    def screenshot(job_id: int, ws: Workspace = WS):
        path = _sibling(ws, _job_or_404(ws, job_id), SCREENSHOT)
        return _file(path, "screenshot", "image/png")


def _actions(app: FastAPI) -> None:
    @app.post("/jobs/{job_id}/answers")
    async def save_answers(request: Request, job_id: int, ws: Workspace = WS):
        job = _job_or_404(ws, job_id)
        form = await request.form()
        answers = {k[2:]: str(v) for k, v in form.items() if k.startswith("a:")}
        letter = str(form.get("cover_letter", job["cover_letter"]))
        ws.store.update(job_id, answers={**job["answers"], **answers}, cover_letter=letter)
        ws.store.log(job_id, "edited", "answers / cover letter")
        if _all_answered(job, answers):
            ws.store.set_status(job_id, Status.READY, "All questions answered")
        return _back(job_id)

    @app.post("/jobs/{job_id}/send")
    def send(request: Request, job_id: int, ws: Workspace = WS):
        job = _job_or_404(ws, job_id)
        if job["status"] != Status.READY:
            raise HTTPException(409, "only jobs in Ready to send can be sent")
        cv = _cv(ws, job)
        if cv is None:
            raise HTTPException(409, "the tailored CV is missing: re-render it first")
        result = request.app.state.apply_fn(
            job["apply_url"] or job["url"],
            ws.settings.profile,
            cv,
            _sibling(ws, job, "cover_letter.pdf"),
            job["answers"],
            screenshot=cv.parent / SCREENSHOT,
        )
        if result.submitted:  # an old failure screenshot no longer applies
            (cv.parent / SCREENSHOT).unlink(missing_ok=True)
        status = Status.APPLIED if result.submitted else Status.NEEDS_YOU
        ws.store.update(job_id, route_reason=result.reason)
        ws.store.set_status(job_id, status, result.reason)
        return _back(job_id)

    @app.post("/jobs/{job_id}/status")
    def move(job_id: int, status: str = Form(...), ws: Workspace = WS):
        _job_or_404(ws, job_id)
        try:
            new = Status(status)
        except ValueError as exc:
            raise HTTPException(400, "unknown status") from exc
        ws.store.set_status(job_id, new, "moved by hand")
        return _back(job_id)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if app.state.scheduler:
        app.state.scheduler.start()
    yield
    if app.state.scheduler:
        app.state.scheduler.stop()
    app.state.stores.close_all()


def create_app(
    root: Path,
    auth: AuthConfig | None = None,
    apply_fn: ApplyFn = applier.apply,
    *,
    oauth=None,
    scheduler=None,
    llm_factory=get_llm,
    mailer=None,
) -> FastAPI:
    """root is the data dir in local mode, or the volume holding allowlist.yaml + users/."""
    from job_hunter.web.admin import install_admin
    from job_hunter.web.files import install_files

    auth = auth or AuthConfig()
    app = FastAPI(title="job-hunter", lifespan=_lifespan)
    state = app.state
    state.auth, state.root = auth, Path(root)
    state.directory = auth.directory(Path(root))
    state.stores = Stores()
    state.apply_fn, state.llm_factory, state.mailer = apply_fn, llm_factory, mailer
    state.oauth = oauth if oauth is not None or auth.local else make_oauth(auth)
    state.scheduler = scheduler
    app.add_middleware(
        SessionMiddleware,
        secret_key=auth.session_secret or "local-dev-only",
        session_cookie="jh_session",
        max_age=SESSION_DAYS * 86400,
        same_site="lax",
        https_only=not auth.local,
    )
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    install_auth(app, render)
    _pages(app)
    _actions(app)
    install_files(app)
    install_admin(app)
    return app
