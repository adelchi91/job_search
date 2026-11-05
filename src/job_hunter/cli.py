"""Command line: `job-hunter init | run | serve | schedule`."""

from __future__ import annotations

import argparse
import logging
import shutil
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
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from job_hunter.web.app import create_app

    settings = load_settings(Path(args.data))
    app = create_app(settings, Store(settings.db_path))
    uvicorn.run(app, host="127.0.0.1", port=args.port)
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
    s = sub.add_parser("serve", help="start the tracker dashboard on localhost")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)
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
