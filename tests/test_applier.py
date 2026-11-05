from __future__ import annotations

from pathlib import Path

from job_hunter import applier
from job_hunter.config import Profile

PROFILE = Profile(name="Ada Lovelace King", email="a@b.c", phone="+33", linkedin="li", github="gh")


def test_split_and_values():
    assert applier.split_name("Ada Lovelace King") == ("Ada", "Lovelace King")
    assert applier.split_name("") == ("", "")
    assert applier.field_values(PROFILE)["last_name"] == "Lovelace King"


def test_pick_value_prefers_saved_answers():
    values = applier.field_values(PROFILE)
    assert applier.pick_value("Why Mistral? q_1", values, {"Why Mistral?": "Because"}) == "Because"
    assert applier.pick_value("First Name *", values, {}) == "Ada"
    assert applier.pick_value("Email address", values, {}) == "a@b.c"
    assert applier.pick_value("Favourite colour", values, {}) == ""


class El:
    def __init__(self, desc, value=""):
        self.desc, self.value, self.files = desc, value, None

    def input_value(self):
        return self.value

    def fill(self, v):
        self.value = v

    def set_input_files(self, f):
        self.files = f

    def click(self):
        pass


class Page:
    def __init__(self, captcha=False, required_empty=(), body="Thank you for applying"):
        self.inputs = [El("First name"), El("Email"), El("Already", "x")]
        self.uploads = [El("Resume/CV"), El("Cover letter")]
        self.captcha, self.required_empty, self.body, self.clicked = (
            captcha,
            list(required_empty),
            body,
            False,
        )

    def query_selector(self, sel):
        if "captcha" in sel or "recaptcha" in sel:
            return El("c") if self.captcha else None
        return None

    def query_selector_all(self, sel):
        return self.uploads if sel == "input[type=file]" else self.inputs

    def evaluate(self, script, el=None):
        return el.desc if el is not None else self.required_empty

    def click(self, sel):
        self.clicked = True

    def wait_for_load_state(self, *_):
        pass

    def inner_text(self, _):
        return self.body


def run(page, submit=True):
    return applier._fill_and_submit(page, PROFILE, Path("cv.pdf"), Path("l.pdf"), {}, submit)


def test_fills_uploads_and_submits():
    page = Page()
    assert run(page) == applier.ApplyResult(True, "Submitted")
    assert [e.value for e in page.inputs] == ["Ada", "a@b.c", "x"]
    assert [e.files for e in page.uploads] == ["cv.pdf", "l.pdf"]


def test_never_submits_past_captcha_or_missing_fields():
    page = Page(captcha=True)
    assert not run(page).submitted and not page.clicked
    page = Page(required_empty=["salary"])
    r = run(page)
    assert not r.submitted and "salary" in r.reason and not page.clicked
    assert run(Page(), submit=False).reason.startswith("Dry run")


def test_submitted_without_confirmation_asks_to_verify():
    assert "verify" in run(Page(body="Loading...")).reason
