"""Check browser-produced forms, not just test clients with injected headers."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from job_hunter.models import Status
from job_hunter.web.app import create_app
from job_hunter.web.auth import AuthConfig


def forms(page):
    return re.findall(r"<form\b[^>]*>.*?</form>", page, re.S)


def test_settings_forms_carry_the_session_token(tmp_path):
    client = TestClient(create_app(tmp_path, AuthConfig()))
    page = client.get("/settings").text
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    for form in forms(page):
        assert f'name="csrf_token" value="{token}"' in form
    response = client.post("/settings/ai/groq/key", data={"csrf_token": token, "key": "test-key"})
    assert response.status_code == 200
    remove = next(f for f in forms(response.text) if "/groq/remove" in f)
    assert f'name="csrf_token" value="{token}"' in remove
    assert client.post("/settings/ai/groq/remove", data={"csrf_token": token}).status_code == 200
    assert client.post("/settings/ai/groq/remove").status_code == 403


def test_rerun_and_claude_forms_carry_tokens(settings, store):
    from conftest import make_job

    job_id, _ = store.upsert(make_job())
    store.update(job_id, ai_fallback="quota")
    store.set_status(job_id, Status.READY, "ready")
    client = TestClient(create_app(settings.data_dir, AuthConfig()))
    for path in (f"/jobs/{job_id}", "/admin"):
        page = client.get(path).text
        for form in forms(page):
            assert 'name="csrf_token"' in form, form


def test_claude_script_sends_csrf_for_every_action():
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the dashboard JavaScript regression test")
    script = Path("src/job_hunter/web/static/claude.js").resolve()
    harness = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const handlers = {};
const requests = [];
const root = {
  dataset: {local: "0"},
  querySelector(selector) {
    return {addEventListener(event, handler) {handlers[selector] = handler;}};
  }
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), {
  document: {
    getElementById() {return root;},
    querySelector() {return {content: "session-token"};}
  },
  FormData: class {},
  confirm: () => true,
  fetch(path, options) {
    requests.push({path, options});
    return new Promise(() => {});
  }
});
for (const action of ["login", "cancel", "logout", "code"]) {
  handlers['[data-act="' + action + '"]']({
    preventDefault() {},
    target: {reset() {}}
  });
}
assert.equal(requests.length, 5);
assert.equal(requests[0].options.method, undefined);
for (const {options} of requests.slice(1)) {
  assert.equal(options.method, "POST");
  assert.equal(options.headers["X-CSRF-Token"], "session-token");
}
"""
    subprocess.run([node, "-e", harness, str(script)], check=True)
