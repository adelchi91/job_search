"""Job-board collectors: Welcome to the Jungle (Algolia), LinkedIn/Indeed/Glassdoor via
python-jobspy (read-only, optional), and the monthly Hacker News "Who is hiring" thread."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

from job_hunter.collectors.base import strip_html
from job_hunter.models import Job

log = logging.getLogger("job_hunter.collectors")

# Public search-only key served to every visitor of welcometothejungle.com.
WTTJ_APP = "CSEKHVMS53"
WTTJ_KEY = "4bd8f6215d0cc52b26430765769e65a0"
WTTJ_URL = f"https://{WTTJ_APP.lower()}-dsn.algolia.net/1/indexes/wttj_jobs_production_{{lang}}/query"


def parse_wttj(data: dict) -> list[Job]:
    jobs = []
    for h in data.get("hits", []):
        org = h.get("organization") or {}
        offices = h.get("offices") or []
        loc = "; ".join(
            ", ".join(x for x in (o.get("city"), o.get("country")) if x) for o in offices
        )
        lang = h.get("language", "")
        url = (
            f"https://www.welcometothejungle.com/{lang or 'fr'}/companies/"
            f"{org.get('slug', '')}/jobs/{h.get('slug', '')}"
        )
        desc = "\n\n".join(
            strip_html(h.get(k) or "") for k in ("summary", "key_missions", "profile")
        )
        jobs.append(
            Job(
                source="wttj",
                source_id=str(h.get("reference") or h.get("objectID")),
                company=org.get("name", ""),
                title=h.get("name", ""),
                url=url,
                apply_url=url,
                location=loc,
                remote={"fulltime": "full", "partial": "partial"}.get(h.get("remote") or "", ""),
                description=desc.strip(),
                language=lang,
                published_at=h.get("published_at", ""),
                salary=_wttj_salary(h),
                extra={"contract_type": h.get("contract_type", "")},
            )
        )
    return jobs


def _wttj_salary(h: dict) -> str:
    lo, hi = h.get("salary_minimum"), h.get("salary_maximum")
    if not (lo or hi):
        return ""
    return f"{lo or '?'}-{hi or '?'} {h.get('salary_currency') or ''} {h.get('salary_period') or ''}"


@dataclass
class WttjCollector:
    queries: list[str]
    langs: tuple[str, ...] = ("fr", "en")
    per_query: int = 50
    name: str = "wttj"

    def collect(self, client: httpx.Client) -> list[Job]:
        headers = {
            "X-Algolia-Application-Id": WTTJ_APP,
            "X-Algolia-API-Key": WTTJ_KEY,
            "Referer": "https://www.welcometothejungle.com/",
            "Origin": "https://www.welcometothejungle.com",
        }
        jobs: list[Job] = []
        for lang in self.langs:
            for q in self.queries:
                body = {"query": q, "hitsPerPage": self.per_query}
                resp = client.post(WTTJ_URL.format(lang=lang), json=body, headers=headers)
                resp.raise_for_status()
                jobs.extend(parse_wttj(resp.json()))
        return [j for j in jobs if (j.extra.get("contract_type") or "full_time") != "internship"]


@dataclass
class JobspyCollector:
    """LinkedIn / Indeed / Glassdoor guest search via python-jobspy. Read-only: these jobs
    always land in "Needs you" because applying there needs Adelchi's own session."""

    queries: list[str]
    sites: list[str] = field(default_factory=lambda: ["linkedin", "indeed", "glassdoor"])
    location: str = "Paris, France"
    hours_old: int = 72
    per_query: int = 30
    name: str = "jobspy"

    def collect(self, client: httpx.Client) -> list[Job]:
        try:
            from jobspy import scrape_jobs  # type: ignore[import-not-found]
        except ImportError:
            log.info("python-jobspy not installed; skipping LinkedIn/Indeed/Glassdoor")
            return []
        jobs: list[Job] = []
        for q in self.queries:
            df = scrape_jobs(
                site_name=self.sites,
                search_term=q,
                location=self.location,
                results_wanted=self.per_query,
                hours_old=self.hours_old,
                country_indeed="France",
                linkedin_fetch_description=True,
            )
            jobs.extend(parse_jobspy_records(df.to_dict("records")))
        return jobs


def _s(v) -> str:
    return "" if v is None or (isinstance(v, float) and v != v) else str(v)


def parse_jobspy_records(records: list[dict]) -> list[Job]:
    jobs = []
    for r in records:
        jobs.append(
            Job(
                source=_s(r.get("site")) or "jobspy",
                source_id=_s(r.get("id")) or _s(r.get("job_url")),
                company=_s(r.get("company")),
                title=_s(r.get("title")),
                url=_s(r.get("job_url")),
                apply_url=_s(r.get("job_url_direct")) or _s(r.get("job_url")),
                location=_s(r.get("location")),
                remote="full" if r.get("is_remote") is True else "",
                description=_s(r.get("description")),
                published_at=_s(r.get("date_posted")),
            )
        )
    return jobs


HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"


def parse_hn_comment(c: dict) -> Job | None:
    text = strip_html(c.get("comment_text") or "")
    first = text.split("\n", 1)[0]
    parts = [p.strip() for p in first.split("|")]
    if len(parts) < 3:
        return None
    company, title = parts[0], parts[1]
    location = next((p for p in parts[2:] if re.search(r"remote|paris|france|europe", p, re.I)), "")
    cid = c.get("objectID", "")
    return Job(
        source="hn",
        source_id=str(cid),
        company=company,
        title=title,
        url=f"https://news.ycombinator.com/item?id={cid}",
        location=location or parts[2],
        remote="full" if re.search(r"remote", first, re.I) else "",
        description=text,
        language="en",
        published_at=c.get("created_at", ""),
    )


@dataclass
class HnCollector:
    """Top-level comments of the latest "Ask HN: Who is hiring?" thread."""

    name: str = "hn"

    def collect(self, client: httpx.Client) -> list[Job]:
        story = client.get(
            HN_SEARCH, params={"query": "Ask HN: Who is hiring?", "tags": "story,author_whoishiring"}
        ).json()["hits"][0]
        comments = client.get(
            HN_SEARCH,
            params={"tags": f"comment,story_{story['objectID']}", "hitsPerPage": 1000},
        ).json()["hits"]
        top = [c for c in comments if str(c.get("parent_id")) == str(story["objectID"])]
        return [j for j in map(parse_hn_comment, top) if j]
