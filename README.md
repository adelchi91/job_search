# job-hunter

A daily job search, CV tailoring and application tracker (JH-3) for Presales, Forward Deployed
Engineer, AI Engineer, AI Architect and AI Solution Engineer roles in Paris/IdF (hybrid) and
remote Europe.

```
collect ─▶ filter & dedupe ─▶ score (fit 0-100) ─▶ tailor CV + letter ─▶ route ─▶ digest
                                                                         │
                         dashboard: Ready to send ──(you click Send)──▶ Applied
                                    Needs you    ──(you apply by hand)─▶ Applied
```

Nothing is ever submitted without your click. LinkedIn, Indeed and WTTJ are read-only, so jobs
found there always land in **Needs you**.

## Setup

```sh
uv sync                       # core
uv sync --extra apply         # + Playwright for one-click sending
uv run playwright install chromium
uv sync --extra boards        # + LinkedIn/Indeed/Glassdoor search (python-jobspy), optional
uv run job-hunter init        # creates data/config.yaml, companies.yaml, master_cv.yaml
```

Then edit these files:

- `data/master_cv.yaml`: your real CV, bilingual (EN/FR). Tailoring only selects, reorders and
  rewords these bullets. A rewrite that adds a number the original bullet lacks is discarded.
- `data/config.yaml`: the `profile` block (name, email, phone, LinkedIn, GitHub) and its
  `answers` (notice period, salary expectation, work authorisation, location). These answers
  prefill application questions. Here you can also change roles, keywords, locations,
  `fit_threshold` (60) and `max_open_per_company` (2).
- `data/companies.yaml`: the watchlist. `ats` + `token` read the company's own careers API
  (Greenhouse, Lever, Ashby, Workable). Companies without one are searched on the boards by name.

## Daily use

```sh
uv run job-hunter run         # collect, score, tailor, route; writes data/digests/YYYY-MM-DD.md
uv run job-hunter serve       # dashboard on http://127.0.0.1:8000
uv run job-hunter schedule    # installs a launchd job: weekdays 08:00
```

In the dashboard, open a card to check the CV diff against your master CV, the PDFs and the
cover letter, and to answer any open questions:

- **Approve & send** fills the form in a visible browser and submits it.
- If a CAPTCHA appears, or a login is needed, or a required field is still empty, it stops
  without submitting and moves the card to **Needs you**.

Scoring and tailoring use Claude through `ANTHROPIC_API_KEY` (`uv sync --extra llm`) or else
the signed-in `claude` CLI. Without either, they fall back to keyword heuristics.
`--no-llm` forces the heuristics.

## Checks

```sh
uv run pytest -q
uv run ruff check .
uv run job-hunter run --dry-run --fixtures   # replays recorded API responses, sends nothing
```
