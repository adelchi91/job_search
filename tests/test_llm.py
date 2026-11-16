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


def test_llm_for_is_admin_only(monkeypatch):
    from job_hunter import claude_auth
    from job_hunter.users import User

    admin, other = User("a@x", "", True), User("b@x", "", False)
    signed_in = claude_auth.ClaudeStatus(installed=True, logged_in=True)
    monkeypatch.delenv("JOB_HUNTER_NO_LLM", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(claude_auth, "binary", lambda: "/bin/claude")
    monkeypatch.setattr(claude_auth, "status", lambda force=False: signed_in)
    assert isinstance(llm.llm_for(admin), llm.ClaudeCliLLM)
    assert llm.llm_for(other) is None and llm.llm_for(None) is None
    monkeypatch.setenv("JOB_HUNTER_NO_LLM", "1")
    assert llm.llm_for(admin) is None
    monkeypatch.delenv("JOB_HUNTER_NO_LLM")
    signed_out = claude_auth.ClaudeStatus(installed=True)
    monkeypatch.setattr(claude_auth, "status", lambda force=False: signed_out)
    assert llm.llm_for(admin) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(llm, "AnthropicLLM", lambda: "api")
    assert llm.llm_for(admin) == "api" and llm.llm_for(other) is None


def test_api_llm_without_sdk(monkeypatch):
    def missing():
        raise ImportError

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(llm, "AnthropicLLM", missing)
    assert llm._api_llm() is None


def test_claude_cli_auth_failure_marks_login_expired(monkeypatch):
    from job_hunter import claude_auth

    flagged = []
    monkeypatch.setattr(claude_auth, "mark_auth_failed", lambda: flagged.append(1))

    def refuse(msg):
        def run(cmd, **kw):
            raise subprocess.CalledProcessError(1, cmd, msg, "")

        return run

    monkeypatch.setattr(llm.subprocess, "run", refuse("Invalid API key · Please run /login"))
    with pytest.raises(subprocess.CalledProcessError):
        llm.ClaudeCliLLM("claude").json("p", model="m")
    monkeypatch.setattr(llm.subprocess, "run", refuse("model overloaded"))
    with pytest.raises(subprocess.CalledProcessError):
        llm.ClaudeCliLLM("claude").json("p", model="m")
    assert flagged == [1]
