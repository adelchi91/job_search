"""Admin → Claude account: only the admin sees it or can drive the login; nobody else's
actions ever reach the admin's Claude subscription."""

from __future__ import annotations

import json
import time

import pytest
from fake_claude import MANUAL, install
from fastapi.testclient import TestClient
from test_multiuser import ADMIN, WIFE, server, sign_in  # noqa: F401 - fixture

from job_hunter import claude_auth
from job_hunter.userfiles import resolve_in
from job_hunter.web.app import create_app
from job_hunter.web.auth import AuthConfig

ROUTES = [
    ("get", "/admin/claude/status"),
    ("post", "/admin/claude/login"),
    ("post", "/admin/claude/code"),
    ("post", "/admin/claude/cancel"),
    ("post", "/admin/claude/logout"),
]


@pytest.fixture(autouse=True)
def fake_cli(monkeypatch, tmp_path):
    claude_auth._cache.__init__()
    claude_auth._Login.current = None
    folder = tmp_path / "bin"
    folder.mkdir()
    install(folder, monkeypatch)
    yield
    if claude_auth._Login.current:
        claude_auth._Login.current.cancel()


def poll(client, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        data = client.get("/admin/claude/status").json()
        if cond(data):
            return data
        time.sleep(0.05)
    raise AssertionError(f"not reached: {data}")


def test_non_admin_cannot_touch_claude(server):  # noqa: F811
    c, oauth, _ = server
    sign_in(c, oauth, WIFE)
    for method, path in ROUTES:
        kw = {"data": {"code": "x"}} if method == "post" else {}
        r = getattr(c, method)(path, **kw)
        assert r.status_code == 403, path
    assert claude_auth.current() is None  # no login was ever started
    assert 'id="claude"' not in c.get("/").text


def test_signed_out_visitor_is_redirected(server):  # noqa: F811
    c, _, _ = server
    r = c.post("/admin/claude/login", follow_redirects=False)
    assert r.status_code in (303, 401, 403) and claude_auth.current() is None


def test_admin_paste_code_flow(server, monkeypatch):  # noqa: F811
    c, oauth, _ = server
    sign_in(c, oauth, ADMIN)
    page = c.get("/admin").text
    assert 'id="claude"' in page and 'data-local="0"' in page and "/static/claude.js" in page
    first = c.get("/admin/claude/status").json()
    assert first["status"]["label"] == "Not signed in" and first["login"] is None
    assert first["local"] is False
    assert c.post("/admin/claude/code", data={"code": "x"}).status_code == 409
    started = c.post("/admin/claude/login").json()
    assert started["login"]["running"]
    data = poll(c, lambda d: d["login"]["prompted"])
    assert data["login"]["manual_url"] == MANUAL
    assert c.post("/admin/claude/code", data={"code": "a\x1bb"}).status_code == 409
    monkeypatch.setenv("FAKE_CLAUDE_STATUS", json.dumps({"loggedIn": True, "email": ADMIN}))
    c.post("/admin/claude/code", data={"code": "good-code"})
    data = poll(c, lambda d: not d["login"]["running"])
    assert data["login"]["state"] == "succeeded"
    assert data["status"]["logged_in"] and data["status"]["email"] == ADMIN
    assert "good-code" not in json.dumps(data)
    out = c.post("/admin/claude/logout").json()
    assert out["status"]["installed"]


def test_admin_cancel_and_missing_cli(server, monkeypatch):  # noqa: F811
    c, oauth, _ = server
    sign_in(c, oauth, ADMIN)
    monkeypatch.setenv("FAKE_CLAUDE_HANG", "1")
    c.post("/admin/claude/login")
    assert c.post("/admin/claude/cancel").json()["login"]["state"] == "cancelled"
    monkeypatch.setenv("PATH", "/nonexistent")
    assert c.post("/admin/claude/login").status_code == 503
    assert c.get("/admin/claude/status?refresh=1").json()["status"]["installed"] is False


def test_local_mode_admin_gets_panel_and_nav(tmp_path):
    c = TestClient(create_app(tmp_path, AuthConfig(), llm_factory=lambda u: None))
    assert 'href="/admin"' in c.get("/").text
    page = c.get("/admin").text
    assert 'data-local="1"' in page and "Who can sign in" not in page
    assert c.get("/admin/claude/status").json()["local"] is True


def test_claude_config_dir_is_outside_every_user_folder(tmp_path):
    user_dir = tmp_path / "users" / "boss"
    (tmp_path / ".claude").mkdir(parents=True)
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    user_dir.mkdir(parents=True)
    assert resolve_in(user_dir, str(tmp_path / ".claude" / ".credentials.json")) is None
    assert resolve_in(user_dir, "../../.claude/.credentials.json") is None
