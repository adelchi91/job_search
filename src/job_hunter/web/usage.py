"""LLM usage on the dashboard: the board widget, the /usage page, and "Re-run with AI"."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from job_hunter import usage
from job_hunter.collectors.base import make_client
from job_hunter.llm import configured_providers
from job_hunter.web.workspace import Workspace, flash, render, workspace

WS = Depends(workspace)


def widget(ws: Workspace, now: datetime | None = None) -> list[usage.ProviderUsage]:
    """Today's usage of every provider with a key, plus any provider called today."""
    return usage.today_summary(ws.store, ws.settings.llm_limits, configured_providers(), now)


def _quota_message(ws: Workspace) -> str:
    resets = [p.resets_at for p in widget(ws) if p.tone == "red" and p.resets_at]
    when = f", resets at {min(resets):%H:%M} (Paris)" if resets else ""
    return f"Quota still exhausted{when}: the template version was kept."


def _rerun(request: Request, ws: Workspace, job_id: int) -> None:
    from job_hunter.pipeline import rerun_ai
    from job_hunter.tailor.cv import load_master

    llm = request.app.state.llm_factory()
    if llm is None:
        flash(request, "No AI provider is configured, nothing to re-run.", "warn")
        return
    master = load_master(ws.settings.master_cv_path)
    with usage.recording(ws.store, ws.user.email), make_client() as client:
        ok = rerun_ai(ws.store, job_id, master, ws.settings, llm, client)
    if ok:
        flash(request, "Re-run with AI: CV, cover letter and fit score are up to date.")
    else:
        flash(request, _quota_message(ws), "warn")


def install_usage(app: FastAPI) -> None:
    @app.get("/usage")
    def usage_page(request: Request, ws: Workspace = WS):
        now = datetime.now(UTC)
        days = usage.history(ws.store, 7, now)
        names = list(
            dict.fromkeys([*configured_providers(), *(p for d in days for p in d.providers)])
        )
        ctx = {
            "usage": widget(ws, now),
            "days": days,
            "providers": names,
            "peak": max([1, *(t.requests for d in days for t in d.providers.values())]),
            "states": ws.store.rate_states(),
        }
        return render(request, "usage.html", ctx)

    @app.post("/jobs/{job_id}/rerun-ai")
    def rerun(request: Request, job_id: int, ws: Workspace = WS):
        if not ws.store.get(job_id):
            raise HTTPException(404, "job not found")
        _rerun(request, ws, job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)
