"""Shared collector plumbing: the HTTP client, HTML stripping and the collector protocol."""

from __future__ import annotations

import html
import logging
import re
from typing import Protocol

import httpx

from job_hunter.models import Job

log = logging.getLogger("job_hunter.collectors")

USER_AGENT = "job-hunter/0.1 (personal job search)"


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=30,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        transport=transport,
    )


def strip_html(text: str | list | None) -> str:
    if isinstance(text, list):  # WTTJ sends some sections (key_missions) as a list of items
        text = "\n".join(f"- {t}" for t in text if t)
    text = html.unescape(text or "")
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


class Collector(Protocol):
    name: str

    def collect(self, client: httpx.Client) -> list[Job]: ...


def run_collectors(collectors: list[Collector], client: httpx.Client) -> tuple[list[Job], dict]:
    """Run every collector; one failing source never stops the others."""
    jobs: list[Job] = []
    report: dict[str, str | int] = {}
    for c in collectors:
        try:
            found = c.collect(client)
        except Exception as exc:  # noqa: BLE001 - a broken source is reported, not fatal
            log.warning("collector %s failed: %s", c.name, exc)
            report[c.name] = f"error: {exc.__class__.__name__}: {exc}"[:200]
            continue
        report[c.name] = len(found)
        jobs.extend(found)
    return jobs, report
