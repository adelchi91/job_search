from __future__ import annotations

from conftest import make_job

from job_hunter.db import Store, job_from_row
from job_hunter.models import Status


def test_upsert_dedupes_across_sources():
    s = Store(":memory:")
    a, new_a = s.upsert(make_job())
    b, new_b = s.upsert(make_job(source="wttj", source_id="w"))
    s.upsert(make_job(source="wttj", source_id="w"))
    assert (a, new_a, new_b) == (b, True, False)
    assert s.get(a)["also_seen_on"] == ["wttj"]


def test_status_counts_and_open_cap():
    s = Store(":memory:")
    i, _ = s.upsert(make_job())
    j, _ = s.upsert(make_job(title="AI Architect", source_id="2"))
    s.update(i, score=80, answers={"Q": "A"})
    s.set_status(i, Status.READY, "ok")
    s.set_status(j, Status.LOW_FIT)
    assert s.open_count("mistral ai") == 1
    assert s.counts() == {"ready": 1, "low_fit": 1}
    assert s.get(i)["answers"] == {"Q": "A"}
    assert [e["kind"] for e in s.events(i)] == ["collected", "status:ready"]
    assert job_from_row(s.by_status(Status.READY)[0]).title == "Forward Deployed Engineer"
    assert s.get(999) is None
