"""Admin-only routes behind the "Claude account" panel: status, sign in, paste code, cancel.

Everything here is guarded by ADMIN (403 for anyone else). The app only relays what the
`claude` CLI prints; tokens stay inside the CLI's own config dir and never reach a response.
"""

from __future__ import annotations

from fastapi import FastAPI, Form, HTTPException, Request

from job_hunter import claude_auth
from job_hunter.web.admin import ADMIN
from job_hunter.web.workspace import Workspace


def _login_view() -> dict | None:
    session = claude_auth.current()
    return session.snapshot() if session else None


def panel_state(request: Request, force: bool = False) -> dict:
    return {
        "status": claude_auth.status(force=force).as_dict(),
        "login": _login_view(),
        "local": request.app.state.auth.local,
    }


def install_claude(app: FastAPI) -> None:
    @app.get("/admin/claude/status")
    def claude_status(request: Request, refresh: int = 0, ws: Workspace = ADMIN):
        return panel_state(request, force=bool(refresh))

    @app.post("/admin/claude/login")
    def claude_login(request: Request, ws: Workspace = ADMIN):
        try:
            claude_auth.start_login()
        except (RuntimeError, OSError) as exc:
            raise HTTPException(503, str(exc)) from exc
        return panel_state(request)

    @app.post("/admin/claude/code")
    def claude_code(request: Request, code: str = Form(...), ws: Workspace = ADMIN):
        session = claude_auth.current()
        if not session:
            raise HTTPException(409, "no sign-in is running")
        try:
            session.submit_code(code)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return panel_state(request)

    @app.post("/admin/claude/cancel")
    def claude_cancel(request: Request, ws: Workspace = ADMIN):
        session = claude_auth.current()
        if session:
            session.cancel()
        return panel_state(request)

    @app.post("/admin/claude/logout")
    def claude_logout(request: Request, ws: Workspace = ADMIN):
        claude_auth.logout()
        return panel_state(request, force=True)
