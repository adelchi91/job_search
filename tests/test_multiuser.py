"""Google sign-in by allowlist, per-user isolation, My files and the Admin page."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from conftest import FakeLLM, make_job
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from job_hunter.db import Store
from job_hunter.models import Status
from job_hunter.users import Directory
from job_hunter.web import admin as admin_mod
from job_hunter.web.app import create_app
from job_hunter.web.auth import AuthConfig

ADMIN = "boss@example.com"
WIFE = "partner@example.com"


class FakeGoogle:
    """Stands in for authlib's client: the next callback signs in as `email`."""

    def __init__(self):
        self.email, self.verified, self.fail = ADMIN, True, False

    async def authorize_redirect(self, request, redirect_uri):
        return RedirectResponse(f"https://accounts.example/auth?r={redirect_uri}", 302)

    async def authorize_access_token(self, request):
        if self.fail:
            raise RuntimeError("access_denied")
        return {"userinfo": {"email": self.email, "email_verified": self.verified}}


class FakeOAuth:
    def __init__(self):
        self.google = FakeGoogle()


def cfg() -> AuthConfig:
    return AuthConfig(ADMIN, "s3cret", "client-id", "client-secret", "https://jh.example")


@pytest.fixture
def server(tmp_path: Path):
    oauth = FakeOAuth()
    Directory(tmp_path, ADMIN).add(WIFE, "Partner")
    app = create_app(tmp_path, cfg(), oauth=oauth, llm_factory=lambda: None)
    # https_only cookies need an https base url in the test client
    client = TestClient(app, base_url="https://testserver")
    return client, oauth, app


def sign_in(client, oauth, email):
    oauth.google.email = email
    return client.get("/auth/callback", follow_redirects=False)


def test_pages_need_sign_in(server):
    c, _, _ = server
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "Continue with Google" in c.get("/login").text
    assert c.get("/healthz").status_code == 200
    r = c.get("/auth/google", follow_redirects=False)
    assert "https://jh.example/auth/callback" in r.headers["location"]


def test_allowlisted_user_gets_in_and_can_sign_out(server):
    c, oauth, _ = server
    assert sign_in(c, oauth, WIFE).headers["location"] == "/"
    page = c.get("/").text
    assert "Partner" in page and 'href="/admin"' not in page
    c.post("/logout")
    assert c.get("/", follow_redirects=False).status_code == 303


def test_stranger_and_unverified_are_refused(server):
    c, oauth, _ = server
    r = sign_in(c, oauth, "stranger@example.com")
    assert r.status_code == 403 and "stranger@example.com" in r.text
    oauth.google.verified = False
    assert sign_in(c, oauth, WIFE).status_code == 403
    oauth.google.fail = True
    assert "cancelled or failed" in c.get("/auth/callback").text


def test_removed_user_is_signed_out_at_once(server, tmp_path):
    c, oauth, _ = server
    sign_in(c, oauth, WIFE)
    Directory(tmp_path, ADMIN).remove(WIFE)
    assert c.get("/", follow_redirects=False).status_code == 303


def test_users_never_see_each_other(server, tmp_path):
    c, oauth, app = server
    sign_in(c, oauth, ADMIN)
    c.get("/")
    admin_dir = app.state.directory.user_dir(app.state.directory.get(ADMIN))
    store = Store(admin_dir / "jobs.sqlite")
    job_id, _ = store.upsert(make_job(company="SecretCo"))
    store.set_status(job_id, Status.READY, "test")
    store.close()
    assert "SecretCo" in c.get("/").text
    sign_in(c, oauth, WIFE)
    assert "SecretCo" not in c.get("/").text
    assert c.get(f"/jobs/{job_id}").status_code == 404
    wife_dir = app.state.directory.user_dir(app.state.directory.get(WIFE))
    assert wife_dir != admin_dir and (wife_dir / "files" / "cv").is_dir()


def test_yaml_edit_validation_and_undo(server):
    c, oauth, _ = server
    sign_in(c, oauth, WIFE)
    assert "Profile &amp; search" in c.get("/files").text
    r = c.post("/files/yaml/companies.yaml", data={"text": "companies: [unclosed"})
    assert r.status_code == 400 and "unclosed" in r.text
    good = "companies:\n  - name: Acme\n"
    r = c.post("/files/yaml/companies.yaml", data={"text": good})
    assert "Saved Companies" in r.text
    assert "Acme" in c.get("/files?edit=companies.yaml").text
    r = c.post("/files/yaml/companies.yaml/undo")
    assert "Restored" in r.text and "Acme" not in r.text
    assert "No earlier version" in c.post("/files/yaml/companies.yaml/undo").text
    assert c.post("/files/yaml/secrets.env", data={"text": "x"}).status_code == 404


def test_documents_upload_download_delete(server):
    c, oauth, _ = server
    sign_in(c, oauth, WIFE)
    files = {"upload": ("diploma.pdf", b"%PDF-1.4 hi", "application/pdf")}
    r = c.post("/files/docs", data={"folder": "diplomas"}, files=files)
    assert "Uploaded diploma.pdf" in r.text
    assert c.get("/files/docs/diplomas/diploma.pdf").content == b"%PDF-1.4 hi"
    assert c.get("/files/docs/diplomas/..%2F..%2Fconfig.yaml").status_code == 404
    assert c.get("/files/docs/secret/diploma.pdf").status_code == 404
    r = c.post("/files/docs", data={"folder": "nope"}, files=files)
    assert "unknown folder" in r.text
    assert "Deleted" in c.post("/files/docs/diplomas/diploma.pdf/delete").text
    assert c.get("/files/docs/diplomas/diploma.pdf").status_code == 404
    assert c.post("/files/docs/nope/x.pdf/delete").status_code == 404


def test_export_then_import_round_trip(server):
    c, oauth, _ = server
    sign_in(c, oauth, WIFE)
    data = c.get("/files/export.zip")
    names = zipfile.ZipFile(io.BytesIO(data.content)).namelist()
    assert "config.yaml" in names and data.headers["content-type"] == "application/zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data/files/other/note.txt", "hello")
    up = {"upload": ("d.zip", buf.getvalue(), "application/zip")}
    assert "Imported 1 files" in c.post("/files/import", files=up).text
    assert c.get("/files/docs/other/note.txt").text == "hello"
    bad = {"upload": ("d.zip", b"not a zip", "application/zip")}
    assert "Import failed" in c.post("/files/import", files=bad).text


def test_cv_import_without_llm_explains(server):
    c, oauth, _ = server
    sign_in(c, oauth, WIFE)
    up = {"upload": ("cv.pdf", b"not really a pdf", "application/pdf")}
    r = c.post("/files/cv-import", files=up)
    assert 'class="flash error"' in r.text


def test_cv_import_draft_apply_and_discard(server, monkeypatch):
    c, oauth, app = server
    sign_in(c, oauth, WIFE)
    from job_hunter import cvimport

    master = {"name": "Partner Person", "experience": [{"company": "X", "bullets": []}]}
    monkeypatch.setattr(cvimport, "pdf_text", lambda content: "Partner Person, worked at X")
    app.state.llm_factory = lambda: FakeLLM({"Convert this CV": master})
    up = {"upload": ("cv.pdf", b"%PDF", "application/pdf")}
    r = c.post("/files/cv-import", files=up)
    assert "Draft master CV ready" in r.text and "Partner Person" in r.text
    assert c.get("/files/docs/cv/cv.pdf").status_code == 200
    r = c.post("/files/cv-import/apply", data={"text": "name: [broken"})
    assert "Draft not applied" in r.text
    draft = cvimport.read_draft(app.state.directory.user_dir(app.state.directory.get(WIFE)))
    r = c.post("/files/cv-import/apply", data={"text": draft})
    assert "Master CV replaced" in r.text and "Apply as master CV" not in r.text
    c.post("/files/cv-import", files=up)
    assert "Draft discarded" in c.post("/files/cv-import/discard").text


def test_admin_page_is_admin_only(server):
    c, oauth, _ = server
    sign_in(c, oauth, WIFE)
    assert c.get("/admin").status_code == 403
    assert c.post("/admin/people", data={"email": "x@example.com"}).status_code == 403
    sign_in(c, oauth, ADMIN)
    page = c.get("/admin").text
    assert WIFE in page and "scheduler is off" in page and "off (no SMTP_HOST set)" in page


def test_admin_manages_people(server, tmp_path):
    c, oauth, _ = server
    sign_in(c, oauth, ADMIN)
    assert "can now sign in" in c.post("/admin/people", data={"email": "New@Example.com"}).text
    assert Directory(tmp_path, ADMIN).get("new@example.com") is not None
    assert "not an email" in c.post("/admin/people", data={"email": "nope"}).text
    r = c.post("/admin/people/remove", data={"email": "new@example.com"})
    assert "can no longer sign in" in r.text
    assert "was not on the list" in c.post("/admin/people/remove", data={"email": "q@x.io"}).text
    assert "cannot be removed" in c.post("/admin/people/remove", data={"email": ADMIN}).text


def test_admin_run_now(server, monkeypatch):
    c, oauth, app = server
    sign_in(c, oauth, ADMIN)
    started = []
    monkeypatch.setattr(admin_mod, "start_run", lambda a: started.append(a) or True)
    assert "Run started" in c.post("/admin/run").text and started == [app]
    monkeypatch.setattr(admin_mod, "start_run", lambda a: False)
    assert "already in progress" in c.post("/admin/run").text


def test_start_run_records_results(server, monkeypatch):
    _, _, app = server
    done = []

    def fake_run(directory, llm, mailer, app_url):
        done.append(app_url)
        return ["result"]

    monkeypatch.setattr(admin_mod, "run_everyone", fake_run)
    monkeypatch.setattr(admin_mod.threading, "Thread", _InlineThread)
    assert admin_mod.start_run(app) is True
    assert done == ["https://jh.example"] and app.state.last_manual == ["result"]


class _InlineThread:
    def __init__(self, target, **kw):
        self.target = target

    def start(self):
        self.target()
