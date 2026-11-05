from __future__ import annotations

from conftest import FakeLLM, make_job

from job_hunter.filters import fold, keep, location_matches, role_matches
from job_hunter.scoring import detect_language, heuristic_score, score_job


def test_fold_strips_accents():
    assert fold("Ingénieur IA") == " ingenieur ia "


def test_role_and_exclusions(settings):
    assert role_matches("Forward Deployed Engineer", settings)
    assert role_matches("Ingénieur Avant-Vente IA", settings) or role_matches(
        "Presales Engineer", settings
    )
    assert not role_matches("Stage - AI Engineer", settings)
    assert not role_matches("Accountant", settings)


def test_location_rules(settings):
    assert location_matches(make_job(location="Paris, France"), settings)
    assert location_matches(make_job(location=""), settings)
    assert not location_matches(make_job(location="Tokyo, Japan"), settings)
    assert location_matches(make_job(location="Remote - Europe", remote="full"), settings)
    assert not keep(make_job(location="New York"), settings)


def test_heuristic_rewards_target_titles():
    cv = "unrelated words only"
    hit, _ = heuristic_score(make_job(), cv, ["forward deployed"])
    miss, reason = heuristic_score(make_job(title="Accountant"), cv, ["forward deployed"])
    assert hit - miss == 40 and "near" in reason


def test_detect_language():
    assert detect_language(make_job(language="fr")) == "fr"
    fr = make_job(
        language="",
        description="Vous rejoindrez une équipe et nous cherchons des profils pour les clients",
    )
    assert detect_language(fr) == "fr"
    assert (
        detect_language(make_job(language="", description="You will join the team and we")) == "en"
    )


def test_score_job_llm_then_fallback():
    llm = FakeLLM({"JOB:": {"score": 140, "reason": "great"}})
    assert score_job(make_job(), "cv", ["FDE"], llm, "m")["score"] == 100
    out = score_job(make_job(), "cv", ["FDE"], FakeLLM(fail=True), "m", ["deployed"])
    assert out["reason"].startswith("heuristic") and out["language"] == "en"
