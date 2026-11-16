"""Bring-your-own-key LLM providers (Groq, Mistral) and the per-language routing.

- Groq and Mistral configured: English jobs go to Groq, French jobs to Mistral (`llm.route`).
- Only one configured: it handles every language.
- Neither (or `llm.prefer: claude` with Claude available): Claude API / CLI, else heuristics.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

from job_hunter import llm as llm_mod
from job_hunter import usage
from job_hunter.config import Settings
from job_hunter.keystore import KeyStore
from job_hunter.llm import LLM, OpenAICompatLLM
from job_hunter.ratelimit import Limiter, QuotaGuard, RateLimited, Slot


@dataclass(frozen=True)
class Preset:
    name: str
    label: str
    base_url: str
    score_model: str
    tailor_model: str
    console_url: str


PRESETS = {
    "groq": Preset(
        "groq",
        "Groq",
        "https://api.groq.com/openai/v1",
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
        "https://console.groq.com",
    ),
    "mistral": Preset(
        "mistral",
        "Mistral",
        "https://api.mistral.ai/v1",
        "mistral-small-latest",
        "mistral-large-latest",
        "https://console.mistral.ai",
    ),
}
DEFAULT_ROUTE = {"en": "groq", "fr": "mistral"}

# Shown in the dashboard (Settings -> AI provider) and copied verbatim into the README
# (tests/test_ai_settings.py checks they stay in sync).
GUIDE = [
    (
        "Groq (free, very fast; used for English jobs when both are set)",
        [
            "Create a Groq account at https://console.groq.com",
            "Open API Keys, then click Create key and copy it (it starts with gsk_).",
        ],
    ),
    (
        "Mistral (free plan; used for French jobs when both are set)",
        [
            "Create a Mistral account at https://console.mistral.ai",
            "Choose the free plan (Experiment), then open API Keys and click Create key.",
            'In Admin, turn off "allow training on my data".',
        ],
    ),
    (
        "Connect",
        [
            "Paste the key(s) below and click Save, then click Test connection: "
            "it makes one tiny call and shows OK or the provider's error.",
            "Or set GROQ_API_KEY / MISTRAL_API_KEY in the environment instead.",
        ],
    ),
    (
        "Good to know",
        [
            "Free tiers are rate-limited: on busy days some jobs fall back to the "
            "keyword heuristics and the template letter.",
            "Check the data-training opt-out of each provider before sending your CV.",
        ],
    ),
]


@dataclass
class Backend:
    name: str
    llm: LLM
    score_model: str
    tailor_model: str


class LLMRouter:
    """Picks the backend for a job language."""

    def __init__(
        self,
        backends: dict[str, Backend] | None = None,
        route: dict[str, str] | None = None,
        fallback: Backend | None = None,
    ) -> None:
        self.backends = backends or {}
        self.route = {**DEFAULT_ROUTE, **(route or {})}
        self.fallback = fallback

    @classmethod
    def wrap(cls, llm: LLMRouter | LLM | None, settings: Settings) -> LLMRouter:
        """A plain LLM (Claude, a test fake) or None, as a router using the Claude models."""
        if isinstance(llm, LLMRouter):
            return llm
        if llm is None:
            return cls()
        return cls(fallback=Backend("claude", llm, settings.score_model, settings.tailor_model))

    def pick(self, lang: str) -> Backend | None:
        if not self.backends:
            return self.fallback
        if len(self.backends) == 1:
            return next(iter(self.backends.values()))
        name = self.route.get(lang or "en") or self.route["en"]
        return self.backends.get(name) or next(iter(self.backends.values()))

    def for_scoring(self, lang: str) -> tuple[LLM | None, str]:
        b = self.pick(lang)
        return (b.llm, b.score_model) if b else (None, "")

    def for_tailoring(self, lang: str) -> tuple[LLM | None, str]:
        b = self.pick(lang)
        return (b.llm, b.tailor_model) if b else (None, "")


def make_backend(
    name: str, key: str, cfg: dict | None = None, client: httpx.Client | None = None, **kw
) -> Backend:
    """`cfg` is the provider's block of `llm:` in config.yaml (models, base_url, json_mode)."""
    preset, cfg = PRESETS[name], cfg or {}
    llm = OpenAICompatLLM(
        cfg.get("base_url", preset.base_url),
        key,
        provider=name,
        client=client,
        json_mode=cfg.get("json_mode", True),
        **kw,
    )
    return Backend(
        name,
        llm,
        cfg.get("score_model", preset.score_model),
        cfg.get("tailor_model", preset.tailor_model),
    )


def guarded(backend: Backend, llm_limits: dict | None, retry_429: bool = True) -> Backend:
    """Throttle a backend to its free-tier limits (`llm_limits`). A 429 is retried once (when
    `retry_429`), then the job falls back to heuristics / templates, flagged "quota reached"."""
    caps = usage.limits_for(llm_limits or {}, backend.name)
    slot = Slot(backend.name, backend.llm, "", Limiter.from_limits(caps))
    guard = QuotaGuard([slot], retry_429=retry_429)
    return Backend(backend.name, guard, backend.score_model, backend.tailor_model)


def build_router(
    settings: Settings, keystore: KeyStore, client: httpx.Client | None = None
) -> LLMRouter:
    if os.environ.get("JOB_HUNTER_NO_LLM"):
        return LLMRouter()
    cfg = settings.llm or {}
    retry = bool(cfg.get("retry_429", True))
    backends = {
        name: guarded(
            make_backend(name, key, cfg.get(name), client, retry_429=False),
            settings.llm_limits,
            retry,
        )
        for name in PRESETS
        if (key := keystore.get(name))
    }
    if not backends or cfg.get("prefer") == "claude":
        claude = llm_mod.get_llm()
        if isinstance(claude, QuotaGuard):  # env Groq/Mistral keys: want Claude itself here
            claude = llm_mod.claude_llm()
        if claude is not None or not backends:
            return LLMRouter.wrap(claude, settings)
    return LLMRouter(backends, cfg.get("route"))


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message", err))[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("detail") or err or body)[:200]
    return str(body)[:200]


def test_connection(
    name: str, key: str, cfg: dict | None = None, client: httpx.Client | None = None
) -> tuple[bool, str]:
    """One tiny call with the scoring model. Returns (ok, message for the user)."""
    if name not in PRESETS:
        return False, f"Unknown provider {name}"
    if not key:
        return False, "No key: paste one first."
    b = make_backend(name, key, cfg, client, retry_429=False, max_tokens=64)
    start = time.monotonic()
    try:
        b.llm.complete('Reply with {"ok": true}', model=b.score_model)
    except httpx.HTTPStatusError as exc:
        return False, f"{exc.response.status_code}: {_error_text(exc.response)}"
    except RateLimited as exc:
        return False, f"429: {exc}"
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    return True, f"OK · {b.score_model} · {int((time.monotonic() - start) * 1000)} ms"


test_connection.__test__ = False  # not a pytest test despite its name


def routing_summary(keystore: KeyStore, cfg: dict | None = None) -> str:
    """Plain-language description of which provider gets which jobs."""
    cfg = cfg or {}
    names = [n for n in PRESETS if keystore.get(n)]
    if not names:
        return "No Groq or Mistral key: Claude if available, else keyword heuristics."
    prefix = (
        "Claude first if available (llm.prefer: claude), else: "
        if (cfg.get("prefer") == "claude")
        else ""
    )
    if len(names) == 1:
        return f"{prefix}All jobs (English and French) → {PRESETS[names[0]].label}."
    route = {**DEFAULT_ROUTE, **(cfg.get("route") or {})}
    en, fr = (PRESETS.get(route[k], PRESETS["groq"]).label for k in ("en", "fr"))
    return f"{prefix}English jobs → {en}, French jobs → {fr}."


def mask(key: str | None) -> str:
    if not key:
        return ""
    return f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "••••"
