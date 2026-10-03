# jobfinder

Personal job-search tooling.

- **[application-engine](application-engine/)** — FastAPI + CLI system that discovers postings,
  scores them against a resume, fills out real application forms via browser automation
  (Greenhouse, Ashby, Lever), and holds everything for manual submit/discard review.
  Nothing submits automatically.
- **[resume-modifier](resume-modifier/)** — manual, on-demand LaTeX resume tailoring workflow
  (one tailored, one-page version per job, bullet-wording only, no fabrication).

## Note on data

`application-engine`'s runtime data (SQLite DB, uploaded resume files, scraped postings,
`.env` secrets, `config.yaml`) is intentionally excluded from this repo — see its
`.gitignore`. Set those up locally per `application-engine`'s own docs before running it.
