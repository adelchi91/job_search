"""Unit tests for the deployment building blocks: users, files, CV import, mail, daily run."""

from __future__ import annotations

import io
import smtplib
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeLLM

from job_hunter import cli, cvimport, daily, userfiles
from job_hunter.mailer import Mailer, SmtpConfig, digest_message
from job_hunter.userfiles import FileError
from job_hunter.users import Directory, User, ensure_user_dir, slug_for

UTC = ZoneInfo("UTC")


# --- auth config ---------------------------------------------------------------
def test_server_refuses_open_mode_when_auth_is_required():
    from job_hunter.web.auth import AuthConfig

    assert AuthConfig.from_env({}).local
    with pytest.raises(RuntimeError, match="GOOGLE_CLIENT_ID"):
        AuthConfig.from_env({"JOB_HUNTER_REQUIRE_AUTH": "1"})
    with pytest.raises(RuntimeError, match="ADMIN_EMAIL"):
        AuthConfig.from_env({"GOOGLE_CLIENT_ID": "id"})


# --- users ---------------------------------------------------------------------
def test_slug_is_stable_safe_and_case_blind():
    assert slug_for("Jane.Doe+jobs@Example.com") == slug_for(" jane.doe+jobs@example.com ")
    assert slug_for("jane.doe+jobs@example.com").startswith("jane-doe-jobs-")
    assert slug_for("a@x.io") != slug_for("a@y.io")
    assert slug_for("@x.io").startswith("user-")
    assert User("ana@x.io").label == "ana" and User("ana@x.io", "Ana").label == "Ana"


def test_directory_add_remove_and_admin(tmp_path):
    d = Directory(tmp_path, "Admin@X.io")
    assert [u.email for u in d.users()] == ["admin@x.io"] and d.get("ADMIN@x.io").admin
    d.add("b@x.io", " Bea ")
    d.add("b@x.io", "Bea Two")  # re-adding updates, never duplicates
    assert [(u.email, u.name) for u in d.users()] == [("admin@x.io", ""), ("b@x.io", "Bea Two")]
    with pytest.raises(ValueError):
        d.add("not-an-email")
    with pytest.raises(ValueError):
        d.remove("admin@x.io")
    assert d.remove("b@x.io") is True and d.remove("b@x.io") is False
    assert d.user_dir(User("b@x.io")) == tmp_path / "users" / slug_for("b@x.io")


def test_directory_ignores_junk_rows_and_single_dir(tmp_path):
    (tmp_path / "allowlist.yaml").write_text(
        "users:\n  - nope\n  - {name: x}\n  - {email: c@x.io}\n"
    )
    assert [u.email for u in Directory(tmp_path).users()] == ["c@x.io"]
    local = Directory(tmp_path, "", single_dir=tmp_path / "data")
    assert len(local.users()) == 1 and local.user_dir(local.users()[0]) == tmp_path / "data"


def test_ensure_user_dir_creates_defaults_and_folders(tmp_path):
    path = ensure_user_dir(tmp_path / "u")
    assert (path / "config.yaml").exists() and (path / "files" / "diplomas").is_dir()


# --- YAML files ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "text", "msg"),
    [
        ("secrets.yaml", "a: 1", "not an editable"),
        ("config.yaml", "a: [", "syntax"),
        ("config.yaml", "- a list", "mapping"),
        ("config.yaml", "no_such_key: 1", "unexpected content"),
        ("companies.yaml", "other: 1", "companies:"),
        ("companies.yaml", "companies:\n  - {name: A, bogus: 1}", "unexpected content"),
        ("master_cv.yaml", "summary: x", "needs a 'name'"),
        ("master_cv.yaml", "name: A\nexperience: nope", "must be a list"),
    ],
)
def test_validate_yaml_rejects_bad_files(name, text, msg):
    with pytest.raises(FileError, match=msg):
        userfiles.validate_yaml(name, text)


def test_save_keeps_bounded_history_and_undo(tmp_path, monkeypatch):
    ensure_user_dir(tmp_path)
    monkeypatch.setattr(userfiles, "HISTORY_KEEP", 2)
    for i in range(4):
        userfiles.save_yaml(tmp_path, "master_cv.yaml", f"name: V{i}\r\n")
    assert len(userfiles.history(tmp_path, "master_cv.yaml")) == 2
    assert userfiles.read_yaml(tmp_path, "master_cv.yaml") == "name: V3\n"
    assert userfiles.undo_yaml(tmp_path, "master_cv.yaml")
    assert userfiles.read_yaml(tmp_path, "master_cv.yaml") == "name: V2\n"
    with pytest.raises(FileError):
        userfiles.read_yaml(tmp_path, "jobs.sqlite")


# --- documents -------------------------------------------------------------------
def test_doc_names_are_cleaned_and_confined(tmp_path):
    assert userfiles.safe_name("C:\\Users\\me\\My CV (v2).pdf") == "My CV (v2).pdf"
    assert userfiles.safe_name("../../etc/passwd") == "passwd"
    assert userfiles.safe_name("na;me$.pdf") == "na_me_.pdf"
    for bad in ("", "..", " . "):
        with pytest.raises(FileError):
            userfiles.safe_name(bad)
    with pytest.raises(FileError, match="unknown folder"):
        userfiles.doc_path(tmp_path, "..", "x")
    path = userfiles.save_doc(tmp_path, "letters", "a.txt", b"hi")
    assert path == (tmp_path / "files" / "letters" / "a.txt").resolve()
    docs = userfiles.list_docs(tmp_path)
    assert docs["letters"] == [{"name": "a.txt", "size": 2}] and docs["cv"] == []


def test_doc_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(userfiles, "MAX_DOC_BYTES", 3)
    with pytest.raises(FileError, match="15 MB"):
        userfiles.save_doc(tmp_path, "other", "big.bin", b"1234")


# --- zip export / import -----------------------------------------------------------
def _zip(entries: dict[str, bytes], link: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if link:
            info = zipfile.ZipInfo(link)
            info.external_attr = 0xA1FF << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


def test_export_skips_dry_run_and_import_strips_top_folder(tmp_path):
    src = tmp_path / "src"
    (src / "dry-run").mkdir(parents=True)
    (src / "dry-run" / "x.txt").write_text("x")
    (src / "config.yaml").write_text("a: 1")
    names = zipfile.ZipFile(io.BytesIO(userfiles.export_zip(src))).namelist()
    assert names == ["config.yaml"]
    dest = tmp_path / "dest"
    assert userfiles.import_zip(dest, _zip({"data/config.yaml": b"a", "data/files/cv/c.pdf": b"c"}))
    assert (dest / "config.yaml").read_bytes() == b"a" and (dest / "files/cv/c.pdf").exists()
    assert userfiles.import_zip(dest, _zip({"config.yaml": b"b"})) == 1  # contents-only zip
    assert (dest / "config.yaml").read_bytes() == b"b"


@pytest.mark.parametrize(
    "archive",
    [
        _zip({"../evil.txt": b"x"}),
        _zip({"/abs.txt": b"x"}),
        _zip({"ok.txt": b"x"}, link="link"),
    ],
)
def test_import_refuses_unsafe_archives(tmp_path, archive):
    with pytest.raises(FileError, match="unsafe"):
        userfiles.import_zip(tmp_path, archive)


def test_import_refuses_huge_and_non_zip(tmp_path, monkeypatch):
    with pytest.raises(FileError, match="not a zip"):
        userfiles.import_zip(tmp_path, b"nope")
    monkeypatch.setattr(userfiles, "MAX_ZIP_BYTES", 2)
    with pytest.raises(FileError, match="200 MB"):
        userfiles.import_zip(tmp_path, _zip({"a.txt": b"123"}))


def test_resolve_in_reroots_old_paths_and_refuses_escapes(tmp_path):
    cv = tmp_path / "applications" / "00001" / "cv.pdf"
    cv.parent.mkdir(parents=True)
    cv.write_bytes(b"%PDF")
    assert userfiles.resolve_in(tmp_path, "") is None
    assert userfiles.resolve_in(tmp_path, str(cv)) == cv.resolve()
    assert userfiles.resolve_in(tmp_path, "applications/00001/cv.pdf") == cv.resolve()
    old = "/Users/someone/job-hunter/data/applications/00001/cv.pdf"
    assert userfiles.resolve_in(tmp_path, old) == cv.resolve()
    assert userfiles.resolve_in(tmp_path, "/etc/passwd") is None
    assert userfiles.resolve_in(tmp_path, "applications/../../x") is None


# --- CV from PDF ------------------------------------------------------------------
def test_pdf_text_errors():
    from pypdf import PdfWriter

    with pytest.raises(FileError, match="could not be read"):
        cvimport.pdf_text(b"not a pdf")
    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.write(buf)
    with pytest.raises(FileError, match="no text"):
        cvimport.pdf_text(buf.getvalue())


def test_draft_needs_llm_and_valid_output():
    with pytest.raises(FileError, match="no LLM"):
        cvimport.draft_master("text", None, "m")
    with pytest.raises(FileError, match="needs a 'name'"):
        cvimport.draft_master("text", FakeLLM({"Convert": {"summary": "x"}}), "m")


def test_make_apply_and_discard_draft(tmp_path, monkeypatch):
    ensure_user_dir(tmp_path)
    monkeypatch.setattr(cvimport, "pdf_text", lambda content: "Ana, engineer")
    llm = FakeLLM({"Convert": {"name": "Ana", "experience": []}})
    cvimport.make_draft(tmp_path, b"%PDF", llm, "m")
    assert "Ana, engineer" in llm.calls[0] and "name: Ana" in cvimport.read_draft(tmp_path)
    cvimport.apply_draft(tmp_path)
    assert "name: Ana" in userfiles.read_yaml(tmp_path, "master_cv.yaml")
    assert cvimport.read_draft(tmp_path) == "" and userfiles.history(tmp_path, "master_cv.yaml")
    cvimport.discard_draft(tmp_path)  # no draft: still fine


# --- mail -------------------------------------------------------------------------
class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, fail=False):
        self.host, self.port, self.fail, self.logins = host, port, fail, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logins.append(user)

    def send_message(self, msg):
        if self.fail:
            raise smtplib.SMTPException("nope")
        FakeSMTP.sent.append(msg)


def test_smtp_config_from_env():
    assert SmtpConfig.from_env({}) is None
    cfg = SmtpConfig.from_env({"SMTP_HOST": "smtp.x", "SMTP_USER": "me@x.io", "SMTP_PORT": ""})
    assert (cfg.port, cfg.sender) == (587, "me@x.io")
    cfg = SmtpConfig.from_env({"SMTP_HOST": "h", "SMTP_PORT": "465", "SMTP_FROM": "bot@x.io"})
    assert (cfg.port, cfg.sender) == (465, "bot@x.io")


def test_digest_message_subject_and_link():
    msg = digest_message("bot@x.io", "a@x.io", "# 3 new jobs\nbody", "https://jh.example/")
    assert msg["Subject"] == "3 new jobs" and "https://jh.example/" in msg.get_content()
    assert digest_message("b", "a", "")["Subject"] == "Job digest"


def test_mailer_sends_and_swallows_failures():
    FakeSMTP.sent = []
    mailer = Mailer(SmtpConfig("h", user="me@x.io"), smtp_factory=FakeSMTP)
    assert mailer.send_digest("a@x.io", "# Hi", "") is True and len(FakeSMTP.sent) == 1
    broken = Mailer(SmtpConfig("h"), smtp_factory=lambda h, p: FakeSMTP(h, p, fail=True))
    assert broken.send_digest("a@x.io", "# Hi") is False

    def refuse(host, port):
        raise OSError("connection refused")

    assert Mailer(SmtpConfig("h"), smtp_factory=refuse).send_digest("a@x.io", "x") is False


# --- daily run --------------------------------------------------------------------
@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 10, 6, 0, tzinfo=UTC), datetime(2026, 11, 10, 8, 0)),  # Tue 07:00
        (datetime(2026, 11, 10, 7, 0, tzinfo=UTC), datetime(2026, 11, 11, 8, 0)),  # Tue 08:00
        (datetime(2026, 11, 13, 12, 0, tzinfo=UTC), datetime(2026, 11, 16, 8, 0)),  # Fri→Mon
        (datetime(2026, 11, 14, 12, 0, tzinfo=UTC), datetime(2026, 11, 16, 8, 0)),  # Sat→Mon
        (datetime(2026, 3, 27, 9, 0, tzinfo=UTC), datetime(2026, 3, 30, 8, 0)),  # over DST
    ],
)
def test_next_run_is_weekday_eight_paris(now, expected):
    got = daily.next_run(now)
    assert got.replace(tzinfo=None) == expected and got.tzinfo == daily.PARIS


def test_next_run_summer_offset():
    assert daily.next_run(datetime(2026, 7, 1, 5, 0, tzinfo=UTC)).utcoffset().seconds == 7200


def _real_user(tmp_path: Path, email: str) -> tuple[Directory, User]:
    d = Directory(tmp_path, "admin@x.io")
    user = d.add(email) if email != "admin@x.io" else d.get(email)
    ensure_user_dir(d.user_dir(user))
    (d.user_dir(user) / "master_cv.yaml").write_text("name: Real Person\n")
    return d, user


def test_run_everyone_skips_samples_isolates_failures(tmp_path, monkeypatch):
    from job_hunter import pipeline

    d, ok = _real_user(tmp_path, "ok@x.io")
    d.add("boom@x.io")
    ensure_user_dir(d.user_dir(d.get("boom@x.io")))
    (d.user_dir(d.get("boom@x.io")) / "master_cv.yaml").write_text("name: Boom\n")

    def fake_run(settings, store, client, llm, collectors):
        if "boom" in str(settings.db_path):
            raise RuntimeError("board down")
        return SimpleNamespace(digest="# 1 new", new_ids=[1], prepared=[])

    monkeypatch.setattr(pipeline, "run", fake_run)
    FakeSMTP.sent = []
    mailer = Mailer(SmtpConfig("h"), smtp_factory=FakeSMTP)
    results = {r.email: r for r in daily.run_everyone(d, None, mailer, "https://jh")}
    assert results["admin@x.io"].status == "skipped"  # still the sample CV
    assert results["boom@x.io"].status == "failed" and "board down" in results["boom@x.io"].detail
    assert results["ok@x.io"].detail == "1 new, 0 prepared, emailed"
    assert [m["To"] for m in FakeSMTP.sent] == ["ok@x.io"]


def test_run_everyone_respects_lock(tmp_path):
    with daily.RUN_LOCK:
        assert daily.run_everyone(Directory(tmp_path), None) == []


def test_has_real_cv(tmp_path):
    assert not daily.has_real_cv(tmp_path)
    (tmp_path / "master_cv.yaml").write_text("name: Sample Candidate\n")
    assert not daily.has_real_cv(tmp_path)


def test_scheduler_runs_job_then_stops():
    ran = threading.Event()
    clock = iter([datetime(2026, 11, 10, 6, 59, 59, 999999, tzinfo=UTC)] * 2)
    later = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)

    def job():
        ran.set()
        return ["r"]

    sched = daily.DailyScheduler(job, clock=lambda: next(clock, later))
    sched.start()
    assert ran.wait(2)
    sched.stop()
    assert sched.last == ["r"] and sched.next_at is not None


def test_scheduler_survives_a_failing_job():
    calls = []

    def job():
        calls.append(1)
        raise RuntimeError("x")

    sched = daily.DailyScheduler(job, clock=lambda: datetime(2026, 11, 10, 6, 59, tzinfo=UTC))
    sched._stop.wait = lambda t: len(calls) >= 2  # fire immediately, twice, then stop
    sched._loop()
    assert len(calls) == 2 and sched.last == []


# --- CLI --------------------------------------------------------------------------
def test_cli_export_and_run_all(tmp_path, monkeypatch, capsys):
    ensure_user_dir(tmp_path / "data")
    out = tmp_path / "out.zip"
    assert cli.main(["--data", str(tmp_path / "data"), "export", "--out", str(out)]) == 0
    assert "config.yaml" in zipfile.ZipFile(out).namelist()
    fake = [daily.UserRun("a@x.io", "done", "ok"), daily.UserRun("b@x.io", "failed", "x")]
    monkeypatch.setattr(daily, "run_everyone", lambda *a, **k: fake)
    for key in ("GOOGLE_CLIENT_ID", "SMTP_HOST"):
        monkeypatch.delenv(key, raising=False)
    assert cli.main(["--data", str(tmp_path / "data"), "run-all"]) == 1
    assert "b@x.io: failed x" in capsys.readouterr().out


def test_app_from_env_wires_scheduler_and_mail(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_HUNTER_SCHEDULER", "1")
    monkeypatch.setenv("SMTP_HOST", "smtp.example")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    app = cli._app_from_env(tmp_path)
    assert isinstance(app.state.scheduler, daily.DailyScheduler)
    assert app.state.mailer.config.host == "smtp.example"
