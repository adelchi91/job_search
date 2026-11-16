"""Groq / Mistral bring-your-own-key backends, against a mocked OpenAI-compatible server."""

from __future__ import annotations

import json
import os
import stat

import httpx
import pytest
from conftest import make_job

from job_hunter import fixtures, providers
from job_hunter import llm as llm_mod
from job_hunter.collectors.base import make_client
from job_hunter.keystore import ChainKeyStore, EnvKeyStore, FileKeyStore, default_keystore
from job_hunter.llm import OpenAICompatLLM
from job_hunter.models import Status
from job_hunter.pipeline import run
from job_hunter.providers import Backend, LLMRouter, build_router, test_connection
from job_hunter.tailor.cv import load_master, tailor

FR_DESC = "Vous rejoindrez nous pour les clients et les agents avec Python pour les équipes."


def _reply(content) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def _answer(prompt: str):
    if prompt.startswith("Target roles"):
        return {"score": 91, "reason": "strong fit"}
    if prompt.startswith("Language"):
        return {"letter": "Bonjour" if "Language: fr" in prompt else "Dear team"}
    return {"headline": "FDE", "experiences": [], "skills_first": []}


class FakeServer:
    """Records (host, model, prompt) and answers like an OpenAI-compatible endpoint."""

    def __init__(self, status: list[int] | None = None):
        self.calls: list[tuple[str, str, str]] = []
        self.bodies: list[dict] = []
        self.status = list(status or [])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        prompt = body["messages"][-1]["content"]
        self.calls.append((request.url.host, body["model"], prompt))
        if self.status:
            code = self.status.pop(0)
            if code != 200:
                return httpx.Response(code, json={"error": {"message": f"boom {code}"}})
        return _reply(_answer(prompt))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def hosts(self) -> set[str]:
        return {h for h, _, _ in self.calls}


class DictStore:
    def __init__(self, **keys):
        self.keys = keys

    def get(self, provider):
        return self.keys.get(provider)

    def set(self, provider, key):
        self.keys[provider] = key

    def delete(self, provider):
        self.keys.pop(provider, None)

    def source(self, provider):
        return "file" if provider in self.keys else ""


@pytest.fixture(autouse=True)
def no_claude(monkeypatch):
    monkeypatch.delenv("JOB_HUNTER_NO_LLM", raising=False)
    monkeypatch.setattr(llm_mod, "get_llm", lambda: None)


def _run(settings, store, server, jobs, **keys):
    router = build_router(settings, DictStore(**keys), server.client())
    with make_client(fixtures.transport()) as c:
        return run(settings, store, c, router, [_List(jobs)])


class _List:
    name = "list"

    def __init__(self, jobs):
        self.jobs = jobs

    def collect(self, client):
        return self.jobs


EN_JOB = dict(source_id="en", title="Forward Deployed Engineer")
FR_JOB = dict(source_id="fr", title="Forward Deployed Engineer FR", description=FR_DESC)


def test_groq_only_handles_both_languages(settings, store):
    server = FakeServer()
    settings.max_open_per_company = 5
    _run(settings, store, server, [make_job(**EN_JOB), make_job(**FR_JOB)], groq="gsk_x")
    assert server.hosts() == {"api.groq.com"}
    models = {m for _, m, _ in server.calls}
    assert models == {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}
    ready = store.by_status(Status.READY)
    assert len(ready) == 2 and {r["score"] for r in ready} == {91}


def test_mistral_only_handles_both_languages(settings, store):
    server = FakeServer()
    settings.max_open_per_company = 5
    _run(settings, store, server, [make_job(**EN_JOB), make_job(**FR_JOB)], mistral="m_x")
    assert server.hosts() == {"api.mistral.ai"}
    assert {m for _, m, _ in server.calls} == {"mistral-small-latest", "mistral-large-latest"}


def test_both_route_english_to_groq_and_french_to_mistral(settings, store):
    server = FakeServer()
    settings.max_open_per_company = 5
    _run(settings, store, server, [make_job(**EN_JOB), make_job(**FR_JOB)], groq="g", mistral="m")
    for host, _, prompt in server.calls:
        assert host == ("api.mistral.ai" if FR_DESC[:20] in prompt else "api.groq.com")
    assert server.hosts() == {"api.groq.com", "api.mistral.ai"}
    letters = {r["language"]: r["cover_letter"] for r in store.by_status(Status.READY)}
    assert letters["en"].startswith("Dear team") and letters["fr"].startswith("Bonjour")


def test_no_keys_keeps_todays_behaviour(settings, store, monkeypatch):
    server = FakeServer()
    _run(settings, store, server, [make_job(**EN_JOB)])
    assert server.calls == []
    assert store.by_status(Status.MATCH, Status.READY, Status.LOW_FIT)[0][
        "score_reason"
    ].startswith("heuristic")
    claude = object()
    monkeypatch.setattr(llm_mod, "get_llm", lambda: claude)
    router = build_router(settings, DictStore())
    assert router.pick("fr").llm is claude and router.pick("fr").score_model == settings.score_model


def test_prefer_claude_route_override_and_no_llm(settings, monkeypatch):
    claude = object()
    monkeypatch.setattr(llm_mod, "get_llm", lambda: claude)
    settings.llm = {"prefer": "claude"}
    assert build_router(settings, DictStore(groq="g")).pick("en").llm is claude
    monkeypatch.setattr(llm_mod, "get_llm", lambda: None)
    assert build_router(settings, DictStore(groq="g")).pick("en").name == "groq"
    settings.llm = {"route": {"en": "mistral", "fr": "groq"}, "groq": {"score_model": "x"}}
    router = build_router(settings, DictStore(groq="g", mistral="m"))
    assert router.pick("en").name == "mistral" and router.pick("fr").score_model == "x"
    monkeypatch.setenv("JOB_HUNTER_NO_LLM", "1")
    assert build_router(settings, DictStore(groq="g")).pick("en") is None


def test_router_wrap_and_empty():
    assert LLMRouter().for_scoring("en") == (None, "")
    assert LLMRouter().for_tailoring("fr") == (None, "")
    r = LLMRouter(fallback=Backend("x", object(), "s", "t"))
    assert LLMRouter.wrap(r, None) is r
    assert r.for_tailoring("en")[1] == "t"


def _compat(server, **kw) -> OpenAICompatLLM:
    llm = OpenAICompatLLM("https://api.groq.com/openai/v1/", "k", client=server.client(), **kw)
    llm.sleep = lambda s: server.calls.append(("sleep", str(s), ""))
    return llm


def test_429_is_retried_once_then_succeeds():
    server = FakeServer([429, 200])
    out = _compat(server).json("Target roles: x", model="m")
    assert out["score"] == 91 and ("sleep", "1.0", "") in server.calls
    assert server.bodies[0]["response_format"] == {"type": "json_object"}


def test_rate_limits_and_server_errors_fall_back_to_heuristics(settings, store):
    server = FakeServer([429, 429] + [500] * 20)
    settings.max_open_per_company = 5
    _run(settings, store, server, [make_job(**EN_JOB)], groq="g")
    row = store.by_status(Status.MATCH, Status.READY, Status.NEEDS_YOU, Status.LOW_FIT)[0]
    assert row["score_reason"].startswith("heuristic")
    assert row["status"] != Status.READY or row["cover_letter"].startswith("Dear Mistral AI")


def test_prose_reply_goes_through_extract_json():
    llm = OpenAICompatLLM(
        "https://x/v1",
        "k",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: _reply('Sure! ```json\n{"score": 7, "reason": "r"}\n```')
            )
        ),
    )
    assert llm.json("p", model="m") == {"score": 7, "reason": "r"}


def test_400_in_json_mode_retries_without_response_format():
    server = FakeServer([400, 200])
    assert _compat(server).json("Target roles", model="m")["score"] == 91
    assert "response_format" in server.bodies[0] and "response_format" not in server.bodies[1]
    with pytest.raises(httpx.HTTPStatusError):
        _compat(FakeServer([401]), json_mode=False).json("p", model="m")


def test_validate_still_drops_invented_numbers(settings):
    master = load_master(settings.master_cv_path)
    exp = master["experience"][0]
    bullet = exp["bullets"][0]
    proposal = {
        "experiences": [
            {"id": exp["id"], "bullets": [{"id": bullet["id"], "text": "Grew revenue 999%"}]}
        ]
    }
    server = httpx.Client(transport=httpx.MockTransport(lambda r: _reply(proposal)))
    backend = providers.make_backend("groq", "g", client=server)
    out = tailor(master, make_job(), "en", backend.llm, backend.tailor_model)
    texts = [b["text"] for e in out["experiences"] for b in e["bullets"]]
    assert not any("999" in t for t in texts)


def test_test_connection_ok_and_errors():
    ok, msg = test_connection("groq", "g", client=FakeServer().client())
    assert ok and msg.startswith("OK · openai/gpt-oss-20b")
    server = FakeServer([401])
    ok, msg = test_connection("mistral", "bad", client=server.client())
    assert not ok and msg == "401: boom 401" and server.bodies[0]["max_tokens"] == 64
    assert test_connection("nope", "k") == (False, "Unknown provider nope")
    assert test_connection("groq", "")[0] is False

    def down(request):
        raise httpx.ConnectError("no route")

    ok, msg = test_connection("groq", "g", client=httpx.Client(transport=httpx.MockTransport(down)))
    assert not ok and "no route" in msg


@pytest.mark.parametrize(
    ("resp", "text"),
    [
        (httpx.Response(400, text="plain"), "plain"),
        (httpx.Response(400, json={"message": "msg"}), "msg"),
        (httpx.Response(422, json={"detail": "bad"}), "bad"),
        (httpx.Response(400, json=["x"]), "['x']"),
        (httpx.Response(400, json={"error": "flat"}), "flat"),
    ],
)
def test_error_text_shapes(resp, text):
    assert providers._error_text(resp) == text


def test_routing_summary_and_mask():
    assert "heuristics" in providers.routing_summary(DictStore())
    assert "→ Groq." in providers.routing_summary(DictStore(groq="g"))
    both = providers.routing_summary(DictStore(groq="g", mistral="m"), {"prefer": "claude"})
    assert both.startswith("Claude first") and "French jobs → Mistral" in both
    assert providers.mask("gsk_1234567890abcd") == "gsk_…abcd"
    assert providers.mask("short") == "••••" and providers.mask(None) == ""


def test_keystore_env_beats_file_and_file_is_private(tmp_path):
    path = tmp_path / "keys.yaml"
    env = {"GROQ_API_KEY": " env-key "}
    ks = ChainKeyStore(EnvKeyStore(env), FileKeyStore(path))
    ks.set("groq", "file-key")
    ks.set("mistral", "m-key")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert ks.get("groq") == "env-key" and ks.source("groq") == "env"
    assert ks.get("mistral") == "m-key" and ks.source("mistral") == "file"
    ks.delete("mistral")
    ks.delete("mistral")
    assert ks.get("mistral") is None and ks.source("mistral") == ""
    with pytest.raises(PermissionError):
        EnvKeyStore(env).set("groq", "x")
    with pytest.raises(PermissionError):
        EnvKeyStore(env).delete("groq")
    assert default_keystore(tmp_path).get("unknown") is None
