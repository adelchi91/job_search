from __future__ import annotations

import plistlib
import sys

from conftest import FakeLLM, make_job

from job_hunter import cli, fixtures, schedule
from job_hunter.collectors.base import make_client
from job_hunter.db import Store
from job_hunter.digest import build_digest
from job_hunter.models import Status
from job_hunter.pipeline import run


class ListCollector:
    name = "list"

    def __init__(self, jobs):
        self.jobs = jobs

    def collect(self, client):
        return self.jobs


def test_run_scores_tailors_caps_and_digests(settings, store):
    jobs = [make_job(source_id=str(i), title=f"Forward Deployed Engineer {i}") for i in range(3)]
    jobs.append(make_job(source_id="x", title="Accountant"))
    llm = FakeLLM(
        {"Target roles": {"score": 90, "reason": "fit"}, "Language": {"letter": "Dear"}},
    )
    with make_client(fixtures.transport()) as c:
        result = run(settings, store, c, llm, [ListCollector(jobs)])
    assert result.kept == 3 and len(result.new_ids) == 3
    assert len(store.by_status(Status.READY)) == 2  # max 2 open per company
    waiting = store.by_status(Status.MATCH)
    assert waiting and waiting[0]["route_reason"].startswith("Waiting")
    ready = store.by_status(Status.READY)[0]
    assert ready["cover_letter"] == "Dear" and ready["cv_path"].endswith(".pdf")
    assert result.digest_path.exists() and "Ready to send" in result.digest


def test_low_fit_and_prepare_errors_are_isolated(settings, store, monkeypatch):
    from job_hunter import pipeline

    monkeypatch.setattr(
        pipeline, "render_cv", lambda *a: (_ for _ in ()).throw(RuntimeError("typst"))
    )
    jobs = [make_job(), make_job(source_id="2", title="AI Architect", description="zzz")]
    with make_client(fixtures.transport()) as c:
        run(settings, store, c, None, [ListCollector(jobs)])
    assert store.by_status(Status.MATCH)
    assert any(e["kind"] == "error" for e in store.events(1))


def test_digest_lists_errors():
    s = Store(":memory:")
    text = build_digest(s, {"wttj": "error: HTTP 403", "hn": 0}, [])
    assert "wttj" in text and "HTTP 403" in text


def test_cli_dry_run_with_fixtures(tmp_path, capsys):
    assert cli.main(["--data", str(tmp_path), "run", "--dry-run", "--fixtures"]) == 0
    out = capsys.readouterr().out
    assert "Nothing was submitted." in out and "Needs you" in out
    db = Store(tmp_path / "dry-run" / "jobs.sqlite")
    assert db.by_status(Status.READY) and db.by_status(Status.NEEDS_YOU)


def test_cli_init_is_idempotent(tmp_path, capsys):
    cli.main(["--data", str(tmp_path), "init"])
    assert (tmp_path / "master_cv.yaml").exists()
    assert cli.init(tmp_path) == []


def test_schedule_plist_weekdays_at_8(tmp_path):
    path = schedule.install(tmp_path, agents_dir=tmp_path / "agents")
    data = plistlib.loads(path.read_bytes())
    assert [d["Weekday"] for d in data["StartCalendarInterval"]] == [1, 2, 3, 4, 5]
    assert {d["Hour"] for d in data["StartCalendarInterval"]} == {8}
    assert data["ProgramArguments"][-2:] == ["job-hunter", "run"]


def test_cli_schedule(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(schedule, "install", lambda d: tmp_path / "x.plist")
    assert cli.main(["schedule"]) == 0
    assert "launchctl bootstrap" in capsys.readouterr().out


def test_post_digest_runs_configured_command(settings, tmp_path):
    from job_hunter.cli import post_digest

    assert post_digest(settings, "# digest") is False  # nothing configured
    out = tmp_path / "posted.txt"
    settings.digest_command = [
        sys.executable,
        "-c",
        f"import sys; open({str(out)!r}, 'w').write(sys.argv[1])",
    ]
    assert post_digest(settings, "# digest") is True
    assert out.read_text() == "# digest"
    settings.digest_command = ["/nonexistent/command"]
    assert post_digest(settings, "x") is False
