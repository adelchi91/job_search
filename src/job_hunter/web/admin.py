"""Admin page: who may sign in, and the daily run (next time, last results, Run now)."""

from __future__ import annotations

import threading

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from job_hunter.daily import RUN_LOCK, run_everyone
from job_hunter.web.workspace import Workspace, flash, render, workspace

WS = Depends(workspace)


def admin_only(ws: Workspace = WS) -> Workspace:
    if not ws.user.admin:
        raise HTTPException(403, "admins only")
    return ws


ADMIN = Depends(admin_only)


def _to_admin() -> RedirectResponse:
    return RedirectResponse("/admin", status_code=303)


def start_run(app: FastAPI) -> bool:
    """Run every user's pipeline in a background thread. False if a run is already going."""
    if RUN_LOCK.locked():
        return False
    state = app.state

    def job():
        results = run_everyone(
            state.directory, state.llm_factory(), state.mailer, state.auth.app_url
        )
        if results:
            state.last_manual = results

    threading.Thread(target=job, name="manual-run", daemon=True).start()
    return True


def install_admin(app: FastAPI) -> None:
    app.state.last_manual = []

    @app.get("/admin")
    def admin(request: Request, ws: Workspace = ADMIN):
        state = request.app.state
        sched = state.scheduler
        ctx = {
            "people": state.directory.users(),
            "next_at": sched.next_at if sched else None,
            "scheduled": sched is not None,
            "last": (sched.last if sched else []) or state.last_manual,
            "running": RUN_LOCK.locked(),
            "mail": state.mailer is not None,
        }
        return render(request, "admin.html", ctx)

    @app.post("/admin/people")
    def add_person(
        request: Request, email: str = Form(...), name: str = Form(""), ws: Workspace = ADMIN
    ):
        try:
            user = request.app.state.directory.add(email, name)
        except ValueError as exc:
            flash(request, str(exc), "error")
        else:
            flash(request, f"{user.email} can now sign in with Google.")
        return _to_admin()

    @app.post("/admin/people/remove")
    def remove_person(request: Request, email: str = Form(...), ws: Workspace = ADMIN):
        try:
            removed = request.app.state.directory.remove(email)
        except ValueError as exc:
            flash(request, str(exc), "error")
        else:
            msg = f"{email} can no longer sign in (their files are kept)."
            flash(request, msg if removed else f"{email} was not on the list.")
        return _to_admin()

    @app.post("/admin/run")
    def run_now(request: Request, ws: Workspace = ADMIN):
        if start_run(request.app):
            flash(request, "Run started for everyone. Refresh in a few minutes.")
        else:
            flash(request, "A run is already in progress.", "warn")
        return _to_admin()
