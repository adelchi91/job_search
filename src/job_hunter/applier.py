"""Fill and submit a standard ATS application form with Playwright.

Runs ONLY when Adelchi presses Send on a "Ready to send" card. It never solves a CAPTCHA
or signs in: if one appears, or a required field is still empty after filling, it stops
without submitting and the job moves to "Needs you".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from job_hunter.config import Profile

log = logging.getLogger("job_hunter.applier")

CAPTCHA_SELECTORS = (
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='turnstile']",
    "[class*='captcha']",
)

# (label/name keywords, value key) for plain text inputs
TEXT_FIELDS = [
    (("first name", "first_name", "prénom"), "first_name"),
    (("last name", "last_name", "nom de famille"), "last_name"),
    (("full name", "name"), "name"),
    (("email",), "email"),
    (("phone", "téléphone"), "phone"),
    (("linkedin",), "linkedin"),
    (("github", "website", "portfolio"), "github"),
]


@dataclass
class ApplyResult:
    submitted: bool
    reason: str


def split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split()
    return (parts[0], " ".join(parts[1:])) if parts else ("", "")


def field_values(profile: Profile) -> dict[str, str]:
    first, last = split_name(profile.name)
    return {
        "first_name": first,
        "last_name": last,
        "name": profile.name,
        "email": profile.email,
        "phone": profile.phone,
        "linkedin": profile.linkedin,
        "github": profile.github,
    }


def pick_value(descriptor: str, values: dict[str, str], answers: dict[str, str]) -> str:
    """Value for an input given its label/name/placeholder text."""
    low = descriptor.lower()
    for label, value in answers.items():
        if label and label.lower() in low:
            return value
    for keywords, key in TEXT_FIELDS:
        if any(k in low for k in keywords):
            return values.get(key, "")
    return ""


DESCRIBE_JS = """el => {
    const id = el.id; let label = '';
    if (id) {
        const l = document.querySelector(`label[for="${id}"]`);
        if (l) label = l.innerText;
    }
    if (!label) {
        const l = el.closest('label, .field, .application-question, div');
        if (l) label = l.innerText.slice(0, 200);
    }
    return [label, el.name, el.placeholder, el.getAttribute('aria-label')].join(' ');
}"""

EMPTY_REQUIRED_JS = """() => [...document.querySelectorAll(
        'input[required], textarea[required], select[required], [aria-required=true]')]
    .filter(el => el.type !== 'hidden' && !el.value && !(el.files && el.files.length))
    .map(el => el.name || el.id || el.getAttribute('aria-label') || '?')"""


def _descriptor(page, el) -> str:
    return page.evaluate(DESCRIBE_JS, el)


def _fill_inputs(page, values: dict, answers: dict) -> None:
    for el in page.query_selector_all(
        "input[type=text], input[type=email], input[type=tel], input:not([type]), textarea"
    ):
        if el.input_value():
            continue
        value = pick_value(_descriptor(page, el), values, answers)
        if value:
            el.fill(value)


def _upload(page, cv: Path, letter: Path | None) -> None:
    files = page.query_selector_all("input[type=file]")
    for i, el in enumerate(files):
        desc = _descriptor(page, el).lower()
        if "cover" in desc or "lettre" in desc:
            if letter:
                el.set_input_files(str(letter))
        elif i == 0 or "resume" in desc or "cv" in desc:
            el.set_input_files(str(cv))


def _empty_required(page) -> list[str]:
    return page.evaluate(EMPTY_REQUIRED_JS)


def _headless(headless: bool | None) -> bool:
    if headless is not None:
        return headless
    return os.environ.get("JOB_HUNTER_HEADLESS", "") in ("1", "true", "yes")


def apply(
    url: str,
    profile: Profile,
    cv: Path,
    letter: Path | None,
    answers: dict[str, str],
    *,
    submit: bool = True,
    headless: bool | None = None,
    screenshot: Path | None = None,
) -> ApplyResult:
    """Fill and submit the form. On a server, headless comes from JOB_HUNTER_HEADLESS and a
    screenshot of the page is saved to `screenshot` whenever the send does not go through."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ApplyResult(False, "Playwright not installed: uv sync --extra apply")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=_headless(headless))
        page = browser.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            result = _fill_and_submit(page, profile, cv, letter, answers, submit)
            if screenshot and not result.submitted:
                _snap(page, screenshot)
            return result
        finally:
            browser.close()


def _snap(page, path: Path) -> None:
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:  # noqa: BLE001 - a screenshot is a courtesy, never a failure
        pass


def _fill_and_submit(page, profile, cv, letter, answers, submit) -> ApplyResult:
    apply_btn = page.query_selector("a:has-text('Apply'), button:has-text('Apply for this job')")
    if apply_btn and not page.query_selector("input[type=file]"):
        apply_btn.click()
        page.wait_for_load_state("networkidle")
    if any(page.query_selector(s) for s in CAPTCHA_SELECTORS):
        return ApplyResult(False, "CAPTCHA on the form: please apply by hand")
    _fill_inputs(page, field_values(profile), answers)
    _upload(page, cv, letter)
    missing = _empty_required(page)
    if missing:
        return ApplyResult(False, f"Required fields left empty: {', '.join(missing[:6])}")
    if not submit:
        return ApplyResult(False, "Dry run: form filled, not submitted")
    page.click("button[type=submit], input[type=submit], button:has-text('Submit')")
    page.wait_for_load_state("networkidle")
    body = page.inner_text("body").lower()
    if any(k in body for k in ("thank", "merci", "received", "submitted")):
        return ApplyResult(True, "Submitted")
    return ApplyResult(True, "Submitted, but no confirmation page seen: please verify")
