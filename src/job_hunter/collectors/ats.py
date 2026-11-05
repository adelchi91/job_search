"""Direct careers-site collectors for the watchlist: Greenhouse, Lever, Ashby, Workable.

These hit each company's public job-board JSON API, so they find roles that never reach
LinkedIn or Welcome to the Jungle.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from job_hunter.collectors.base import strip_html
from job_hunter.config import Company
from job_hunter.models import Job


def _greenhouse_host(company: Company) -> str:
    return "boards-api.eu.greenhouse.io" if company.region == "eu" else "boards-api.greenhouse.io"


def _remote_flag(text: str) -> str:
    return "full" if "remote" in (text or "").lower() else ""


def parse_greenhouse(company: Company, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        jobs.append(
            Job(
                source="greenhouse",
                source_id=f"{company.token}:{j['id']}",
                company=company.name,
                title=j.get("title", ""),
                url=j.get("absolute_url", ""),
                apply_url=j.get("absolute_url", ""),
                location=loc,
                remote=_remote_flag(loc),
                description=strip_html(j.get("content", "")),
                published_at=j.get("first_published") or j.get("updated_at", ""),
                ats="greenhouse",
                extra={"board": company.token, "job_id": j["id"], "region": company.region},
            )
        )
    return jobs


def parse_lever(company: Company, data: list) -> list[Job]:
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ", ".join(cats.get("allLocations", []) or [])
        workplace = j.get("workplaceType", "")
        jobs.append(
            Job(
                source="lever",
                source_id=f"{company.token}:{j['id']}",
                company=company.name,
                title=j.get("text", ""),
                url=j.get("hostedUrl", ""),
                apply_url=j.get("applyUrl", ""),
                location=loc,
                remote={"remote": "full", "hybrid": "partial"}.get(workplace, ""),
                description=j.get("descriptionPlain", "") + "\n\n" + _lever_lists(j),
                published_at=str(j.get("createdAt", "")),
                ats="lever",
                extra={"board": company.token, "posting_id": j["id"]},
            )
        )
    return jobs


def _lever_lists(j: dict) -> str:
    parts = []
    for block in j.get("lists", []) or []:
        parts.append(block.get("text", "") + "\n" + strip_html(block.get("content", "")))
    return "\n\n".join(parts).strip()


def parse_ashby(company: Company, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [
            s.get("location", "") for s in j.get("secondaryLocations", []) or []
        ]
        workplace = (j.get("workplaceType") or "").lower()
        remote = "full" if j.get("isRemote") or workplace == "remote" else ""
        jobs.append(
            Job(
                source="ashby",
                source_id=f"{company.token}:{j['id']}",
                company=company.name,
                title=j.get("title", ""),
                url=j.get("jobUrl", ""),
                apply_url=j.get("applyUrl", ""),
                location=", ".join(x for x in locs if x),
                remote=remote or ("partial" if workplace == "hybrid" else ""),
                description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
                published_at=j.get("publishedAt", ""),
                ats="ashby",
                extra={"board": company.token, "job_id": j["id"]},
            )
        )
    return jobs


def parse_workable(company: Company, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        jobs.append(
            Job(
                source="workable",
                source_id=f"{company.token}:{j.get('shortcode', '')}",
                company=company.name,
                title=j.get("title", ""),
                url=j.get("url") or j.get("shortlink", ""),
                apply_url=j.get("application_url", ""),
                location=loc,
                remote="full" if j.get("telecommuting") else "",
                description=strip_html(j.get("description", "")),
                published_at=j.get("published_on", ""),
                ats="workable",
                extra={"board": company.token, "shortcode": j.get("shortcode", "")},
            )
        )
    return jobs


def _url(company: Company) -> str:
    t = company.token
    return {
        "greenhouse": f"https://{_greenhouse_host(company)}/v1/boards/{t}/jobs?content=true",
        "lever": f"https://api.lever.co/v0/postings/{t}?mode=json",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{t}",
        "workable": f"https://apply.workable.com/api/v1/widget/accounts/{t}?details=true",
    }[company.ats]


PARSERS = {
    "greenhouse": parse_greenhouse,
    "lever": parse_lever,
    "ashby": parse_ashby,
    "workable": parse_workable,
}


@dataclass
class AtsCollector:
    company: Company

    @property
    def name(self) -> str:
        return f"{self.company.ats}:{self.company.token}"

    def collect(self, client: httpx.Client) -> list[Job]:
        resp = client.get(_url(self.company))
        resp.raise_for_status()
        return PARSERS[self.company.ats](self.company, resp.json())


def watchlist_collectors(companies: list[Company]) -> list[AtsCollector]:
    return [AtsCollector(c) for c in companies if c.enabled and c.ats in PARSERS and c.token]
