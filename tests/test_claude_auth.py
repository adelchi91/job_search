"""The admin's Claude CLI account: status parsing and the pty-driven `claude auth login`."""

from __future__ import annotations

import json
import time

import pytest
from fake_claude import LOCAL, MANUAL, install

from job_hunter import claude_auth

SIGNED_IN = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "email": "boss@example.com",
    "orgName": "Boss Org",
    "subscriptionType": "max",
    "loginExpiresAt": "2099-01-01T00:00:00Z",
}


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    claude_auth._cache.__init__()
    claude_auth._Login.current = None
    install(tmp_path, monkeypatch)
    yield
    session = claude_auth._Login.current
    if session:
        session.cancel()


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    raise AssertionError("condition not reached")


# -- status -----------------------------------------------------------------------------


def test_parse_signed_in():
    s = claude_auth.parse_status(SIGNED_IN, "2.1.325")
    assert s.logged_in and not s.expired and s.plan == "Max" and s.org == "Boss Org"
    assert s.label == "Signed in as boss@example.com (Max)"
    assert s.as_dict()["label"] == s.label and s.as_dict()["version"] == "2.1.325"


def test_parse_signed_out_and_expired(tmp_path):
    out = claude_auth.parse_status({"loggedIn": False, "configDirectory": str(tmp_path)})
    assert not out.logged_in and not out.expired and out.label == "Not signed in"
    past = {"loggedIn": False, "loginExpiresAt": "2001-01-01T00:00:00Z"}
    assert claude_auth.parse_status(past).label == "Login expired — re-authenticate"
    (tmp_path / ".credentials.json").write_text("{}")
    lapsed = claude_auth.parse_status({"loggedIn": False, "configDirectory": str(tmp_path)})
    assert lapsed.expired
    refused = claude_auth.parse_status(SIGNED_IN, auth_failed=True)
    assert refused.expired and not refused.logged_in
    assert not claude_auth.parse_status({"loginExpiresAt": "garbage"}).expired
    assert claude_auth.parse_status({"subscriptionType": "custom"}).plan == "Custom"


def test_status_reads_cli_and_caches(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_STATUS", json.dumps(SIGNED_IN))
    s = claude_auth.status()
    assert s.installed and s.logged_in and s.version == "9.9.9"
    monkeypatch.setenv("FAKE_CLAUDE_STATUS", json.dumps({"loggedIn": False}))
    assert claude_auth.status() is s  # cached
    assert not claude_auth.status(force=True).logged_in  # rc=1 when signed out is fine
    claude_auth.mark_auth_failed()
    monkeypatch.setenv("FAKE_CLAUDE_STATUS", json.dumps(SIGNED_IN))
    assert claude_auth.status().expired


def test_status_without_cli_or_bad_json(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert claude_auth.status().label == "Claude CLI not installed"
    assert claude_auth.cli_version() == "" and claude_auth.logout() is False
    with pytest.raises(RuntimeError):
        claude_auth.start_login()


def test_status_tolerates_garbage(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_STATUS", "")
    monkeypatch.setattr(
        claude_auth.subprocess,
        "run",
        lambda *a, **k: claude_auth.subprocess.CompletedProcess(a[0], 1, "not json", ""),
    )
    s = claude_auth.status(force=True)
    assert s.installed and not s.logged_in and s.version == ""


def test_logout_runs_cli():
    assert claude_auth.logout() is True


def test_clean_strips_osc8_and_ansi():
    raw = f"visit: \x1b]8;;{MANUAL}\x07{MANUAL}\x1b]8;;\x07\r\n\x1b[?25hok\r"
    assert claude_auth.clean(raw) == f"visit: {MANUAL}\nok\n"
    assert claude_auth.clean("a\r\r\nb") == "a\nb"


# -- login ------------------------------------------------------------------------------


def test_login_with_pasted_code_succeeds(monkeypatch):
    session = claude_auth.start_login()
    assert claude_auth.start_login() is session  # one run per process
    wait_for(lambda: session.snapshot()["prompted"])
    snap = session.snapshot()
    assert snap["state"] == "waiting" and snap["manual_url"] == MANUAL
    assert snap["local_url"] == LOCAL
    assert any("visit:" in line for line in snap["log"])
    session.submit_code("good-code")
    wait_for(lambda: not session.running)
    snap = session.snapshot()
    assert snap["state"] == "succeeded" and snap["log"][-1] == "Login successful."
    assert "" not in snap["log"]
    assert "good-code" not in "\n".join(snap["log"]) and snap["local_url"] == ""


def test_bad_code_fails_and_is_never_logged():
    session = claude_auth.start_login()
    wait_for(lambda: session.snapshot()["prompted"])
    with pytest.raises(ValueError):
        session.submit_code("two\nlines")
    with pytest.raises(ValueError):
        session.submit_code("   ")
    session.submit_code("wrong-123")
    with pytest.raises(ValueError):
        session.submit_code("again")  # already verifying
    wait_for(lambda: not session.running)
    snap = session.snapshot()
    assert snap["state"] == "failed"
    log = "\n".join(snap["log"])
    assert "wrong-123" not in log and "[code]" in log and "[code submitted]" in log


def test_cancel_and_timeout(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_HANG", "1")
    session = claude_auth.start_login()
    session.cancel()
    wait_for(lambda: session.proc.poll() is not None)
    assert session.snapshot()["state"] == "cancelled"
    session.cancel()  # idempotent
    slow = claude_auth.LoginSession(claude_auth.binary(), timeout=0.5).start()
    wait_for(lambda: not slow.running)
    assert slow.state == "timed_out"


def test_feed_joins_split_crlf_and_masks_partial():
    session = claude_auth.LoginSession("claude")
    session.feed("line one\r")
    session.feed("\nPaste code here if prompted > ")
    assert session.snapshot()["log"] == ["line one", "Paste code here if prompted > "]
    assert session.prompted and session.state == "waiting"
