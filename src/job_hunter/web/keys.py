"""My files > AI keys: each user saves their own Groq / Mistral key, write-only.

The page only ever shows a mask (gsk_…a1b2); the key itself never goes back to the browser.
Keys are encrypted in the user's own folder (see secrets_store), so they are as private as
the user's jobs.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from job_hunter import usage
from job_hunter.secrets_store import PROVIDERS, EncryptedKeyStore
from job_hunter.web.workspace import Workspace, flash, workspace

WS = Depends(workspace)


def key_store(request: Request, ws: Workspace) -> EncryptedKeyStore:
    return EncryptedKeyStore(ws.data_dir, request.app.state.fernet)


def keys_context(request: Request, ws: Workspace) -> dict:
    """What the My files page shows about keys and usage, for the signed-in user only."""
    return {
        "keys": key_store(request, ws).status(),
        "usage": usage.summary(ws.settings.db_path),
        "uses_claude": ws.user.admin,
    }


def _provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise HTTPException(404, "unknown provider")
    return provider


def _to_keys() -> RedirectResponse:
    return RedirectResponse("/files#keys", status_code=303)


def install_keys(app: FastAPI) -> None:
    @app.post("/keys/{provider}")
    def save_key(request: Request, provider: str, key: str = Form(""), ws: Workspace = WS):
        label = PROVIDERS[_provider(provider)]
        try:
            key_store(request, ws).set(provider, key)
        except ValueError as exc:
            flash(request, f"{label} key not saved: {exc}", "error")
        else:
            flash(request, f"{label} key saved.")
        return _to_keys()

    @app.post("/keys/{provider}/delete")
    def delete_key(request: Request, provider: str, ws: Workspace = WS):
        label = PROVIDERS[_provider(provider)]
        key_store(request, ws).delete(provider)
        flash(request, f"{label} key removed.")
        return _to_keys()
