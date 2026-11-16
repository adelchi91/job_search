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

Scoring and tailoring use, in this order: your own Groq / Mistral key (see below), then Claude
through `ANTHROPIC_API_KEY` (`uv sync --extra llm`) or the signed-in `claude` CLI. With none of
them, they fall back to keyword heuristics. Set `llm: {prefer: claude}` in `data/config.yaml`
to try Claude before your keys. `--no-llm` forces the heuristics.

## Free AI tailoring with your own Groq / Mistral key

Both providers have a free tier and an OpenAI-compatible API. Which one gets which job:

| Keys set | English jobs | French jobs |
|---|---|---|
| Groq only | Groq | Groq |
| Mistral only | Mistral | Mistral |
| Both | Groq | Mistral |
| Neither | Claude, else heuristics | Claude, else heuristics |

The same guide is in the dashboard under **Settings → AI provider**:

**Groq (free, very fast; used for English jobs when both are set)**

1. Create a Groq account at https://console.groq.com
2. Open API Keys, then click Create key and copy it (it starts with gsk_).

**Mistral (free plan; used for French jobs when both are set)**

1. Create a Mistral account at https://console.mistral.ai
2. Choose the free plan (Experiment), then open API Keys and click Create key.
3. In Admin, turn off "allow training on my data".

**Connect**

1. Paste the key(s) below and click Save, then click Test connection: it makes one tiny call and shows OK or the provider's error.
2. Or set GROQ_API_KEY / MISTRAL_API_KEY in the environment instead.

**Good to know**

1. Free tiers are rate-limited: on busy days some jobs fall back to the keyword heuristics and the template letter.
2. Check the data-training opt-out of each provider before sending your CV.

Keys are read from `GROQ_API_KEY` / `MISTRAL_API_KEY` first, then from `data/keys.yaml`
(written by the dashboard, readable by you only, git-ignored with the rest of `data/`).
The page never shows a key back, only its first and last four characters.

Default models: Groq `openai/gpt-oss-20b` (scoring) and `openai/gpt-oss-120b` (tailoring);
Mistral `mistral-small-latest` and `mistral-large-latest`. Override them, or the routing, in
`data/config.yaml`:

```yaml
llm:
  groq: {score_model: openai/gpt-oss-20b, tailor_model: openai/gpt-oss-120b}
  mistral: {score_model: mistral-small-latest, tailor_model: mistral-large-latest}
  route: {en: groq, fr: mistral}
  retry_429: true   # one retry on a rate limit, then the heuristics take over
```

The usual guardrails stay: the tailored CV is validated against your master CV (only its own
experiences, bullets and skills; a reworded bullet that adds a number is reverted), and any
provider error, including a 429 rate limit, falls back to the heuristics for that job.

## Checks

```sh
uv run pytest -q
uv run ruff check .
uv run job-hunter run --dry-run --fixtures   # replays recorded API responses, sends nothing
```
