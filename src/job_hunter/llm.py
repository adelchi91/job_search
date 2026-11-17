"""Thin LLM layer returning JSON.

Backends: an OpenAI-compatible endpoint (Groq / Mistral with the user's own key, wired up in
`providers.py`, or from GROQ_API_KEY / MISTRAL_API_KEY behind a QuotaGuard), the Anthropic API
when ANTHROPIC_API_KEY is set, or the local `claude` CLI (Claude Code, already signed in).
With none of them, callers fall back to deterministic heuristics.
Every API call is recorded in the current usage sink (see usage.py)."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Protocol

import httpx

from job_hunter import usage
from job_hunter.ratelimit import Limiter, QuotaGuard, RateLimited, Slot, parse_rate_headers

log = logging.getLogger("job_hunter.llm")


class LLM(Protocol):
    def json(self, prompt: str, *, model: str, system: str = "") -> dict: ...


def extract_json(text: str) -> dict:
    """Parse the first JSON object in a model reply (tolerates ``` fences and prose)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        return json.loads(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[start : end + 1])


class AnthropicLLM:
    def __init__(self) -> None:
        import anthropic  # optional dependency

        self.client = anthropic.Anthropic()

    def json(self, prompt: str, *, model: str, system: str = "") -> dict:
        try:
            msg = self.client.messages.create(
                model=model,
                max_tokens=4096,
                system=system or "Reply with a single JSON object only.",
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            status = "429" if getattr(exc, "status_code", None) == 429 else "error"
            usage.emit(usage.CallRecord("anthropic", model, status))
            raise
        u = msg.usage
        usage.emit(usage.CallRecord("anthropic", model, "ok", u.input_tokens, u.output_tokens))
        return extract_json("".join(b.text for b in msg.content if b.type == "text"))


class ClaudeCliLLM:
    def __init__(self, binary: str) -> None:
        self.binary = binary

    def json(self, prompt: str, *, model: str, system: str = "") -> dict:
        full = (system + "\n\n" if system else "") + prompt + "\n\nReply with one JSON object only."
        out = subprocess.run(
            [self.binary, "-p", "--output-format", "json", "--model", model],
            input=full,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        envelope = json.loads(out.stdout)
        return extract_json(envelope.get("result", ""))


JSON_SYSTEM = "Reply with a single JSON object only."


def _retry_after(resp: httpx.Response, cap: float) -> float:
    try:
        return max(0.0, min(cap, float(resp.headers.get("retry-after", "1"))))
    except ValueError:
        return min(cap, 1.0)


class OpenAICompatLLM:
    """Any OpenAI-compatible /chat/completions endpoint (Groq, Mistral La Plateforme...).

    HTTP errors (401, 5xx) are raised, so callers fall back to their heuristics.
    One 429 is retried after Retry-After (capped) when `retry_429`; a 429 that remains raises
    RateLimited so a QuotaGuard can retry or fail over. A 400 in JSON mode is retried without
    response_format, for models that do not support it. Every call is recorded in the usage
    sink under `provider`, with the latest rate-limit headers.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        provider: str = "",
        client: httpx.Client | None = None,
        json_mode: bool = True,
        retry_429: bool = True,
        max_wait: float = 10.0,
        max_tokens: int | None = None,
    ) -> None:
        self.provider = provider or httpx.URL(base_url).host or "openai-compat"
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.client = client or httpx.Client(timeout=120)
        self.json_mode = json_mode
        self.retry_429 = retry_429
        self.max_wait = max_wait
        self.max_tokens = max_tokens
        self.sleep = time.sleep
        self.last_state = None
        self.last_tokens = 0

    def _send(self, body: dict) -> httpx.Response:
        try:
            resp = self.client.post(self.url, json=body, headers=self.headers)
        except httpx.HTTPError:
            usage.emit(usage.CallRecord(self.provider, body["model"], "error"))
            raise
        self.last_state = parse_rate_headers(self.provider, resp.headers)
        if self.last_state.windows:
            usage.emit_rate_state(self.provider, body["model"], self.last_state.to_json())
        if resp.status_code == 429:
            usage.emit(usage.CallRecord(self.provider, body["model"], "429"))
        return resp

    def _post(self, body: dict) -> httpx.Response:
        resp = self._send(body)
        if resp.status_code == 429 and self.retry_429:
            self.sleep(_retry_after(resp, self.max_wait))
            resp = self._send(body)
        return resp

    def complete(self, prompt: str, *, model: str, system: str = "") -> str:
        body: dict = {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system or JSON_SYSTEM},
                {"role": "user", "content": prompt},
            ],
        }
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = self._post(body)
        if resp.status_code == 400 and self.json_mode:
            body.pop("response_format")
            resp = self._post(body)
        if resp.status_code == 429:
            state = self.last_state
            raise RateLimited(self.provider, state.retry_after if state else None, state)
        if resp.status_code >= 400:
            usage.emit(usage.CallRecord(self.provider, model, "error"))
            resp.raise_for_status()
        data = resp.json()
        u = data.get("usage") or {}
        tokens_in, tokens_out = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        self.last_tokens = tokens_in + tokens_out
        usage.emit(usage.CallRecord(self.provider, model, "ok", tokens_in, tokens_out))
        return data["choices"][0]["message"]["content"] or ""

    def json(self, prompt: str, *, model: str, system: str = "") -> dict:
        content = self.complete(prompt, model=model, system=system)
        try:
            out = json.loads(content)
        except ValueError:
            return extract_json(content)
        return out if isinstance(out, dict) else extract_json(content)


# provider -> (base URL, API key variable, model variable, default model)
PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "GROQ_MODEL", "openai/gpt-oss-120b"),
    "mistral": (
        "https://api.mistral.ai/v1",
        "MISTRAL_API_KEY",
        "MISTRAL_MODEL",
        "mistral-small-latest",
    ),
}


def configured_providers() -> list[str]:
    return [name for name, (_, key, _, _) in PROVIDERS.items() if os.environ.get(key)]


def default_limits() -> dict:
    from job_hunter.config import DEFAULTS_DIR, _read_yaml

    return _read_yaml(DEFAULTS_DIR / "config.yaml").get("llm_limits") or {}


def quota_guard(llm_limits: dict, client: httpx.Client | None = None, **kw) -> QuotaGuard | None:
    """Groq then Mistral (whichever have a key), throttled to the configured limits."""
    slots = []
    for name in configured_providers():
        base_url, key, model_var, default_model = PROVIDERS[name]
        model = os.environ.get(model_var) or default_model
        caps = usage.limits_for(llm_limits, name, model)
        client_llm = OpenAICompatLLM(
            base_url, os.environ[key], provider=name, client=client, retry_429=False
        )
        slots.append(Slot(name, client_llm, model, Limiter.from_limits(caps)))
    return QuotaGuard(slots, **kw) if slots else None


def claude_llm() -> LLM | None:
    """The Anthropic API when ANTHROPIC_API_KEY is set, else the local `claude` CLI."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicLLM()
        except ImportError:
            log.warning("ANTHROPIC_API_KEY set but `anthropic` not installed (uv sync --extra llm)")
    binary = shutil.which("claude")
    return ClaudeCliLLM(binary) if binary else None


def get_llm(llm_limits: dict | None = None) -> LLM | None:
    if os.environ.get("JOB_HUNTER_NO_LLM"):
        return None
    guard = quota_guard(default_limits() if llm_limits is None else llm_limits)
    if guard is not None:
        return guard
    return claude_llm()
