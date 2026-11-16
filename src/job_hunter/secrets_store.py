"""Each user's own LLM API keys (Groq, Mistral), encrypted at rest in their folder.

Keys are stored in <user dir>/secrets.json, encrypted with Fernet under the server key
JOB_HUNTER_ENCRYPTION_KEY (a comma-separated list: the first key encrypts, any of them
decrypts, so keys can be rotated). The web pages only ever show a mask such as gsk_…a1b2.
A value that no longer decrypts (key lost or rotated away) reads as missing, never a crash.

The interface (get / set / delete) is the KeyStore protocol the LLM router uses.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

log = logging.getLogger("job_hunter.secrets")

FILENAME = "secrets.json"
LOCAL_KEY_FILE = ".encryption_key"
PROVIDERS = {"groq": "Groq", "mistral": "Mistral"}
MAX_KEY_LEN = 400


class KeyStore(Protocol):
    def get(self, provider: str) -> str | None: ...
    def set(self, provider: str, key: str) -> None: ...
    def delete(self, provider: str) -> None: ...


def make_fernet(keys: str) -> MultiFernet:
    """MultiFernet from a comma-separated list of Fernet keys. Raises ValueError if unusable."""
    parts = [k.strip() for k in (keys or "").split(",") if k.strip()]
    if not parts:
        raise ValueError("JOB_HUNTER_ENCRYPTION_KEY is empty")
    try:
        return MultiFernet([Fernet(k.encode()) for k in parts])
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "JOB_HUNTER_ENCRYPTION_KEY must be Fernet keys (32 url-safe base64 bytes each)"
        ) from exc


def local_fernet(root: Path) -> MultiFernet:
    """Local mode only: a key kept next to the data, created once with owner-only access."""
    path = Path(root) / LOCAL_KEY_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(path, Fernet.generate_key())
    return make_fernet(path.read_text(encoding="ascii"))


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o600)


def mask(key: str) -> str:
    key = key or ""
    if len(key) <= 10:
        return "…" + key[-2:] if key else ""
    return f"{key[:4]}…{key[-4:]}"


def _check_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}")
    return provider


class EncryptedKeyStore:
    """KeyStore backed by one encrypted JSON file in a user's folder."""

    def __init__(self, data_dir: Path, fernet: MultiFernet):
        self.path = Path(data_dir) / FILENAME
        self.fernet = fernet

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("unreadable %s", self.path)
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, str)} if isinstance(raw, dict) else {}

    def _write(self, rows: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(self.path, json.dumps(rows, indent=1).encode())

    def _decrypt(self, token: str) -> str | None:
        try:
            return self.fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            return None

    def get(self, provider: str) -> str | None:
        token = self._read().get(_check_provider(provider))
        return self._decrypt(token) if token else None

    def set(self, provider: str, key: str) -> None:
        key = (key or "").strip()
        if not key or len(key) > MAX_KEY_LEN or any(c.isspace() for c in key):
            raise ValueError("that does not look like an API key")
        rows = self._read()
        rows[_check_provider(provider)] = self.fernet.encrypt(key.encode()).decode()
        self._write(rows)

    def delete(self, provider: str) -> None:
        rows = self._read()
        if rows.pop(_check_provider(provider), None) is not None:
            self._write(rows)

    def status(self) -> list[dict]:
        """For the page: one row per provider with a mask, never the key itself."""
        rows = self._read()
        out = []
        for provider, label in PROVIDERS.items():
            token = rows.get(provider)
            key = self._decrypt(token) if token else None
            state = "set" if key else ("unreadable" if token else "missing")
            out.append({"provider": provider, "label": label, "state": state, "mask": mask(key)})
        return out
