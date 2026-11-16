"""Thin LLM layer returning JSON. Uses the Anthropic API when ANTHROPIC_API_KEY is set,
otherwise the local `claude` CLI (Claude Code, already signed in), otherwise nothing and the
callers fall back to deterministic heuristics.

In the web app the choice is per user (llm_for): the admin's Claude Code subscription only
ever serves the admin; other users get no LLM until they bring their own (JH-65)."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from job_hunter.users import User

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


AUTH_ERROR = re.compile(r"log ?in|authenticat|oauth|credential|\b401\b", re.I)


class ClaudeCliLLM:
    def __init__(self, binary: str) -> None:
        self.binary = binary

    def json(self, prompt: str, *, model: str, system: str = "") -> dict:
        full = (system + "\n\n" if system else "") + prompt + "\n\nReply with one JSON object only."
        try:
            out = subprocess.run(
                [self.binary, "-p", "--output-format", "json", "--model", model],
                input=full,
                capture_output=True,
                text=True,
                timeout=300,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            if AUTH_ERROR.search(f"{exc.stdout or ''} {exc.stderr or ''}"):
                from job_hunter import claude_auth

                claude_auth.mark_auth_failed()
            raise
        envelope = json.loads(out.stdout)
        return extract_json(envelope.get("result", ""))


def get_llm() -> LLM | None:
    if os.environ.get("JOB_HUNTER_NO_LLM"):
        return None
    api = _api_llm()
    if api:
        return api
    binary = shutil.which("claude")
    return ClaudeCliLLM(binary) if binary else None


def _api_llm() -> LLM | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        return AnthropicLLM()
    except ImportError:
        log.warning("ANTHROPIC_API_KEY set but `anthropic` not installed (uv sync --extra llm)")
        return None


def llm_for(user: User | None) -> LLM | None:
    """The LLM a web user's actions may spend. Never the admin's Claude account for anyone else."""
    if os.environ.get("JOB_HUNTER_NO_LLM") or user is None or not user.admin:
        return None  # JH-65: non-admins will bring their own provider
    from job_hunter import claude_auth

    exe = claude_auth.binary()
    if exe and claude_auth.status().logged_in:
        return ClaudeCliLLM(exe)
    return _api_llm()
