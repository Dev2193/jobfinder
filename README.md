# jobfinder

**Personal job-search tooling: a local pipeline that finds and pre-fills job applications for human review, and a no-fabrication LaTeX resume tailoring workflow.**

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?logo=playwright&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)
![Claude API](https://img.shields.io/badge/Anthropic-Claude_API-191919?logo=anthropic&logoColor=white)
![LaTeX](https://img.shields.io/badge/LaTeX-resumes-008080?logo=latex&logoColor=white)

---

## Contents

- [Overview](#overview)
- [What's in the repo](#whats-in-the-repo)
- [application-engine](#application-engine)
  - [Features](#features)
  - [Guardrails](#guardrails)
  - [Tech stack](#tech-stack)
  - [Getting started](#getting-started)
  - [Configuration](#configuration)
  - [Using the web app](#using-the-web-app)
  - [CLI commands](#cli-commands)
  - [Local data](#local-data)
  - [Code layout](#code-layout)
- [resume-modifier](#resume-modifier)
- [Known gaps](#known-gaps)
- [License](#license)

## Overview

This repository holds two independent tools used for a personal internship and entry-level job search:

1. **`application-engine`** pulls new postings from company job boards, ranks them against your resume, and opens each eligible application in a real browser to fill it in. It then **stops before submitting**. Every filled application waits for you to press *Submit* or *Discard*, and every outreach email waits for your approval.
2. **`resume-modifier`** is a manual, on-demand process for producing a one-page, job-specific version of a LaTeX resume by rewording existing bullets only.

The two don't share code. The engine attaches the original uploaded resume file to applications and never uses the resume-modifier output.

## What's in the repo

```
jobfinder/
├── application-engine/   # FastAPI web UI + CLI, SQLite, Playwright, Claude API
└── resume-modifier/      # master LaTeX resume and tailored examples
```

## application-engine

### Features

- **Resume intake.** Upload a PDF or DOCX resume; text is extracted with pdfplumber / python-docx and structured into a master resume JSON with a single Claude call. You review and confirm the result.
- **Search filters.** Location (cities, countries, remote/hybrid, excluded countries), employment type, seniority, keywords with synonym expansion, target roles, industries, minimum salary, how recently a job was posted, an exclusion list, a weekly application cap, and a maximum years-of-experience requirement. Saved to `config.yaml`.
- **Answers Bank.** Standard answers for application-form questions (work authorization and sponsorship, availability, salary expectations, profile links, optional demographics, and so on), pre-filled from the resume where possible.
- **Resume diagnosis gate.** A Claude-based review that lists ATS problems and section-level issues in the resume. Each blocking item has to be resolved or waived before tailoring can continue. The review is re-run only when the resume content changes.
- **Job discovery.** Polls the public job-board APIs of **Greenhouse, Lever, Ashby and SmartRecruiters** for the companies listed in `company_universe.txt`, saves the raw postings to disk, and records new jobs in SQLite. Designed to be run on a schedule (cron/launchd).
- **Dedupe and fit scoring.** Normalized company/title/location matching plus SimHash near-duplicate detection on descriptions, then a 0–100 keyword fit score computed in plain Python (no model call) to filter postings before any LLM sees them.
- **Role families and Recruiter step.** Scored jobs are grouped into role families; a Claude "Recruiter" pass runs once per family, grounded in the requirements text of the postings actually discovered.
- **Per-job tailoring (optional, `tailor` command).** A Claude rewriter adjusts resume bullets for one posting, a truth validator rejects any new organization, title, date, degree, certification or metric not present in the master resume, missing numbers are queued as questions for you rather than invented, and the result is rendered as a single-column ATS-friendly `.docx`, parsed back to check nothing was lost, and held to one page.
- **Application form filling.** Playwright (Chromium) opens the real posting and fills Greenhouse, Ashby and Lever application forms from your resume and Answers Bank, saves a screenshot, and stops at the submit button.
- **Review queue.** The *Ready to submit* page lists every filled application with per-item Submit and Discard buttons, plus a release-all action.
- **Contact discovery and outreach drafts.** Looks for a recruiter or hiring manager named in the posting or on the company's own team/about page, flags fellow alumni, and produces a LinkedIn search link for you to check by hand (LinkedIn is never scraped). Drafts cover letters and hiring-manager emails into an approval queue; approved emails are sent through your own SMTP account.
- **Mock interviews.** An interactive terminal session per job: 5 technical questions, then 3 behavioral questions graded against STAR. Transcripts are appended (never overwritten) and the score is tracked across sessions.
- **Dashboard.** Funnel counts, weekly application totals against your cap, outcomes you mark by hand (reply, interview, offer), and a CSV export.
- **Token ledger.** Every Claude call's input, output and cached token counts are recorded in SQLite.
- **In-app coding assistant (`/chat`).** Lets you request changes to the app's own code from the browser. Changes are proposed as diffs and applied only when you click *Apply*; `data/`, `.env` and `.venv/` are off-limits to it.
- **Readiness check.** A local, no-cost pre-flight check of configuration, the running web service, Answers Bank coverage, the resume, the diagnosis gate, and an API-access marker you set yourself.

### Guardrails

These rules are enforced in code:

- Nothing is submitted without an explicit action from you: a per-item *Submit*, the *release all* button, or the `release` command.
- No CAPTCHA solving and no guessing. Bot-detection walls, or required form fields that can't be answered truthfully from your data, move the job to `NEEDS_HUMAN`.
- At most **2 applications per company**, and companies already tracked elsewhere are skipped.
- Once an application has been sent in a role category, no more jobs in that category are auto-filled.
- Postings below a fit score of **75** that also don't match a target role are skipped.
- Postings that require more years of experience than your configured maximum (default 2), or a hard advanced-degree requirement (unless allowed), are excluded outright.
- Outreach email is only ever sent for items you have approved.

### Tech stack

| Area | Tools |
| --- | --- |
| Web UI | FastAPI, Jinja2 templates, Uvicorn |
| CLI | `argparse` (`python -m app.cli`) |
| Storage | SQLite, YAML (`config.yaml`), JSON files |
| LLM | Anthropic Python SDK (Claude) |
| Browser automation | Playwright (Chromium) |
| Parsing / documents | pdfplumber, python-docx, BeautifulSoup |
| HTTP / email | httpx, dnspython, `smtplib` |
| Validation | Pydantic v2 |

Full dependency list: [`application-engine/requirements.txt`](application-engine/requirements.txt).

### Getting started

**Prerequisites**

- Python 3.10 or newer
- An Anthropic API key
- Chromium for Playwright (installed below)

**Install**

```sh
git clone https://github.com/Dev2193/jobfinder.git
cd jobfinder/application-engine

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

**Configure** — create `application-engine/.env` (it is gitignored; see [Configuration](#configuration)):

```sh
ANTHROPIC_API_KEY=your-key-here
```

**Start the web UI**

```sh
python -m app.cli serve
```

This runs Uvicorn with auto-reload at <http://127.0.0.1:8000>. Run CLI commands from the `application-engine/` directory so the `.env` file is found.

**Suggested first run**

1. Open <http://127.0.0.1:8000>, upload your resume, set your search filters, and confirm the parsed resume.
2. Fill in the Answers Bank at `/answers-bank`.
3. Work through the diagnosis at `/diagnosis` until it is cleared.
4. With the server still running, check everything is in place: `python -m app.cli readiness`.
5. Start a run from `/run` (or `python -m app.cli auto-run`), then review results at `/ready-to-submit`.

### Configuration

**Environment variables** (read from the shell or from `application-engine/.env`):

| Variable | Required | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Yes | Resume structuring, diagnosis, Recruiter, rewriting/validation, outreach drafts, interview prep, and the in-app assistant |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | Only to send outreach email | Your own mail account; all four must be set. The connection uses STARTTLS. Values are never logged or displayed. |
| `AUTO_APPLY_PROVIDER_STATUS` | Optional | A non-secret marker checked by `readiness`. Set to `ready`, `confirmed` or `ok` once you have confirmed your Anthropic account has usable credit (or `blocked` / `disabled`). Anything else leaves that check at "needs review". |

**`config.yaml`** holds the search filters. It is created with defaults on first use and edited through the intake page.

**`company_universe.txt`** lists the job boards to poll, one `<ats>:<slug>` per line (supported prefixes: `greenhouse`, `lever`, `ashby`, `smartrecruiters`). Lines starting with `#` are ignored. A company's careers-page URL usually reveals which ATS and slug it uses.

### Using the web app

| Page | Purpose |
| --- | --- |
| `/` | Upload a resume and set search filters |
| `/answers-bank` | Standard answers used when filling forms |
| `/diagnosis` | Resolve or waive resume issues found by the diagnoser |
| `/tailoring-issues` | Problems raised while tailoring a resume for a job |
| `/run` | Start a discover → score → fill run |
| `/ready-to-submit` | Submit or discard each filled application, or release all |
| `/applications` | Application history, screenshots, and outcome tracking |
| `/outreach` | Review, edit, approve, reject and send outreach drafts |
| `/dashboard` | Funnel metrics; CSV export at `/dashboard/export.csv` |
| `/chat` | In-app coding assistant with apply/discard for each proposed edit |

JSON endpoints: `/api/readiness` and `/api/dashboard-sync`.

### CLI commands

Run as `python -m app.cli <command>` from `application-engine/` (the CLI calls itself `engine` in its help text). `--help` works on every command.

| Command | What it does |
| --- | --- |
| `serve` | Start the web UI on 127.0.0.1:8000 |
| `readiness` | No-cost local pre-flight checks; exits non-zero unless the result is "go" |
| `parse-resume <path>` | Parse a PDF/DOCX resume into the master JSON |
| `save-resume` | Register a hand-edited `data/master_resume.json` as a new resume version |
| `discover` | Poll the configured job boards for new postings |
| `score` | Dedupe and fit-score newly discovered jobs |
| `diagnose [--force]` | Run the resume diagnosis gate |
| `cluster-families` | Group scored jobs into role families (no LLM call) |
| `recruiter [--force] [--label ...]` | Run the Recruiter step for new or stale role families |
| `tailor <job_id>` | Rewrite, validate, render and re-parse a tailored resume for one job |
| `prep <job_id> [--headed]` | Fill the application form for one job and stop before submitting |
| `auto-run [--headed]` | Discover, score and fill every eligible job; submits nothing |
| `release [--headed]` | Submit every application currently waiting in the review queue |
| `discover-contact <job_id>` | Find a likely recruiter or hiring manager for a job |
| `draft-outreach <job_id> [--cover-letter] [--application-ref REF]` | Draft a cover letter and/or hiring-manager email into the approval queue |
| `interview <job_id>` | Interactive mock interview in the terminal |

`--headed` shows the browser window instead of running headless.

### Local data

Everything personal stays on your machine under `application-engine/data/` and is excluded by `.gitignore`: the SQLite database (`engine.db`), the master resume and Answers Bank JSON, uploaded resume files, raw postings (`data/raw_postings/`), and generated files such as screenshots and tailored resumes (`data/output/`). The `.env` file and `config.yaml` are ignored as well. The diagnoser also writes a Markdown report to `data/diagnosis.md`; note that this file is **not** covered by `.gitignore`, and a copy is currently committed.

### Code layout

```
application-engine/
├── app/
│   ├── cli.py              # CLI entry point
│   ├── web/                # FastAPI app (main.py) and Jinja2 templates
│   ├── db.py               # SQLite schema and helpers
│   ├── config.py           # search filters / config.yaml
│   ├── llm.py              # Claude wrapper and token ledger
│   ├── resume_parser.py    # PDF/DOCX → master resume JSON
│   ├── answers_bank.py
│   ├── readiness.py        # pre-flight checks
│   ├── auto_pipeline.py    # full run, guardrails, submit/discard/release
│   ├── discovery/          # Greenhouse, Lever, Ashby, SmartRecruiters clients
│   ├── scoring/            # dedupe, fit score, role families
│   ├── diagnoser/          # resume diagnosis gate
│   ├── recruiter/          # per-role-family analysis
│   ├── rewriter/           # tailoring, truth validator, metric gaps, .docx rendering
│   ├── prep/               # Playwright form filling
│   ├── approval/           # application and outreach queues
│   ├── outreach/           # contact discovery, drafts, SMTP sending
│   ├── interview/          # mock interview sessions
│   ├── dashboard/          # metrics and CSV export
│   ├── site_chat.py        # in-app coding assistant
│   └── shippy_export.py    # export for a separate personal job dashboard
├── company_universe.txt
├── requirements.txt
└── tests/                  # package placeholder, no tests yet
```

## resume-modifier

A manual process (carried out with Claude, on request) for tailoring a resume to a specific job without inventing anything. It is separate from `application-engine`.

- **Source of truth:** [`resume-modifier/master-resume.tex`](resume-modifier/master-resume.tex), a one-page LaTeX resume based on Jake's Resume template. When the real resume changes, re-export it from Overleaf and replace this file.
- **Rules:** check `examples/` first and reuse an existing version for a very similar role; otherwise start from the master. Only the wording inside existing `\resumeItem{...}` bullets may change, never section headers, section order, or which entries appear. Nothing is fabricated: no new skills, metrics or experience. The result must compile to exactly one page; if it runs long, tighten the wording rather than removing content.
- **Output:** `examples/{company}-{role}/resume.tex` plus the compiled `resume.pdf`, recorded in the table in [`resume-modifier/README.md`](resume-modifier/README.md).
- **Building:** compile with [Tectonic](https://tectonic-typesetting.github.io/), which needs no local TeX installation:

  ```sh
  tectonic resume.tex
  ```

- **Cover letters:** written only when asked for, under the same no-fabrication rule, in a direct and concise tone.

Current examples: McKinsey / QuantumBlack (Software Engineering AI Intern) and John Deere (Part-Time Student, UX Research).

## Known gaps

- There is no automated test suite yet (`application-engine/tests/` is empty apart from `__init__.py`).
- Several modules refer to a `CLAUDE.md` design spec (the "§" section numbers in docstrings), which is not committed to this repository.
- `shippy_export.py` cross-checks against a personal job-dashboard HTML file at a hard-coded macOS path. If the file isn't there, the check is simply skipped.
- Post-submission outcomes (replies, interviews, offers) are recorded by hand; there is no inbox integration.

## License

No license has been specified for this project yet. Until one is added, all rights are reserved by the author.
