"""Dashboard Settings -> AI provider page."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import arm_csrf
from fastapi.testclient import TestClient

from job_hunter.keystore import ChainKeyStore, EnvKeyStore, FileKeyStore
from job_hunter.providers import GUIDE
from job_hunter.web.app import create_app
from job_hunter.web.auth import AuthConfig

README = Path(__file__).parent.parent / "README.md"


def _client(settings, store, tmp_path, status=200, env=None):
    def handler(request):
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "Invalid API Key"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    keystore = ChainKeyStore(EnvKeyStore(env or {}), FileKeyStore(tmp_path / "keys.yaml"))
    http = httpx.Client(transport=httpx.MockTransport(handler))
    app = create_app(
        settings.data_dir, AuthConfig(), keystore_factory=lambda _dir: keystore, http_client=http
    )
    client = TestClient(app)
    arm_csrf(client, "/settings")
    return client, keystore


def test_page_renders_guide_and_masked_key(settings, store, tmp_path):
    c, ks = _client(settings, store, tmp_path)
    page = c.get("/settings").text
    assert "console.groq.com" in page and "console.mistral.ai" in page
    assert "allow training on my data" in page and "No Groq or Mistral key" in page
    ks.set("groq", "gsk_secretsecret1234")
    page = c.get("/settings").text
    assert "gsk_…1234" in page and "gsk_secretsecret1234" not in page
    assert 'href="/settings"' in c.get("/").text


def test_save_then_test_connection_ok(settings, store, tmp_path):
    c, ks = _client(settings, store, tmp_path)
    r = c.post("/settings/ai/mistral/key", data={"key": "  m-key-123456789  "})
    assert r.status_code == 200 and "Mistral key saved." in r.text
    assert ks.get("mistral") == "m-key-123456789"
    r = c.post("/settings/ai/mistral/test", data={"key": ""})
    assert "OK · mistral-small-latest" in r.text and "m-key-123456789" not in r.text
    assert "All jobs (English and French) → Mistral" in r.text
    assert "Paste a key first." in c.post("/settings/ai/groq/key", data={"key": " "}).text
    r = c.post("/settings/ai/mistral/remove")
    assert "Mistral key removed." in r.text and ks.get("mistral") is None


def test_test_connection_shows_provider_error(settings, store, tmp_path):
    c, _ = _client(settings, store, tmp_path, status=401)
    r = c.post("/settings/ai/groq/test", data={"key": "gsk_wrong"})
    assert "401: Invalid API Key" in r.text and "gsk_wrong" not in r.text


def test_env_key_cannot_be_removed_from_the_page(settings, store, tmp_path):
    c, _ = _client(settings, store, tmp_path, env={"GROQ_API_KEY": "gsk_env_key_12345"})
    page = c.get("/settings").text
    assert "from the environment" in page and "/settings/ai/groq/remove" not in page
    r = c.post("/settings/ai/groq/remove")
    assert "still set in the environment (GROQ_API_KEY)" in r.text


@pytest.mark.parametrize("action", ["key", "remove", "test"])
def test_unknown_provider_is_404(settings, store, tmp_path, action):
    c, _ = _client(settings, store, tmp_path)
    assert c.post(f"/settings/ai/openai/{action}", data={"key": "x"}).status_code == 404


def test_every_guide_step_is_in_the_readme():
    readme = README.read_text(encoding="utf-8")
    for heading, steps in GUIDE:
        assert heading in readme
        for step in steps:
            assert step in readme
