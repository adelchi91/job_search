"""Command line: `job-hunter init | run | run-all | serve | export | schedule`."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from job_hunter import fixtures
from job_hunter.collectors import build_collectors
from job_hunter.collectors.base import make_client
from job_hunter.collectors.boards import JobspyCollector
from job_hunter.config import DEFAULTS_DIR, Settings, load_settings
from job_hunter.db import Store
from job_hunter.llm import get_llm
from job_hunter.tailor.cv import load_master

DEFAULT_FILES = ("config.yaml", "companies.yaml", "master_cv.yaml")


def init(data_dir: Path) -> list[Path]:
    """Copy the editable defaults into the data dir without overwriting anything."""
    data_dir.mkdir(parents=True, exist_ok=True)
    created = []
    for name in DEFAULT_FILES:
        target = data_dir / name
        if not target.exists():
            shutil.copy(DEFAULTS_DIR / name, target)
            created.append(target)
    return created


def _dry_run_settings(base: Path) -> Settings:
    """Fresh throwaway data dir; fills a demo profile from the CV so routing can be shown."""
    dry = base / "dry-run"
    if dry.exists():
        shutil.rmtree(dry)
    init(dry)
    for name in DEFAULT_FILES:
        if (base / name).exists():
            shutil.copy(base / name, dry / name)
    settings = load_settings(dry)
    if not settings.profile.email:
        contact = load_master(settings.master_cv_path).get("contact", {})
        settings.profile.name = load_master(settings.master_cv_path).get("name", "")
        settings.profile.email = contact.get("email", "")
    return settings


def post_digest(settings: Settings, digest: str) -> bool:
    """Hand the digest to the configured command (e.g. a board comment). Never fails the run."""
    if not settings.digest_command:
        return False
    try:
        done = subprocess.run([*settings.digest_command, digest], timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logging.getLogger("job_hunter").warning("digest command failed: %s", exc)
        return False
    return done.returncode == 0


def cmd_run(args: argparse.Namespace) -> int:
    base = Path(args.data)
    if args.dry_run:
        settings, llm = _dry_run_settings(base), None
    else:
        init(base)
        settings, llm = load_settings(base), (None if args.no_llm else get_llm())
    transport = fixtures.transport() if args.fixtures else None
    collectors = build_collectors(settings)
    if args.fixtures:  # jobspy does its own HTTP: not replayable from fixtures
        collectors = [c for c in collectors if not isinstance(c, JobspyCollector)]
    from job_hunter.pipeline import run

    store = Store(settings.db_path)
    with make_client(transport) as client:
        result = run(settings, store, client, llm, collectors)
    print(result.digest)
    print(f"Prepared {len(result.prepared)} application(s). Digest: {result.digest_path}")
    print("Nothing was submitted." if args.dry_run else "Open the dashboard: job-hunter serve")
    if not args.dry_run and post_digest(settings, result.digest):
        print("Digest posted.")
    return 0


def cmd_rerender(args: argparse.Namespace) -> int:
    from job_hunter.pipeline import rerender

    settings = load_settings(Path(args.data))
    llm = None if args.no_llm else get_llm()
    done = rerender(Store(settings.db_path), settings, llm)
    print(f"Re-rendered {len(done)} application(s).")
    return 0


def _app_from_env(root: Path):
    """The web app as configured by environment variables (see README, Deploy)."""
    from job_hunter.daily import DailyScheduler, run_everyone
    from job_hunter.mailer import Mailer, SmtpConfig
    from job_hunter.web.app import create_app
    from job_hunter.web.auth import AuthConfig

    auth = AuthConfig.from_env()
    smtp = SmtpConfig.from_env()
    mailer = Mailer(smtp) if smtp else None
    scheduler = None
    if os.environ.get("JOB_HUNTER_SCHEDULER") == "1":
        directory = auth.directory(root)
        scheduler = DailyScheduler(lambda: run_everyone(directory, get_llm(), mailer, auth.app_url))
    return create_app(root, auth, mailer=mailer, scheduler=scheduler)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    root = Path(args.data)
    root.mkdir(parents=True, exist_ok=True)
    app = _app_from_env(root)
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")
    return 0


def cmd_run_all(args: argparse.Namespace) -> int:
    """One pass for every allowed user, as the scheduler does it (server layout)."""
    from job_hunter.daily import run_everyone
    from job_hunter.mailer import Mailer, SmtpConfig
    from job_hunter.web.auth import AuthConfig

    auth = AuthConfig.from_env()
    smtp = SmtpConfig.from_env()
    results = run_everyone(
        auth.directory(Path(args.data)), get_llm(), Mailer(smtp) if smtp else None, auth.app_url
    )
    for r in results:
        print(f"{r.email}: {r.status} {r.detail}")
    return 0 if all(r.status != "failed" for r in results) else 1


def cmd_export(args: argparse.Namespace) -> int:
    from job_hunter.userfiles import export_zip

    out = Path(args.out)
    out.write_bytes(export_zip(Path(args.data)))
    print(f"wrote {out}: import it on the My files page of the deployed app")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    for path in init(Path(args.data)):
        print(f"created {path}")
    print("Edit data/config.yaml (profile, answers) and data/master_cv.yaml (your CV).")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    from job_hunter.schedule import LABEL, install

    path = install(Path.cwd())
    print(f"wrote {path}\nload it with: launchctl bootstrap gui/$(id -u) {path}")
    print(f"remove with:  launchctl bootout gui/$(id -u)/{LABEL}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="job-hunter")
    p.add_argument("--data", default="data", help="data directory (default: ./data)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="collect, score, tailor and route today's jobs")
    r.add_argument("--dry-run", action="store_true", help="throwaway data dir, no LLM, no sends")
    r.add_argument("--fixtures", action="store_true", help="replay recorded API responses")
    r.add_argument("--no-llm", action="store_true", help="use heuristics instead of Claude")
    r.set_defaults(func=cmd_run)
    s = sub.add_parser("serve", help="start the tracker dashboard")
    s.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    s.set_defaults(func=cmd_serve)
    sub.add_parser("run-all", help="run every allowed user's pipeline once").set_defaults(
        func=cmd_run_all
    )
    e = sub.add_parser("export", help="zip the data dir, to import it on the deployed app")
    e.add_argument("--out", default="job-hunter-data.zip")
    e.set_defaults(func=cmd_export)
    rr = sub.add_parser("rerender", help="rebuild prepared CV/letter PDFs with the current look")
    rr.add_argument("--no-llm", action="store_true", help="heuristic tailoring for old items")
    rr.set_defaults(func=cmd_rerender)
    sub.add_parser("init", help="create editable config files").set_defaults(func=cmd_init)
    sub.add_parser("schedule", help="install the weekday 08:00 launchd job").set_defaults(
        func=cmd_schedule
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
