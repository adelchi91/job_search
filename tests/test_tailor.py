from __future__ import annotations

from conftest import FakeLLM, make_job

from job_hunter.config import DEFAULTS_DIR
from job_hunter.tailor.cover import cover_letter
from job_hunter.tailor.cv import (
    MAX_BULLETS,
    build_document,
    cv_diff,
    default_tailoring,
    heuristic_tailoring,
    load_master,
    tailor,
    to_text,
    tr,
    validate,
)
from job_hunter.tailor.render import render_cv, render_letter

MASTER = load_master(DEFAULTS_DIR / "master_cv.yaml")


def bullet(eid, bid):
    exp = next(e for e in MASTER["experience"] if e["id"] == eid)
    return next(b for b in exp["bullets"] if b["id"] == bid)


def test_tr():
    assert tr({"en": "a", "fr": "b"}, "fr") == "b"
    assert tr({"en": "a"}, "fr") == "a"
    assert tr("plain", "fr") == "plain" and tr(None, "en") == ""


def test_validate_never_invents():
    proposal = {
        "headline": "FDE",
        "skills_first": ["python", "Quantum Knitting"],
        "experiences": [
            {
                "id": "acme",
                "bullets": [
                    {"id": "acme-2", "text": "Shipped it to 9999 users"},
                    {"id": "made-up", "text": "Invented"},
                    {"id": "acme-1", "text": "Reworded, no new numbers"},
                ],
            },
            {"id": "fake-co", "bullets": [{"id": "x", "text": "Never worked here"}]},
        ],
    }
    out = validate(MASTER, proposal, "en")
    acme = next(e for e in out["experiences"] if e["id"] == "acme")
    assert [b["id"] for b in acme["bullets"]] == ["acme-2", "acme-1"]
    assert acme["bullets"][0]["text"] == tr(bullet("acme", "acme-2"), "en")
    assert acme["bullets"][1]["text"] == "Reworded, no new numbers"
    assert {e["id"] for e in out["experiences"]} == {e["id"] for e in MASTER["experience"]}
    assert out["skills_first"] == ["Python"] and out["headline"] == "FDE"


def test_default_and_heuristic_tailoring_cap_bullets():
    for t in (default_tailoring(MASTER, "fr"), heuristic_tailoring(MASTER, make_job(), "fr")):
        assert all(len(e["bullets"]) <= MAX_BULLETS for e in t["experiences"])
        assert t["lang"] == "fr"


def test_tailor_uses_llm_and_falls_back():
    good = FakeLLM(
        {"MASTER CV": {"experiences": [{"id": "lab", "bullets": [{"id": "lab-1", "text": "x"}]}]}}
    )
    out = tailor(MASTER, make_job(), "en", good, "m")
    assert next(e for e in out["experiences"] if e["id"] == "lab")["bullets"] == [
        {"id": "lab-1", "text": "x"}
    ]
    assert tailor(MASTER, make_job(), "en", FakeLLM(fail=True), "m")["lang"] == "en"


def test_document_text_and_diff():
    t = default_tailoring(MASTER, "en")
    t["experiences"][0]["bullets"] = t["experiences"][0]["bullets"][:1]
    doc = build_document(MASTER, t)
    assert MASTER["name"] in to_text(doc)
    diff = cv_diff(MASTER, t)
    assert diff.count("\n-") >= 1


def test_cover_letter_llm_and_template():
    t = default_tailoring(MASTER, "fr")
    letter = cover_letter(MASTER, t, make_job(), FakeLLM({"Language": {"letter": "Bonjour"}}), "m")
    assert letter == "Bonjour"
    fallback = cover_letter(MASTER, t, make_job(), FakeLLM(fail=True), "m")
    assert "Mistral AI" in fallback and MASTER["name"] in fallback


def test_render_pdfs(tmp_path):
    t = default_tailoring(MASTER, "en")
    doc = build_document(MASTER, t)
    cv = render_cv(doc, tmp_path / "cv.pdf")
    letter = render_letter(doc, "Dear team,\n\nHello.", "Mistral AI", "FDE", tmp_path / "l.pdf")
    assert cv.read_bytes()[:4] == b"%PDF" and letter.read_bytes()[:4] == b"%PDF"
