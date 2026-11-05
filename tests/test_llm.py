from __future__ import annotations

import json
import subprocess

import pytest

from job_hunter import llm


def test_extract_json_variants():
    assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extract_json('Sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        llm.extract_json("nothing")


def test_get_llm_choices(monkeypatch):
    monkeypatch.setenv("JOB_HUNTER_NO_LLM", "1")
    assert llm.get_llm() is None
    monkeypatch.delenv("JOB_HUNTER_NO_LLM")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/bin/claude")
    assert isinstance(llm.get_llm(), llm.ClaudeCliLLM)
    monkeypatch.setattr(llm.shutil, "which", lambda _: None)
    assert llm.get_llm() is None


def test_claude_cli_parses_envelope(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw["input"]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": '{"score": 70}'}), "")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    assert llm.ClaudeCliLLM("claude").json("p", model="m", system="s") == {"score": 70}
    assert seen["cmd"][-1] == "m" and seen["input"].startswith("s")
