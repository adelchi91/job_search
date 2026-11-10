"""The server-side daily run: every weekday at 08:00 Paris time, each user's pipeline in turn.

Runs inside the web service (JOB_HUNTER_SCHEDULER=1) so it shares the /data volume. A lock
keeps a manual "Run now" and the scheduled run from overlapping.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from job_hunter.collectors.base import make_client
from job_hunter.config import load_settings
from job_hunter.db import Store
from job_hunter.llm import LLM
from job_hunter.mailer import Mailer
from job_hunter.tailor.cv import load_master
from job_hunter.users import Directory, User, ensure_user_dir

log = logging.getLogger("job_hunter.daily")

PARIS = ZoneInfo("Europe/Paris")
RUN_HOUR = 8
WEEKDAYS = range(0, 5)  # Monday..Friday
SAMPLE_NAME = "Sample Candidate"
RUN_LOCK = threading.Lock()


def next_run(now: datetime, hour: int = RUN_HOUR) -> datetime:
    """Next weekday at `hour`:00 Paris time strictly after `now` (any timezone)."""
    local = now.astimezone(PARIS)
    day = local.date()
    for offset in range(8):
        cand = datetime.combine(day + timedelta(days=offset), datetime.min.time(), PARIS)
        cand = cand.replace(hour=hour)
        if cand.weekday() in WEEKDAYS and cand > local:
            return cand
    raise AssertionError("unreachable: a weekday is always within 8 days")


@dataclass
class UserRun:
    email: str
    status: str  # "done" | "skipped" | "failed"
    detail: str = ""


def has_real_cv(data_dir: Path) -> bool:
    try:
        return load_master(data_dir / "master_cv.yaml").get("name") not in ("", SAMPLE_NAME)
    except (OSError, AttributeError, ValueError):
        return False


def run_user(
    user: User,
    data_dir: Path,
    llm: LLM | None,
    mailer: Mailer | None = None,
    app_url: str = "",
    collectors=None,
    transport=None,
) -> UserRun:
    from job_hunter.pipeline import run

    ensure_user_dir(data_dir)
    if not has_real_cv(data_dir):
        return UserRun(user.email, "skipped", "master CV is still the sample")
    settings = load_settings(data_dir)
    store = Store(settings.db_path)
    try:
        with make_client(transport) as client:
            result = run(settings, store, client, llm, collectors)
    finally:
        store.close()
    sent = bool(mailer and mailer.send_digest(user.email, result.digest, app_url))
    note = f"{len(result.new_ids)} new, {len(result.prepared)} prepared"
    return UserRun(user.email, "done", note + (", emailed" if sent else ""))


def run_everyone(directory: Directory, llm: LLM | None, mailer=None, app_url="", **kw) -> list:
    """One pass over every allowed user. Returns [] if another run holds the lock."""
    if not RUN_LOCK.acquire(blocking=False):
        return []
    try:
        results = []
        for user in directory.users():
            try:
                results.append(run_user(user, directory.user_dir(user), llm, mailer, app_url, **kw))
            except Exception as exc:  # noqa: BLE001 - one user's failure never stops the rest
                log.exception("daily run for %s failed", user.email)
                results.append(UserRun(user.email, "failed", str(exc)[:300]))
        return results
    finally:
        RUN_LOCK.release()


class DailyScheduler:
    """Background thread calling `job` at every next_run(). stop() ends it promptly."""

    def __init__(self, job: Callable[[], object], clock: Callable[[], datetime] | None = None):
        self.job = job
        self.clock = clock or (lambda: datetime.now(PARIS))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: list = []
        self.next_at: datetime | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="daily-run", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.next_at = next_run(self.clock())
            wait = (self.next_at - self.clock()).total_seconds()
            if self._stop.wait(max(wait, 0)):
                return
            try:
                self.last = list(self.job() or [])
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("scheduled run failed")
