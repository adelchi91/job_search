"""Encrypted per-user API keys, the Groq/Mistral client, fallback chain and usage metering."""

from __future__ import annotations

import json
import stat

import httpx
import pytest
from conftest import FakeLLM
from cryptography.fernet import Fernet

from job_hunter import usage
from job_hunter.llm_router import ChainLLM, MeteredLLM, OpenAICompatLLM, own_backends
from job_hunter.secrets_store import (
    EncryptedKeyStore,
    local_fernet,
    make_fernet,
    mask,
)

GROQ_KEY = "gsk_abcdefghijklmnopqrstuvwxyz0123"


@pytest.fixture
def fernet():
    return make_fernet(Fernet.generate_key().decode())


def test_round_trip_is_encrypted_and_owner_only(tmp_path, fernet):
    keys = EncryptedKeyStore(tmp_path, fernet)
    assert keys.get("groq") is None
    keys.set("groq", f"  {GROQ_KEY}\n")
    assert keys.get("groq") == GROQ_KEY
    raw = (tmp_path / "secrets.json").read_text()
    assert GROQ_KEY not in raw and "groq" in json.loads(raw)
    assert stat.S_IMODE((tmp_path / "secrets.json").stat().st_mode) == 0o600
    keys.delete("groq")
    keys.delete("groq")  # deleting twice is fine
    assert keys.get("groq") is None


def test_bad_keys_and_providers_are_refused(tmp_path, fernet):
    keys = EncryptedKeyStore(tmp_path, fernet)
    for bad in ("", "has space", "x" * 401):
        with pytest.raises(ValueError, match="API key"):
            keys.set("groq", bad)
    with pytest.raises(ValueError, match="unknown provider"):
        keys.get("openai")


def test_status_masks_and_flags_unreadable(tmp_path, fernet):
    EncryptedKeyStore(tmp_path, fernet).set("groq", GROQ_KEY)
    rows = {r["provider"]: r for r in EncryptedKeyStore(tmp_path, fernet).status()}
    assert rows["groq"]["state"] == "set" and rows["groq"]["mask"] == "gsk_…0123"
    assert rows["mistral"]["state"] == "missing"
    other = make_fernet(Fernet.generate_key().decode())
    lost = EncryptedKeyStore(tmp_path, other)
    assert lost.get("groq") is None and lost.status()[0]["state"] == "unreadable"


def test_rotation_keeps_old_keys_readable(tmp_path):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    EncryptedKeyStore(tmp_path, make_fernet(old)).set("mistral", "m-secret-123456")
    assert EncryptedKeyStore(tmp_path, make_fernet(f"{new}, {old}")).get("mistral")


def test_make_fernet_rejects_garbage():
    with pytest.raises(ValueError, match="empty"):
        make_fernet(" , ")
    with pytest.raises(ValueError, match="Fernet keys"):
        make_fernet("not-a-key")


def test_corrupt_file_reads_as_empty(tmp_path, fernet):
    (tmp_path / "secrets.json").write_text("{nope")
    assert EncryptedKeyStore(tmp_path, fernet).get("groq") is None
    (tmp_path / "secrets.json").write_text("[1, 2]")
    assert EncryptedKeyStore(tmp_path, fernet).status()[0]["state"] == "missing"


def test_local_key_file_is_created_once(tmp_path):
    first = local_fernet(tmp_path)
    token = first.encrypt(b"x")
    assert local_fernet(tmp_path).decrypt(token) == b"x"
    assert stat.S_IMODE((tmp_path / ".encryption_key").stat().st_mode) == 0o600


def test_mask():
    assert mask("") == "" and mask("short") == "…rt" and mask(GROQ_KEY) == "gsk_…0123"


def _groq(handler) -> OpenAICompatLLM:
    return OpenAICompatLLM("groq", GROQ_KEY, transport=httpx.MockTransport(handler))


def test_openai_compatible_client_sends_key_and_reads_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        content = '```json\n{"score": 7}\n```'
        reply = {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 42}}
        return httpx.Response(200, json=reply)

    llm = _groq(handler)
    assert llm.json("rate this", model="claude-x") == {"score": 7}
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["auth"] == f"Bearer {GROQ_KEY}"
    assert seen["body"]["model"] == "llama-3.3-70b-versatile" and llm.last_tokens == 42


def test_metered_llm_records_success_and_failure(tmp_path):
    db = tmp_path / "jobs.sqlite"
    assert usage.summary(db) == []
    ok = _groq(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}))
    MeteredLLM(ok, "groq", db).json("p", model="m")
    down = _groq(lambda r: httpx.Response(429, json={}))
    with pytest.raises(httpx.HTTPStatusError):
        MeteredLLM(down, "groq", db).json("p", model="m")
    [row] = usage.summary(db)
    assert row["provider"] == "groq" and row["calls"] == 2 and row["failed"] == 1 and row["last"]


def test_chain_falls_back_then_raises_last_error():
    first, second = FakeLLM(fail=True), FakeLLM({"": {"ok": True}})
    assert ChainLLM([first, second]).json("anything", model="m") == {"ok": True}
    assert first.calls and second.calls
    with pytest.raises(RuntimeError, match="model down"):
        ChainLLM([FakeLLM(fail=True)]).json("x", model="m")
    with pytest.raises(RuntimeError, match="no LLM backend"):
        ChainLLM([]).json("x", model="m")


def test_own_backends_follow_saved_keys(tmp_path, fernet):
    keys = EncryptedKeyStore(tmp_path, fernet)
    assert own_backends(keys) == []
    keys.set("mistral", "m-secret-123456")
    [(provider, llm)] = own_backends(keys)
    assert provider == "mistral" and llm.model == "mistral-small-latest"
