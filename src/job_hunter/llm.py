"""Thin LLM layer returning JSON.

Backends: an OpenAI-compatible endpoint (Groq / Mistral with the user's own key, wired up in
`providers.py`), the Anthropic API when ANTHROPIC_API_KEY is set, or the local `claude` CLI
(Claude Code, already signed in). With none of them, callers fall back to deterministic
heuristics."""

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
        msg = self.client.messages.create(
            model=model,
            max_tokens=4096,
            system=system or "Reply with a single JSON object only.",
            messages=[{"role": "user", "content": prompt}],
        )
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

    HTTP errors (401, 429, 5xx) are raised, so callers fall back to their heuristics.
    One 429 is retried after Retry-After (capped); a 400 in JSON mode is retried without it,
    for models that do not support response_format.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        json_mode: bool = True,
        retry_429: bool = True,
        max_wait: float = 10.0,
        max_tokens: int | None = None,
    ) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.client = client or httpx.Client(timeout=120)
        self.json_mode = json_mode
        self.retry_429 = retry_429
        self.max_wait = max_wait
        self.max_tokens = max_tokens
        self.sleep = time.sleep

    def _post(self, body: dict) -> httpx.Response:
        resp = self.client.post(self.url, json=body, headers=self.headers)
        if resp.status_code == 429 and self.retry_429:
            self.sleep(_retry_after(resp, self.max_wait))
            resp = self.client.post(self.url, json=body, headers=self.headers)
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
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"] or ""

    def json(self, prompt: str, *, model: str, system: str = "") -> dict:
        content = self.complete(prompt, model=model, system=system)
        try:
            out = json.loads(content)
        except ValueError:
            return extract_json(content)
        return out if isinstance(out, dict) else extract_json(content)


def get_llm() -> LLM | None:
    if os.environ.get("JOB_HUNTER_NO_LLM"):
        return None
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicLLM()
        except ImportError:
            log.warning("ANTHROPIC_API_KEY set but `anthropic` not installed (uv sync --extra llm)")
    binary = shutil.which("claude")
    return ClaudeCliLLM(binary) if binary else None
