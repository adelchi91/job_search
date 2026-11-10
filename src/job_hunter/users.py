"""Who may sign in (allowlist.yaml) and where each person's data lives (users/<slug>/).

The admin (ADMIN_EMAIL) is always allowed and manages the list from the Admin page.
Every user directory is a complete data dir: config.yaml, companies.yaml, master_cv.yaml,
jobs.sqlite, applications/, digests/ and files/<folder>/ for personal documents.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

DOC_FOLDERS = ("cv", "diplomas", "letters", "other")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalise(email: str) -> str:
    return (email or "").strip().lower()


def slug_for(email: str) -> str:
    """Stable, filesystem-safe folder name: readable local part plus a short hash."""
    email = normalise(email)
    local = re.sub(r"[^a-z0-9]+", "-", email.split("@")[0]).strip("-") or "user"
    return f"{local[:24]}-{hashlib.sha1(email.encode()).hexdigest()[:6]}"


@dataclass(frozen=True)
class User:
    email: str
    name: str = ""
    admin: bool = False

    @property
    def slug(self) -> str:
        return slug_for(self.email)

    @property
    def label(self) -> str:
        return self.name or self.email.split("@")[0]


class Directory:
    """The allowlist, stored as root/allowlist.yaml: {users: [{email, name}]}."""

    def __init__(self, root: Path, admin_email: str = "", single_dir: Path | None = None):
        self.root = Path(root)
        self.admin_email = normalise(admin_email)
        # local mode: one user whose data dir is the root itself (today's data/ layout)
        self.single_dir = Path(single_dir) if single_dir else None
        self.path = self.root / "allowlist.yaml"
        self._lock = threading.Lock()

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return [u for u in raw.get("users", []) if isinstance(u, dict) and u.get("email")]

    def _write(self, rows: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump({"users": rows}, allow_unicode=True, sort_keys=False)
        self.path.write_text(text, encoding="utf-8")

    def users(self) -> list[User]:
        if self.single_dir:
            return [User(self.admin_email, "", True)]
        rows = {normalise(r["email"]): r.get("name", "") for r in self._read()}
        if self.admin_email:
            rows.setdefault(self.admin_email, "")
        return [User(e, n, e == self.admin_email) for e, n in sorted(rows.items())]

    def get(self, email: str) -> User | None:
        email = normalise(email)
        return next((u for u in self.users() if u.email == email), None)

    def add(self, email: str, name: str = "") -> User:
        email = normalise(email)
        if not EMAIL.match(email):
            raise ValueError(f"not an email address: {email!r}")
        with self._lock:
            rows = [r for r in self._read() if normalise(r["email"]) != email]
            rows.append({"email": email, "name": name.strip()})
            self._write(rows)
        return User(email, name.strip(), email == self.admin_email)

    def remove(self, email: str) -> bool:
        """Revoke access. The data folder is kept, so re-inviting restores everything."""
        email = normalise(email)
        if email == self.admin_email:
            raise ValueError("the admin cannot be removed")
        with self._lock:
            rows = self._read()
            kept = [r for r in rows if normalise(r["email"]) != email]
            self._write(kept)
        return len(kept) != len(rows)

    def user_dir(self, user: User) -> Path:
        if self.single_dir:
            return self.single_dir
        return self.root / "users" / user.slug


def ensure_user_dir(path: Path) -> Path:
    """Create the data dir with the editable defaults and the document folders."""
    from job_hunter.cli import init

    init(path)
    for folder in DOC_FOLDERS:
        (path / "files" / folder).mkdir(parents=True, exist_ok=True)
    return path
