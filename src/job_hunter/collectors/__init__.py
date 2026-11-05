"""Build the list of collectors for a run from the settings."""

from __future__ import annotations

from job_hunter.collectors.ats import watchlist_collectors
from job_hunter.collectors.base import Collector
from job_hunter.collectors.boards import HnCollector, JobspyCollector, WttjCollector
from job_hunter.config import Settings


def build_collectors(settings: Settings) -> list[Collector]:
    # Watchlist companies with no public ATS API are found by name on the boards.
    by_name = [f"{c.name} AI" for c in settings.companies if c.enabled and not c.ats]
    queries = settings.board_queries + by_name
    return [
        *watchlist_collectors(settings.companies),
        WttjCollector(queries=queries),
        JobspyCollector(queries=queries, sites=settings.jobspy_sites),
        HnCollector(),
    ]
