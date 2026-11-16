"""Two users on one server never see each other's jobs, files, keys or usage.

Also: every page is behind the per-user workspace, every POST needs the CSRF token, and
only the admin's runs may use the server's Claude.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace

import pytest
from conftest import FakeLLM, arm_csrf, make_job
from cryptography.fernet import Fernet
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from test_multiuser import ADMIN, WIFE, FakeOAuth, cfg, sign_in

from job_hunter import usage
from job_hunter.db import Store
from job_hunter.llm_router import MeteredLLM, OpenAICompatLLM, llm_for, make_llm_factory
from job_hunter.secrets_store import EncryptedKeyStore, make_fernet
from job_hunter.userfiles import export_zip, import_zip
from job_hunter.users import Directory, User
from job_hunter.web.app import create_app
from job_hunter.web.auth import AuthConfig
from job_hunter.web.csrf import verify_csrf
from job_hunter.web.workspace import workspace

PUBLIC = {"/healthz", "/login", "/auth/google", "/auth/callback", "/logout"}
GROQ_KEY = "gsk_partnerSECRETkey0000wxyz"


@pytest.fixture
def two_users(tmp_path):
    oauth = FakeOAuth()
    Directory(tmp_path, ADMIN).add(WIFE, "Partner")
    app = create_app(tmp_path, cfg(), oauth=oauth, llm_factory=lambda *a: None)
    return TestClient(app, base_url="https://testserver"), oauth, app


def _calls(dependant) -> set:
    out = {dependant.call}
    for sub in dependant.dependencies:
        out |= _calls(sub)
    return out


def _dir(app, email):
    directory = app.state.directory
    return directory.user_dir(directory.get(email))


def test_every_route_is_per_user_and_csrf_checked(two_users):
    _, _, app = two_users
    routes = [r for r in app.routes if isinstance(r, APIRoute)]
    assert len(routes) > 20
    for route in routes:
        calls = _calls(route.dependant)
        assert verify_csrf in calls, route.path
        if route.path not in PUBLIC:
            assert workspace in calls, f"{route.path} does not go through workspace()"


def test_csrf_is_required_on_posts(two_users):
    c, oauth, _ = two_users
    sign_in(c, oauth, WIFE)
    good = c.headers.pop("X-CSRF-Token")
    assert c.post("/keys/groq", data={"key": GROQ_KEY}).status_code == 403
    c.headers["X-CSRF-Token"] = "forged"
    assert c.post("/keys/groq", data={"key": GROQ_KEY}).status_code == 403
    del c.headers["X-CSRF-Token"]
    r = c.post("/keys/groq", data={"key": GROQ_KEY, "csrf_token": good})  # the form field
    assert r.status_code == 200 and "Groq key saved" in r.text
    assert c.post("/keys/groq/delete", json={}).status_code == 403  # not a form: no token


def _admin_job_with_pdfs(app) -> int:
    admin_dir = _dir(app, ADMIN)
    pdf = admin_dir / "applications" / "1" / "cv.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-admin")
    (pdf.parent / "cover_letter.pdf").write_bytes(b"%PDF-admin")
    (pdf.parent / "send_failure.png").write_bytes(b"\x89PNG")
    store = Store(admin_dir / "jobs.sqlite")
    job_id, _ = store.upsert(make_job(company="SecretCo"))
    store.update(job_id, cv_path=str(pdf))
    store.close()
    return job_id


def test_jobs_pdfs_and_documents_stay_private(two_users):
    c, oauth, app = two_users
    sign_in(c, oauth, ADMIN)
    job_id = _admin_job_with_pdfs(app)
    assert c.get(f"/jobs/{job_id}/cv.pdf").content == b"%PDF-admin"
    up = {"upload": ("diploma.pdf", b"%PDF-diploma", "application/pdf")}
    c.post("/files/docs", data={"folder": "diplomas"}, files=up)
    assert c.get("/files/docs/diplomas/diploma.pdf").status_code == 200

    sign_in(c, oauth, WIFE)
    for path in ("", "/cv.pdf", "/cover_letter.pdf", "/screenshot.png"):
        assert c.get(f"/jobs/{job_id}{path}").status_code == 404, path
    assert c.post(f"/jobs/{job_id}/status", data={"status": "applied"}).status_code == 404
    assert c.get("/files/docs/diplomas/diploma.pdf").status_code == 404
    for evil in ("..%2F..%2Fboss%2Fjobs.sqlite", "..%2Fsecrets.json"):
        assert c.get(f"/files/docs/diplomas/{evil}").status_code == 404
    assert b"SecretCo" not in c.get("/files/export.zip").content


def test_keys_are_masked_and_per_user(two_users):
    c, oauth, app = two_users
    sign_in(c, oauth, WIFE)
    r = c.post("/keys/groq", data={"key": GROQ_KEY})
    assert "gsk_…wxyz" in r.text and GROQ_KEY not in r.text
    assert "not saved" in c.post("/keys/mistral", data={"key": "two words"}).text
    assert c.post("/keys/openai", data={"key": "x"}).status_code == 404
    assert GROQ_KEY.encode() not in (_dir(app, WIFE) / "secrets.json").read_bytes()

    sign_in(c, oauth, ADMIN)
    page = c.get("/files").text
    assert "gsk_…wxyz" not in page and "Claude first" in page
    assert not (_dir(app, ADMIN) / "secrets.json").exists()

    sign_in(c, oauth, WIFE)
    assert "Groq key removed" in c.post("/keys/groq/delete").text
    assert EncryptedKeyStore(_dir(app, WIFE), app.state.fernet).get("groq") is None


def test_usage_is_per_user_and_admin_sees_all(two_users):
    c, oauth, app = two_users
    sign_in(c, oauth, WIFE)
    usage.record(_dir(app, WIFE) / "jobs.sqlite", "groq", "llama", True, 1234)
    assert "Your AI usage" in c.get("/files").text
    assert c.get("/admin").status_code == 403

    sign_in(c, oauth, ADMIN)
    assert "Your AI usage" not in c.get("/files").text
    admin = c.get("/admin").text
    assert WIFE in admin and "1234" in admin


def test_non_admins_never_get_claude(tmp_path):
    claude = FakeLLM({"": {"from": "claude"}})
    keys = EncryptedKeyStore(tmp_path, make_fernet(Fernet.generate_key().decode()))
    wife, boss = User(WIFE, "", False), User(ADMIN, "", True)
    db = tmp_path / "jobs.sqlite"
    assert llm_for(wife, db, keys, lambda: claude) is None
    admin_llm = llm_for(boss, db, keys, lambda: claude)
    assert admin_llm.json("x", model="m") == {"from": "claude"}
    assert usage.summary(db)[0]["provider"] == "claude"

    keys.set("groq", GROQ_KEY)
    own = llm_for(wife, db, keys, lambda: claude)
    assert isinstance(own, MeteredLLM) and isinstance(own.inner, OpenAICompatLLM)
    assert len(claude.calls) == 1  # only the admin's call
    assert llm_for(boss, db, keys, lambda: None).provider == "groq"
    assert type(llm_for(boss, db, keys, lambda: claude)).__name__ == "ChainLLM"


def test_no_llm_switch_and_factory(tmp_path, monkeypatch):
    fernet = make_fernet(Fernet.generate_key().decode())
    factory = make_llm_factory(fernet, claude=lambda: FakeLLM())
    assert factory(User(WIFE, "", False), tmp_path) is None
    assert factory(User(ADMIN, "", True), tmp_path) is not None
    monkeypatch.setenv("JOB_HUNTER_NO_LLM", "1")
    assert factory(User(ADMIN, "", True), tmp_path) is None


def test_usage_table_keeps_existing_jobs(settings, store):
    job_id, _ = store.upsert(make_job(company="KeptCo"))
    usage.record(settings.db_path, "groq", "llama", True)
    usage.record(settings.db_path, "groq", "llama", False)
    assert store.get(job_id)["company"] == "KeptCo"
    assert usage.summary(settings.db_path)[0]["failed"] == 1


def test_zip_never_carries_keys(tmp_path):
    (tmp_path / "secrets.json").write_text("{}")
    (tmp_path / ".encryption_key").write_text("k")
    (tmp_path / "config.yaml").write_text("a: 1")
    names = zipfile.ZipFile(io.BytesIO(export_zip(tmp_path))).namelist()
    assert names == ["config.yaml"]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data/secrets.json", '{"groq": "planted"}')
        zf.writestr("data/master_cv.yaml", "name: X")
    target = tmp_path / "other"
    target.mkdir()
    assert import_zip(target, buf.getvalue()) == 1
    assert not (target / "secrets.json").exists()


def test_allowed_emails_sign_in_and_cannot_be_removed(tmp_path):
    auth = replace(cfg(), allowed_emails=("friend@example.com",))
    oauth = FakeOAuth()
    app = create_app(tmp_path, auth, oauth=oauth, llm_factory=lambda *a: None)
    c = TestClient(app, base_url="https://testserver")
    assert sign_in(c, oauth, "Friend@Example.com").headers["location"] == "/"
    sign_in(c, oauth, ADMIN)
    page = c.get("/admin").text
    assert "friend@example.com" in page and "set in ALLOWED_EMAILS" in page
    r = c.post("/admin/people/remove", data={"email": "friend@example.com"})
    assert "remove it there" in r.text
    assert "friend@example.com" in [u.email for u in app.state.directory.users()]
    assert "friend@example.com" not in [u.email for u in Directory(tmp_path, ADMIN).users()]


def test_from_env_needs_a_valid_encryption_key():
    env = {
        "GOOGLE_CLIENT_ID": "id",
        "GOOGLE_CLIENT_SECRET": "s",
        "ADMIN_EMAIL": ADMIN,
        "SESSION_SECRET": "x",
        "ALLOWED_EMAILS": "a@x.io; B@x.io",
    }
    with pytest.raises(RuntimeError, match="JOB_HUNTER_ENCRYPTION_KEY"):
        AuthConfig.from_env(env)
    with pytest.raises(ValueError, match="Fernet"):
        AuthConfig.from_env({**env, "JOB_HUNTER_ENCRYPTION_KEY": "nope"})
    key = Fernet.generate_key().decode()
    auth = AuthConfig.from_env({**env, "JOB_HUNTER_ENCRYPTION_KEY": key})
    assert auth.allowed_emails == ("a@x.io", "b@x.io")
    assert auth.fernet("/unused").decrypt(make_fernet(key).encrypt(b"k")) == b"k"


def test_local_mode_keys_page_works(tmp_path):
    c = TestClient(create_app(tmp_path, AuthConfig()))
    arm_csrf(c)
    assert "Groq key saved" in c.post("/keys/groq", data={"key": GROQ_KEY}).text
    assert (tmp_path / ".encryption_key").exists()
