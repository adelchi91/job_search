"""Dashboard Settings -> AI provider: setup guide, paste / remove a key, Test connection.

Keys are never sent back to the browser: the page only shows a masked hint and the source.
Each signed-in user has their own keys, stored in their own folder (see workspace.py).
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from job_hunter import providers
from job_hunter.keystore import ENV_VARS, KeyStore
from job_hunter.web.workspace import Workspace, render, workspace

WS = Depends(workspace)


def _back(note: str) -> RedirectResponse:
    return RedirectResponse(f"/settings?note={quote(note)}", status_code=303)


SOURCES = {"env": "from the environment", "file": "saved in your keys.yaml"}


def _provider_or_404(name: str) -> providers.Preset:
    if name not in providers.PRESETS:
        raise HTTPException(404, "unknown provider")
    return providers.PRESETS[name]


def _rows(keystore: KeyStore, cfg: dict) -> list[dict]:
    rows = []
    for name, preset in providers.PRESETS.items():
        own = cfg.get(name) or {}
        rows.append(
            {
                "name": name,
                "label": preset.label,
                "console": preset.console_url,
                "masked": providers.mask(keystore.get(name)),
                "source": SOURCES.get(keystore.source(name), ""),
                "score_model": own.get("score_model", preset.score_model),
                "tailor_model": own.get("tailor_model", preset.tailor_model),
            }
        )
    return rows


def _keystore(request: Request, ws: Workspace) -> KeyStore:
    return request.app.state.keystore_factory(ws.data_dir)


def register(app: FastAPI) -> None:
    """Routes use `app.state.keystore_factory` (data dir -> KeyStore) and `.http_client`."""

    def page(request: Request, ws: Workspace, result: dict | None = None, note: str = ""):
        cfg = ws.settings.llm or {}
        keystore = _keystore(request, ws)
        ctx = {
            "providers": _rows(keystore, cfg),
            "guide": providers.GUIDE,
            "routing": providers.routing_summary(keystore, cfg),
            "result": result,
            "note": note or request.query_params.get("note", ""),
        }
        return render(request, "settings.html", ctx)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, ws: Workspace = WS):
        return page(request, ws)

    @app.post("/settings/ai/{name}/key")
    def save_key(request: Request, name: str, key: str = Form(""), ws: Workspace = WS):
        preset = _provider_or_404(name)
        if key.strip():
            _keystore(request, ws).set(name, key)
            note = f"{preset.label} key saved."
        else:
            note = "Paste a key first."
        return _back(note)

    @app.post("/settings/ai/{name}/remove")
    def remove_key(request: Request, name: str, ws: Workspace = WS):
        preset = _provider_or_404(name)
        keystore = _keystore(request, ws)
        keystore.delete(name)
        note = f"{preset.label} key removed."
        if keystore.source(name) == "env":
            note += f" It is still set in the environment ({ENV_VARS[name]})."
        return _back(note)

    @app.post("/settings/ai/{name}/test", response_class=HTMLResponse)
    def test_key(request: Request, name: str, key: str = Form(""), ws: Workspace = WS):
        preset = _provider_or_404(name)
        ok, message = providers.test_connection(
            name,
            key.strip() or _keystore(request, ws).get(name) or "",
            (ws.settings.llm or {}).get(name),
            request.app.state.http_client,
        )
        return page(request, ws, {"ok": ok, "label": preset.label, "message": message})
