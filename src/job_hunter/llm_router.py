"""Which model a user's run may use.

The admin runs on Claude (API key or the signed-in `claude` CLI, see llm.get_llm), then on
their own keys. Everyone else only ever uses the Groq or Mistral key they saved themselves
under My files: the server's Claude access is never handed to another user. With no key
the callers fall back to their deterministic heuristics. Every call is counted in the
user's own usage table.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

import httpx

from job_hunter import usage
from job_hunter.llm import LLM, extract_json, get_llm
from job_hunter.secrets_store import EncryptedKeyStore, KeyStore
from job_hunter.users import User

log = logging.getLogger("job_hunter.llm")

# provider -> (OpenAI-compatible base URL, default model). Callers pass Claude model names
# from config.yaml, which these providers do not know, so each uses its own default.
OPENAI_COMPAT = {
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-small-latest"),
}


class OpenAICompatLLM:
    """Chat-completions client for Groq and Mistral (both speak the OpenAI API)."""

    def __init__(self, provider: str, api_key: str, transport=None, timeout: float = 120):
        self.provider = provider
        self.base_url, self.model = OPENAI_COMPAT[provider]
        self.api_key = api_key
        self.transport = transport
        self.timeout = timeout
        self.last_tokens = 0

    def json(self, prompt: str, *, model: str, system: str = "") -> dict:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system or "Reply with a single JSON object only."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
            resp = client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        self.last_tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
        return extract_json(data["choices"][0]["message"]["content"] or "")


class MeteredLLM:
    """Records each call (provider, ok, tokens) in the user's own usage table."""

    def __init__(self, inner: LLM, provider: str, db_path: Path):
        self.inner, self.provider, self.db_path = inner, provider, db_path

    def json(self, prompt: str, *, model: str, system: str = "") -> dict:
        name = getattr(self.inner, "model", model)
        try:
            out = self.inner.json(prompt, model=model, system=system)
        except Exception:
            usage.record(self.db_path, self.provider, name, False)
            raise
        usage.record(self.db_path, self.provider, name, True, getattr(self.inner, "last_tokens", 0))
        return out


class ChainLLM:
    """Tries each backend in turn (rate limit, outage, bad key); raises the last error."""

    def __init__(self, backends: list[LLM]):
        self.backends = backends

    def json(self, prompt: str, *, model: str, system: str = "") -> dict:
        error: Exception | None = None
        for backend in self.backends:
            try:
                return backend.json(prompt, model=model, system=system)
            except Exception as exc:  # noqa: BLE001 - fall through to the next provider
                log.warning("LLM backend failed, trying the next: %s", exc)
                error = exc
        raise error or RuntimeError("no LLM backend")


def own_backends(keys: KeyStore, transport=None) -> list[tuple[str, LLM]]:
    out = []
    for provider in OPENAI_COMPAT:
        key = keys.get(provider)
        if key:
            out.append((provider, OpenAICompatLLM(provider, key, transport)))
    return out


def llm_for(
    user: User,
    db_path: Path,
    keys: KeyStore,
    claude: Callable[[], LLM | None] = get_llm,
    transport=None,
) -> LLM | None:
    """The LLM for one user's run, or None (heuristics only). Never Claude for non-admins."""
    if os.environ.get("JOB_HUNTER_NO_LLM"):
        return None
    backends: list[tuple[str, LLM]] = []
    if user.admin:
        server = claude()
        if server is not None:
            backends.append(("claude", server))
    backends += own_backends(keys, transport)
    metered: list[LLM] = [MeteredLLM(b, p, db_path) for p, b in backends]
    if not metered:
        return None
    return metered[0] if len(metered) == 1 else ChainLLM(metered)


LLMFactory = Callable[[User, Path], LLM | None]


def make_llm_factory(fernet, claude: Callable[[], LLM | None] = get_llm) -> LLMFactory:
    """factory(user, data_dir) -> that user's LLM, reading their keys with the server key."""

    def factory(user: User, data_dir: Path) -> LLM | None:
        keys = EncryptedKeyStore(data_dir, fernet)
        return llm_for(user, Path(data_dir) / "jobs.sqlite", keys, claude)

    return factory
