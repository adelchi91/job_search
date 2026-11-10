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
uv run job-hunter schedule    # installs a launchd job: weekdays 08:00 (local only)
uv run job-hunter export      # zip of data/ to import on the deployed app
uv run job-hunter run-all     # server layout: one pass for every invited user
```

In the dashboard, open a card to check the CV diff against your master CV, the PDFs and the
cover letter, and to answer any open questions:

- **Approve & send** fills the form in a visible browser and submits it.
- If a CAPTCHA appears, or a login is needed, or a required field is still empty, it stops
  without submitting and moves the card to **Needs you**.

Scoring and tailoring use Claude through `ANTHROPIC_API_KEY` (`uv sync --extra llm`) or else
the signed-in `claude` CLI. Without either, they fall back to keyword heuristics.
`--no-llm` forces the heuristics.

Locally (no `GOOGLE_CLIENT_ID` set) the app has a single user, no sign-in, and keeps using
`data/` exactly as before.

## Deploy (Railway)

The deployed app runs the daily run itself (weekdays 08:00 Europe/Paris), lets each invited
person sign in with Google, and keeps one folder per person on a volume:

```
/data/allowlist.yaml                 who may sign in (managed on the Admin page)
/data/users/<name>-<hash>/           one complete data dir per person:
    config.yaml companies.yaml master_cv.yaml   (editable on My files, with undo)
    jobs.sqlite applications/ digests/
    files/{cv,diplomas,letters,other}/          personal documents
```

### 1. Google sign-in client (once)

1. In Google Cloud Console, create a project. Under **Google Auth Platform → Branding**, choose
   **External**. Under **Audience**, keep the app in **Testing** and add both Google accounts as
   test users. Testing mode needs no Google verification.
2. Under **Clients**, create an **OAuth client ID** of type *Web application*.
   - Authorised redirect URI: `https://<your-app>.up.railway.app/auth/callback`
3. Copy the client ID and the client secret.

The allowlist in the app has the final say. A Google test user who is not on the Admin page
list still gets "not invited".

### 2. Railway service

1. In Railway, create a **New Project → Deploy from GitHub repo** and pick this repo. The build
   uses the `Dockerfile` (Chromium included). `railway.json` sets the `/healthz` check.
2. Open the service, go to **Settings → Volumes**, and add a volume mounted at `/data`.
3. Under **Settings → Networking**, choose **Generate Domain**. This gives you
   `https://<your-app>.up.railway.app`.
4. Under **Variables**, set:

| Variable | Value |
|---|---|
| `ADMIN_EMAIL` | your Google address (always allowed, sees Admin) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | from step 1 |
| `SESSION_SECRET` | a long random string: `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `APP_URL` | `https://<your-app>.up.railway.app` |
| `ANTHROPIC_API_KEY` | for scoring, tailoring and CV-from-PDF |
| `JOB_HUNTER_SCHEDULER` | `1` (turns on the weekday 08:00 run) |
| `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASSWORD` `SMTP_FROM` | digest email, e.g. `smtp.gmail.com`, `587`, a Gmail address and a Gmail **app password** (optional: without them there is no email) |

Keep the service at **1 replica**: the scheduler and the SQLite files live in one process.

### 3. First sign-in, invite, migrate

1. Open the app URL and choose **Continue with Google** with the `ADMIN_EMAIL` account.
2. Go to **Admin → People** and add your partner's Google address. They can now sign in and get
   their own empty folder.
3. Move your existing local data: run `uv run job-hunter export` (this writes
   `job-hunter-data.zip`), then upload it on **My files → Import a zip**. Tailored CVs recorded
   with local paths are found again under `applications/`.
4. Stop the local daily job so the two runs do not overlap:
   `launchctl bootout gui/$(id -u)/io.jobhunter.daily` (or remove the plist that
   `job-hunter schedule` installed).

Each person then uploads a CV PDF on **My files**. The server drafts a `master_cv.yaml`, which
they review and **Apply**. People whose master CV is still the sample are skipped by the daily
run. The **Admin** page shows the next run and the last results, and has **Run now**.

On the server, **Approve & send** runs Chromium headless (`JOB_HUNTER_HEADLESS=1`). When it
stops without submitting, the card links a screenshot of what the form showed.

To back up, use **My files → Export zip**. Railway volume backups also work.

## Checks

```sh
uv run pytest -q
uv run ruff check .
uv run job-hunter run --dry-run --fixtures   # replays recorded API responses, sends nothing
```
