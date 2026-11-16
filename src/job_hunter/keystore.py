"""Where the user's own LLM API keys live.

Everything that needs a key goes through a `KeyStore`, so the multi-user version can swap in
per-user encrypted storage without touching the providers or the dashboard.
Today: environment variables first (read-only), then `data/keys.yaml` (mode 0600, git-ignored).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

import yaml

ENV_VARS = {"groq": "GROQ_API_KEY", "mistral": "MISTRAL_API_KEY"}


class KeyStore(Protocol):
    def get(self, provider: str) -> str | None: ...

    def set(self, provider: str, key: str) -> None: ...

    def delete(self, provider: str) -> None: ...

    def source(self, provider: str) -> str:
        """Where the key comes from ("env", "file"...), or "" when there is none."""
        ...


class EnvKeyStore:
    """GROQ_API_KEY / MISTRAL_API_KEY. Read-only."""

    def __init__(self, environ: dict | None = None) -> None:
        self.environ = os.environ if environ is None else environ

    def get(self, provider: str) -> str | None:
        var = ENV_VARS.get(provider, "")
        return (self.environ.get(var) or "").strip() or None

    def set(self, provider: str, key: str) -> None:
        raise PermissionError("environment keys are read-only")

    def delete(self, provider: str) -> None:
        raise PermissionError("environment keys are read-only")

    def source(self, provider: str) -> str:
        return "env" if self.get(provider) else ""


class FileKeyStore:
    """A small YAML file readable by its owner only."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        return yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}

    def _write(self, keys: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(keys, fh)
        os.chmod(self.path, 0o600)

    def get(self, provider: str) -> str | None:
        return str(self._read().get(provider) or "").strip() or None

    def set(self, provider: str, key: str) -> None:
        self._write({**self._read(), provider: key.strip()})

    def delete(self, provider: str) -> None:
        keys = self._read()
        if keys.pop(provider, None) is not None:
            self._write(keys)

    def source(self, provider: str) -> str:
        return "file" if self.get(provider) else ""


class ChainKeyStore:
    """Reads from each store in order; writes to the last one."""

    def __init__(self, *stores: KeyStore) -> None:
        self.stores = stores

    def get(self, provider: str) -> str | None:
        return next((k for s in self.stores if (k := s.get(provider))), None)

    def set(self, provider: str, key: str) -> None:
        self.stores[-1].set(provider, key)

    def delete(self, provider: str) -> None:
        self.stores[-1].delete(provider)

    def source(self, provider: str) -> str:
        return next((src for s in self.stores if (src := s.source(provider))), "")


def default_keystore(data_dir: Path) -> KeyStore:
    return ChainKeyStore(EnvKeyStore(), FileKeyStore(Path(data_dir) / "keys.yaml"))
