"""Thin LLM layer returning JSON. Uses the Anthropic API when ANTHROPIC_API_KEY is set,
otherwise the local `claude` CLI (Claude Code, already signed in), otherwise nothing and the
callers fall back to deterministic heuristics."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Protocol

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
