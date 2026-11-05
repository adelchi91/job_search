"""Daily markdown digest: new matches, what is ready to send, what needs Adelchi."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from job_hunter.db import Store
from job_hunter.models import Status


def _line(j: dict) -> str:
    score = j["score"] if j["score"] is not None else "–"
    return f"| {score} | {j['company']} | [{j['title']}]({j['url']}) | {j['location'] or '—'} |"


def _table(rows: list[dict], limit: int) -> list[str]:
    if not rows:
        return ["_none_"]
    head = ["| Fit | Company | Role | Location |", "|---|---|---|---|"]
    return head + [_line(j) for j in rows[:limit]]


def build_digest(store: Store, run_report: dict, new_ids: list[int], limit: int = 10) -> str:
    new = [j for j in (store.get(i) for i in new_ids) if j and j["status"] != Status.LOW_FIT]
    new.sort(key=lambda j: -(j["score"] or 0))
    ready = store.by_status(Status.READY)
    needs = store.by_status(Status.NEEDS_YOU)
    errors = {k: v for k, v in run_report.items() if isinstance(v, str)}
    collected = sum(v for v in run_report.values() if isinstance(v, int))
    out = [
        f"## Job digest — {date.today().isoformat()}",
        "",
        f"Collected {collected} postings from {len(run_report)} sources; "
        f"{len(new)} new matches. Ready to send: {len(ready)}. Needs you: {len(needs)}.",
        "",
        "### Top new matches",
        *_table(new, limit),
        "",
        "### Ready to send (approve in the dashboard)",
        *_table(ready, limit),
        "",
        "### Needs you",
        *(
            f"- {j['company']} — [{j['title']}]({j['url']}): {j['route_reason']}"
            for j in needs[:limit]
        ),
    ]
    if not needs:
        out.append("_none_")
    if errors:
        out += ["", "### Sources with errors", *(f"- {k}: {v}" for k, v in errors.items())]
    return "\n".join(out) + "\n"


def write_digest(text: str, data_dir: Path) -> Path:
    path = data_dir / "digests" / f"{date.today().isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
