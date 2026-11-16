"""Per-request context: the signed-in user, their data dir, settings and store; page rendering.

Every page resolves data through `workspace(request)`, so one user can never open another
user's jobs or files: each user has their own SQLite file inside their own folder.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from job_hunter.config import Settings, load_settings
from job_hunter.db import Store
from job_hunter.users import User, ensure_user_dir
from job_hunter.web.auth import current_user

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@dataclass
class Workspace:
    user: User
    data_dir: Path
    settings: Settings
    store: Store


class Stores:
    """One open SQLite connection per user dir, opened on first use."""

    def __init__(self) -> None:
        self._open: dict[Path, Store] = {}
        self._lock = threading.Lock()

    def get(self, settings: Settings) -> Store:
        key = settings.db_path.resolve()
        with self._lock:
            if key not in self._open:
                self._open[key] = Store(key)
            return self._open[key]

    def drop(self, data_dir: Path) -> None:
        """Close a user's store, e.g. before an import replaces the database file."""
        key = (data_dir / "jobs.sqlite").resolve()
        with self._lock:
            store = self._open.pop(key, None)
        if store:
            store.close()

    def close_all(self) -> None:
        with self._lock:
            for store in self._open.values():
                store.close()
            self._open.clear()


def workspace(request: Request) -> Workspace:
    """FastAPI dependency: signs-in check, then the user's own folder, settings and store."""
    user = current_user(request)
    state = request.app.state
    data_dir = state.directory.user_dir(user)
    ensure_user_dir(data_dir)
    settings = load_settings(data_dir)  # re-read so YAML edits apply at once
    request.state.user = user
    return Workspace(user, data_dir, settings, state.stores.get(settings))


def flash(request: Request, message: str, tone: str = "ok") -> None:
    request.session["flash"] = {"message": message, "tone": tone}


def render(request: Request, name: str, ctx: dict, status_code: int = 200):
    user = getattr(request.state, "user", None)
    base = {
        "me": user,
        "local": request.app.state.auth.local,
        "flash": request.session.pop("flash", None),
    }
    return TEMPLATES.TemplateResponse(request, name, {**base, **ctx}, status_code=status_code)
