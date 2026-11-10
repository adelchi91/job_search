from __future__ import annotations

from pathlib import Path

from conftest import make_job
from fastapi.testclient import TestClient

from job_hunter.applier import ApplyResult
from job_hunter.models import Status
from job_hunter.web.app import create_app
from job_hunter.web.auth import AuthConfig

OK = ApplyResult(True, "Submitted")


def setup(settings, store, tmp_path, result=OK):
    calls = []

    def fake_apply(url, profile, cv, letter, answers, **kw):
        calls.append((url, cv, letter, answers, kw))
        return result

    job_id, _ = store.upsert(make_job())
    cv = tmp_path / "app" / "cv.pdf"
    cv.parent.mkdir()
    cv.write_bytes(b"%PDF-1.7")
    (cv.parent / "cover_letter.pdf").write_bytes(b"%PDF-1.7")
    store.update(
        job_id,
        cv_path=str(cv),
        cv_diff="-old\n+new",
        cover_letter="Hi",
        score=88,
        answers={"Why us?": ""},
    )
    store.set_status(job_id, Status.NEEDS_YOU, "1 open question")
    return TestClient(create_app(tmp_path, AuthConfig(), fake_apply)), job_id, calls


def test_board_and_detail_render(settings, store, tmp_path):
    c, job_id, _ = setup(settings, store, tmp_path)
    assert "Forward Deployed Engineer" in c.get("/").text
    page = c.get(f"/jobs/{job_id}").text
    assert "Why us?" in page and "+new" in page
    assert c.get(f"/jobs/{job_id}/cv.pdf").content.startswith(b"%PDF")
    assert c.get(f"/jobs/{job_id}/cover_letter.pdf").status_code == 200
    assert c.get("/jobs/999").status_code == 404


def test_answering_promotes_then_send_applies(settings, store, tmp_path):
    c, job_id, calls = setup(settings, store, tmp_path)
    assert c.post(f"/jobs/{job_id}/send").status_code == 409
    c.post(f"/jobs/{job_id}/answers", data={"a:Why us?": "Mission", "cover_letter": "Dear"})
    job = store.get(job_id)
    assert job["status"] == Status.READY and job["cover_letter"] == "Dear"
    c.post(f"/jobs/{job_id}/send")
    assert store.get(job_id)["status"] == Status.APPLIED
    url, cv, letter, answers, kw = calls[0]
    assert kw["screenshot"].name == "send_failure.png"
    assert (
        url.endswith("/apply")
        and answers == {"Why us?": "Mission"}
        and letter.name == "cover_letter.pdf"
    )


def test_failed_send_goes_back_to_needs_you(settings, store, tmp_path):
    c, job_id, _ = setup(settings, store, tmp_path, ApplyResult(False, "CAPTCHA on the form"))
    store.set_status(job_id, Status.READY)
    c.post(f"/jobs/{job_id}/send")
    job = store.get(job_id)
    assert job["status"] == Status.NEEDS_YOU and job["route_reason"] == "CAPTCHA on the form"


def test_manual_status_moves(settings, store, tmp_path):
    c, job_id, _ = setup(settings, store, tmp_path)
    c.post(f"/jobs/{job_id}/status", data={"status": "interview"})
    assert store.get(job_id)["status"] == Status.INTERVIEW
    assert c.post(f"/jobs/{job_id}/status", data={"status": "nope"}).status_code == 400


def test_missing_pdfs_404(settings, store):
    job_id, _ = store.upsert(make_job(source_id="z", title="AI Architect"))
    c = TestClient(create_app(settings.data_dir, AuthConfig()))
    assert c.get(f"/jobs/{job_id}/cv.pdf").status_code == 404
    assert c.get(f"/jobs/{job_id}/cover_letter.pdf").status_code == 404
    assert Path(settings.data_dir).exists()


def test_screenshot_link_and_kpis(settings, store, tmp_path):
    c, job_id, _ = setup(settings, store, tmp_path)
    assert c.get(f"/jobs/{job_id}/screenshot.png").status_code == 404
    (tmp_path / "app" / "send_failure.png").write_bytes(b"\x89PNG")
    assert "See what the form showed" in c.get(f"/jobs/{job_id}").text
    assert c.get(f"/jobs/{job_id}/screenshot.png").headers["content-type"] == "image/png"
    board = c.get("/").text
    assert "Needs you" in board and "In play" in board and "--p: 88" in board


def test_send_without_cv_is_409(settings, store, tmp_path):
    c, job_id, calls = setup(settings, store, tmp_path)
    store.update(job_id, cv_path=str(tmp_path / "gone.pdf"))
    store.set_status(job_id, Status.READY)
    assert c.post(f"/jobs/{job_id}/send").status_code == 409 and not calls


def test_local_mode_has_no_login(settings, tmp_path):
    c = TestClient(create_app(tmp_path, AuthConfig()))
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/login", follow_redirects=False).headers["location"] == "/"
    assert "Sign out" not in c.get("/").text
