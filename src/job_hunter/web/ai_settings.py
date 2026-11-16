"""Dashboard Settings -> AI provider: setup guide, paste / remove a key, Test connection.

Keys are never sent back to the browser: the page only shows a masked hint and the source.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from job_hunter import providers
from job_hunter.config import Settings
from job_hunter.keystore import ENV_VARS, KeyStore


def _back(note: str) -> RedirectResponse:
    return RedirectResponse(f"/settings?note={quote(note)}", status_code=303)


SOURCES = {"env": "from the environment", "file": "saved in data/keys.yaml"}


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


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    settings: Settings,
    keystore: KeyStore,
    client: httpx.Client | None = None,
) -> None:
    cfg = settings.llm or {}

    def page(request: Request, result: dict | None = None, note: str = ""):
        ctx = {
            "providers": _rows(keystore, cfg),
            "guide": providers.GUIDE,
            "routing": providers.routing_summary(keystore, cfg),
            "result": result,
            "note": note or request.query_params.get("note", ""),
        }
        return templates.TemplateResponse(request, "settings.html", ctx)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return page(request)

    @app.post("/settings/ai/{name}/key")
    def save_key(name: str, key: str = Form("")):
        preset = _provider_or_404(name)
        if key.strip():
            keystore.set(name, key)
            note = f"{preset.label} key saved."
        else:
            note = "Paste a key first."
        return _back(note)

    @app.post("/settings/ai/{name}/remove")
    def remove_key(name: str):
        preset = _provider_or_404(name)
        keystore.delete(name)
        note = f"{preset.label} key removed."
        if keystore.source(name) == "env":
            note += f" It is still set in the environment ({ENV_VARS[name]})."
        return _back(note)

    @app.post("/settings/ai/{name}/test", response_class=HTMLResponse)
    def test_key(request: Request, name: str, key: str = Form("")):
        preset = _provider_or_404(name)
        ok, message = providers.test_connection(
            name, key.strip() or keystore.get(name) or "", cfg.get(name), client
        )
        return page(request, {"ok": ok, "label": preset.label, "message": message})
