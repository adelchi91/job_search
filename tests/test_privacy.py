"""Personal data never reaches an LLM prompt; outputs still carry the real identity."""

from __future__ import annotations

import copy

import pytest
from conftest import FakeLLM, make_job

from job_hunter import pipeline
from job_hunter.config import DEFAULTS_DIR
from job_hunter.cvimport import draft_master
from job_hunter.scoring import score_job
from job_hunter.tailor.cover import cover_letter, sign
from job_hunter.tailor.cv import build_document, load_master, tailor
from job_hunter.tailor.privacy import Pseudonymiser, Scrubber, redact_master, scrub_pii

NAME = "Jeanne-Marie Dupontel, PhD"
EMAIL = "jm.dupontel@example.org"
PHONE = "+33 6 12 34 56 78"
LINKEDIN = "linkedin.com/in/jmdupontel"
SECRETS = [NAME, "Jeanne", "Marie", "Dupontel", EMAIL, PHONE, LINKEDIN, "06.98.76.54.32"]


@pytest.fixture
def master() -> dict:
    m = copy.deepcopy(load_master(DEFAULTS_DIR / "master_cv.yaml"))
    m["name"] = NAME
    m["contact"] = {"email": EMAIL, "phone": PHONE, "location": "Paris", "linkedin": LINKEDIN}
    m["summary"] = {
        "en": "Dupontel builds agents at Acme Industrial Software. Mail jm.dupontel@example.org.",
        "fr": "Jeanne-Marie construit des agents. Tél 06.98.76.54.32.",
    }
    m["experience"][0]["bullets"][0]["en"] += " Call 06.98.76.54.32 or see " + LINKEDIN
    return m


def leaked(prompts: list[str], secrets: list[str]) -> list[str]:
    text = "\n".join(prompts).casefold()
    return [s for s in secrets if s.casefold() in text]


def employers(m: dict) -> list[str]:
    return [e["company"] for e in m["experience"]]


# --- the scrubbing rules -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "+33 6 12 34 56 78",
        "06.12.34.56.78",
        "06 12 34 56 78",
        "+33 (0)6 12 34 56 78",
        "555-123-4567",
    ],
)
def test_phone_numbers_are_caught(text):
    assert scrub_pii(f"call {text} now") == "call [PHONE] now"


@pytest.mark.parametrize(
    "text",
    ["2019-2023", "2019 – 2023", "7+ years", "cut costs by 40%", "1,200 users", "v0.12.3", "0.5"],
)
def test_ordinary_numbers_are_untouched(text):
    assert scrub_pii(text) == text


def test_emails_are_caught():
    assert scrub_pii("write to a.b+c@mail.example.co.uk.") == "write to [EMAIL]."


def test_redact_master_drops_identity_and_keeps_content(master):
    safe = redact_master(master)
    assert "name" not in safe and "contact" not in safe
    assert all("location" not in e for e in safe["experience"])
    assert not leaked([str(safe)], SECRETS)
    assert "[CANDIDATE]" in safe["summary"]["en"] and "[PHONE]" in safe["summary"]["fr"]
    assert safe["experience"][0]["company"] == master["experience"][0]["company"]
    assert master["name"] == NAME  # the original is not mutated


def test_name_titles_and_short_parts_are_not_masked():
    s = Scrubber({"name": "Li Wu, PhD"})
    assert s.text("PhD in physics with Li Wu") == "PhD in physics with [CANDIDATE]"


# --- the prompts ---------------------------------------------------------------------------


def test_cv_and_cover_prompts_carry_no_identity(master):
    llm = FakeLLM({"MASTER CV": {"experiences": []}, "Language": {"letter": "Dear team, hi."}})
    tailored = tailor(master, make_job(), "en", llm, "m")
    letter = cover_letter(master, tailored, make_job(), llm, "m")
    assert len(llm.calls) == 2
    assert leaked(llm.calls, SECRETS) == []
    assert letter.endswith(f"Kind regards,\n{NAME}")


def test_scoring_prompt_carries_no_identity(master, monkeypatch):
    llm = FakeLLM({"CANDIDATE CV": {"score": 70, "reason": "ok"}})
    seen = []

    def fake_score(job, cv_text, *args, **kw):
        seen.append(cv_text)
        return score_job(job, cv_text, ["engineer"], llm, "m")

    class Store:
        def by_status(self, *_):
            return [{"id": 1, **make_job().__dict__}]

        def update(self, *a, **k): ...

        def set_status(self, *a, **k): ...

    monkeypatch.setattr(pipeline, "score_job", fake_score)
    monkeypatch.setattr(pipeline, "job_from_row", lambda row: make_job())
    settings = type(
        "S", (), {"roles": [], "score_model": "m", "tailor_model": "m", "role_keywords": []}
    )()
    settings.fit_threshold = 60
    pipeline.score_new(Store(), master, settings, llm)
    assert seen and leaked(seen + llm.calls, SECRETS) == []


def test_pdf_import_prompt_has_no_email_or_phone():
    llm = FakeLLM({"CV TEXT": load_master(DEFAULTS_DIR / "master_cv.yaml")})
    draft_master(f"Jane Doe\n{EMAIL} | {PHONE}\nEngineer", llm, "m")
    assert leaked(llm.calls, [EMAIL, PHONE]) == []


# --- the outputs keep the real identity ----------------------------------------------------


def test_documents_still_show_name_and_contact(master):
    tailored = tailor(master, make_job(), "en", FakeLLM({"MASTER CV": {}}), "m")
    doc = build_document(master, tailored)
    assert doc["name"] == NAME and doc["contact"]["email"] == EMAIL
    assert doc["contact"]["phone"] == PHONE


def test_model_signature_is_replaced_by_the_real_one():
    raw = "Madame, Monsieur,\n\nCorps.\n\nBien cordialement,\n[CANDIDATE]"
    assert sign(raw, NAME, "fr") == f"Madame, Monsieur,\n\nCorps.\n\nBien cordialement,\n{NAME}"
    assert sign("Hello.\n\nBest regards,\n[Your Name]", NAME, "en").endswith(
        f"Hello.\n\nKind regards,\n{NAME}"
    )
    assert sign("  ", NAME, "en") == ""


# --- optional employer pseudonymisation ----------------------------------------------------


def test_employer_flag_hides_employers_and_restores_them(master):
    real = master["experience"][0]
    first_bullet = real["bullets"][0]["id"]
    reply = {
        "summary": "Built agents at [EMPLOYER_1].",
        "experiences": [
            {"id": "exp_1", "bullets": [{"id": "exp_1_b2", "text": "Shipped it at [EMPLOYER_1]"}]}
        ],
    }
    llm = FakeLLM(
        {"MASTER CV": reply, "Language": {"letter": "Dear team, [EMPLOYER_2] taught me."}}
    )
    tailored = tailor(master, make_job(), "en", llm, "m", anonymise_employers=True)
    letter = cover_letter(master, tailored, make_job(), llm, "m", anonymise_employers=True)

    ids = [e["id"] for e in master["experience"]] + [first_bullet]
    assert leaked(llm.calls, employers(master) + SECRETS) == []
    assert not any(f"id: {i}\n" in call for call in llm.calls for i in ids)
    assert tailored["summary"] == f"Built agents at {real['company']}."
    chosen = next(e for e in tailored["experiences"] if e["id"] == real["id"])
    assert chosen["bullets"] == [
        {"id": real["bullets"][1]["id"], "text": f"Shipped it at {real['company']}"}
    ]
    doc = build_document(master, tailored)
    assert [e["company"] for e in doc["experience"]] == employers(master)
    assert master["experience"][1]["company"] in letter


def test_employer_flag_off_keeps_employers_in_prompt(master):
    llm = FakeLLM({"MASTER CV": {}})
    tailor(master, make_job(), "en", llm, "m")
    assert master["experience"][0]["company"] in llm.calls[0]


def test_pseudonymiser_round_trip_and_unknown_tokens(master):
    p = Pseudonymiser(master)
    hidden = p.forward(master)
    assert hidden["experience"][0]["id"] == "exp_1"
    assert hidden["experience"][0]["company"] == "[EMPLOYER_1]"
    assert p.back(hidden)["experience"] == master["experience"]
    assert p.show("[EMPLOYER_99] stays") == "[EMPLOYER_99] stays"
    assert Pseudonymiser({"experience": []}).hide("Acme") == "Acme"
