"""`engine` CLI — thin wrappers around each stage so any stage can be re-run
independently without redoing expensive work (§2)."""

import argparse
import json
import sys
from pathlib import Path

from app.answers_bank import load_answers_bank
from app.auto_pipeline import release_all_prepped, run_full_pipeline
from app.config import industry_text, load_config, target_role_text
from app.db import get_conn, init_db
from app.diagnoser.diagnoser import needs_diagnosis, run_diagnoser
from app.discovery.runner import run_discovery
from app.interview.session import run_interview_session
from app.models import MasterResume
from app.outreach.contact_discovery import discover_contact
from app.outreach.orchestrator import draft_cover_letter, draft_hiring_manager_email
from app.prep.prep import prep_job
from app.readiness import evaluate_readiness
from app.recruiter.recruiter import run_recruiter
from app.resume_parser import parse_resume_file, save_master_resume
from app.rewriter.resume_render import reparse_and_check, render_ats_docx
from app.rewriter.rewriter import tailor_job
from app.scoring.pipeline import run_dedupe_and_score
from app.scoring.role_family import assign_role_families

MASTER_RESUME_PATH = Path(__file__).resolve().parent.parent / "data" / "master_resume.json"


def _load_master_resume() -> MasterResume:
    if not MASTER_RESUME_PATH.exists():
        print("No master resume found. Run `engine parse-resume <path>` first.", file=sys.stderr)
        sys.exit(1)
    return MasterResume.model_validate(json.loads(MASTER_RESUME_PATH.read_text()))


def cmd_serve(args):
    import uvicorn

    uvicorn.run("app.web.main:app", host="127.0.0.1", port=8000, reload=True)


def cmd_readiness(args):
    """Run the local, no-charge startup gate without starting the pipeline."""
    init_db()
    with get_conn() as conn:
        report = evaluate_readiness(conn)
    print(json.dumps(report, indent=2))
    if report["status"] != "go":
        raise SystemExit(2)


def cmd_parse_resume(args):
    init_db()
    with get_conn() as conn:
        resume = parse_resume_file(Path(args.path), conn=conn)
        print(json.dumps(resume.model_dump(), indent=2))
        print("\nReview the JSON above. To save it as-is, run: engine save-resume", file=sys.stderr)
        MASTER_RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
        MASTER_RESUME_PATH.write_text(json.dumps(resume.model_dump(), indent=2))


def cmd_save_resume(args):
    init_db()
    resume = _load_master_resume()
    with get_conn() as conn:
        h, path = save_master_resume(resume, conn)
        print(f"Saved master resume (hash {h[:12]}...) to {path}")


def cmd_discover(args):
    init_db()
    with get_conn() as conn:
        summary = run_discovery(conn)
    print(json.dumps(summary, indent=2))


def cmd_score(args):
    init_db()
    resume = _load_master_resume()
    cfg = load_config()
    with get_conn() as conn:
        summary = run_dedupe_and_score(conn, resume, cfg)
    print(json.dumps(summary, indent=2))


def cmd_diagnose(args):
    init_db()
    resume = _load_master_resume()
    cfg = load_config()
    with get_conn() as conn:
        if not args.force and not needs_diagnosis(resume, conn):
            print("Resume hash unchanged and already CLEARED — nothing to do. Use --force to re-run.")
            return
        diagnosis_id = run_diagnoser(resume, cfg, conn)
    print(f"Diagnosis #{diagnosis_id} written to data/diagnosis.md — resolve or waive ATS-killers via the web UI.")


def cmd_cluster_families(args):
    """Free (no LLM) — cluster SCORED jobs into role families and print a
    summary. Meant to be inspected before running `engine recruiter`, per
    §12's own instruction: verify the families are sensible before spending
    on keyword extraction."""
    init_db()
    with get_conn() as conn:
        summary = assign_role_families(conn)
    print(f"{summary['total_jobs']} jobs -> {summary['total_families']} role families\n")
    for f in summary["families"]:
        titles = ", ".join(f["sample_titles"][:3])
        print(f"[{f['job_count']:>2}] {f['label']:<35} ({f['seniority']})  e.g. {titles}")


def cmd_recruiter(args):
    init_db()
    resume = _load_master_resume()
    cfg = load_config()
    target_role = target_role_text(cfg)
    label_filter = [s.strip() for s in args.label.split(",")] if args.label else None
    with get_conn() as conn:
        assign_role_families(conn)  # cheap, keeps role_family_id current before spending on LLM calls
        summary = run_recruiter(
            conn, resume, target_role, industry_text(cfg), force=args.force, label_filter=label_filter
        )
    print(f"Ran Recruiter for {len(summary['ran'])} families: {summary['ran']}")
    print(f"Skipped {len(summary['skipped_fresh'])} already-fresh families: {summary['skipped_fresh']}")


def cmd_tailor(args):
    """§6.3-§6.6 (minimal): Rewriter -> truth validator (with retry) ->
    metrics gap queue -> ATS-safe .docx render -> re-parse check, for a
    single job. Skips jobs already NEEDS_HUMAN or with a still-current
    tailored_resumes row (content-hash keyed, per §10: never re-generate)."""
    init_db()
    resume = _load_master_resume()
    cfg = load_config()
    target_role = target_role_text(cfg)

    with get_conn() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (args.job_id,)).fetchone()
        if job is None:
            print(f"No job with id {args.job_id}", file=sys.stderr)
            sys.exit(1)
        job = dict(job)

        result = tailor_job(job, resume, target_role, conn)
        print(json.dumps(result, indent=2))

        if result["status"] != "TAILORED":
            return

        row = conn.execute("SELECT * FROM tailored_resumes WHERE job_id = ?", (args.job_id,)).fetchone()
        entries = json.loads(row["rewritten_bullets"])

        missing_keywords = []
        if job.get("role_family_id"):
            family = conn.execute(
                "SELECT missing_keywords FROM role_families WHERE id = ?", (job["role_family_id"],)
            ).fetchone()
            if family and family["missing_keywords"]:
                missing_keywords = json.loads(family["missing_keywords"])

        docx_path, final_entries, fits_one_page, trimmed_log = render_ats_docx(
            resume, entries, job["company"], job["title"],
            target_role=target_role, missing_keywords=missing_keywords,
        )
        ok, missing = reparse_and_check(docx_path, resume, final_entries)

        conn.execute(
            "UPDATE tailored_resumes SET docx_path = ?, ats_reparse_ok = ?, rewritten_bullets = ?, "
            "fits_one_page = ?, trimmed_for_length = ? WHERE job_id = ?",
            (
                str(docx_path), int(ok), json.dumps(final_entries),
                int(fits_one_page), json.dumps(trimmed_log), args.job_id,
            ),
        )
        conn.commit()

        print(f"\nRendered: {docx_path}")
        print(f"ATS re-parse check: {'PASSED' if ok else 'FAILED — ' + '; '.join(missing)}")
        print(f"Fits one page: {fits_one_page}")
        if trimmed_log:
            print(f"Trimmed {len(trimmed_log)} bullet(s) to fit one page:")
            for t in trimmed_log:
                print(f"  - {t}")


def cmd_prep(args):
    """§7 Stage 5: open the real application URL, fill every field we can
    confidently and truthfully answer, screenshot the result, and stop.
    Never clicks submit. Requires the job's tailored resume to already be
    APPROVED via the approval queue (§1 rule 1: nothing proceeds without
    explicit approval first)."""
    init_db()
    resume = _load_master_resume()
    answers_bank = load_answers_bank()

    with get_conn() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (args.job_id,)).fetchone()
        if job is None:
            print(f"No job with id {args.job_id}", file=sys.stderr)
            sys.exit(1)
        job = dict(job)

        tailored = conn.execute(
            "SELECT * FROM tailored_resumes WHERE job_id = ?", (args.job_id,)
        ).fetchone()
        if tailored is None or tailored["status"] != "APPROVED":
            print(
                f"Job {args.job_id}'s tailored resume must be APPROVED first "
                f"(current: {tailored['status'] if tailored else 'no tailored resume'}). "
                "Run `engine auto-run` to tailor and prep it automatically, or `engine tailor "
                f"{args.job_id}` to do just this job.",
                file=sys.stderr,
            )
            sys.exit(1)

        docx_path = Path(tailored["docx_path"]) if tailored["docx_path"] else None
        result = prep_job(job, resume, answers_bank, docx_path, conn, headless=not args.headed)

    print(json.dumps(result, indent=2))


def cmd_auto_run(args):
    """Auto-fill pipeline: discover -> dedupe/score -> cluster -> Recruiter
    -> fill, for every SCORED job. Stops at PREPPED — nothing is submitted
    here. Your original resume file is attached to every filled application
    exactly as uploaded — nothing rewrites or reformats it. Each filled
    application then waits for `engine release` (or the web UI's progress
    report) to Submit or Discard it individually. Jobs that hit a genuine
    safety escalation (bot wall, unmapped required field) land in
    NEEDS_HUMAN, same as always."""
    init_db()
    resume = _load_master_resume()
    cfg = load_config()

    with get_conn() as conn:
        summary = run_full_pipeline(conn, resume, cfg, headless=not args.headed)

    if not summary["readiness"]["can_start"]:
        print(f"Readiness: {summary['readiness']['status']}")
        for check in summary["readiness"]["checks"]:
            if check["status"] != "pass":
                print(f"  {check['name']}: {check['detail']}")
        print("No discovery, browser automation, recruiter call, or provider request was started.")
        return

    print(f"Discovered: {json.dumps(summary['discover'])}")
    print(f"Scored: {json.dumps(summary['score'])}")
    if summary.get("recruiter_error"):
        print(f"Recruiter step failed (doesn't affect filled applications below): {summary['recruiter_error']}")
    else:
        print(f"Role families: {summary['families']['total_jobs']} jobs -> {summary['families']['total_families']} families")
        print(f"Recruiter ran for {len(summary['recruiter']['ran'])} families")

    if summary.get("skipped_overlap"):
        companies = sorted({e["company"] for e in summary["skipped_overlap"]})
        print(f"\nSkipped {len(summary['skipped_overlap'])} job(s) at already-tracked companies: {', '.join(companies)}")
    if summary.get("skipped_below_threshold"):
        print(f"Skipped {len(summary['skipped_below_threshold'])} job(s) below the fit-score/role-match bar")
    if summary.get("skipped_company_cap"):
        companies = sorted({e["company"] for e in summary["skipped_company_cap"]})
        print(f"Skipped {len(summary['skipped_company_cap'])} job(s) at the 2-per-company cap: {', '.join(companies)}")
    if summary.get("skipped_category_cap"):
        cats = sorted({e["category"] for e in summary["skipped_category_cap"]})
        print(f"Skipped {len(summary['skipped_category_cap'])} job(s) already applied in category: {', '.join(cats)}")

    print(f"\nFilled, awaiting Submit/Discard: {len(summary['prepped'])}")
    for e in summary["prepped"]:
        print(f"  [{e['job_id']}] {e['company']} — {e['title']} ({e.get('location') or 'location unknown'})")
    print(f"\nNeeds human: {len(summary['needs_human'])}")
    for e in summary["needs_human"]:
        print(f"  [{e['job_id']}] {e['company']} — {e['title']}: {e['reason']}")


def cmd_release(args):
    """Bulk convenience action: submits every currently-PREPPED
    application in one go, for anyone who's reviewed the whole progress
    report and wants to release all of it at once instead of Submit on
    each individually (web UI: /ready-to-submit)."""
    init_db()
    resume = _load_master_resume()
    answers_bank = load_answers_bank()

    with get_conn() as conn:
        summary = release_all_prepped(conn, resume, answers_bank, headless=not args.headed)

    print(f"Submitted: {len(summary['submitted'])}")
    for e in summary["submitted"]:
        print(f"  [{e['job_id']}] {e['company']} — {e['title']} ({e.get('location') or 'location unknown'}) ref={e.get('application_ref')}")
    print(f"Needs human: {len(summary['needs_human'])}")
    for e in summary["needs_human"]:
        print(f"  [{e['job_id']}] {e['company']} — {e['title']}: {e['reason']}")


def cmd_discover_contact(args):
    """§8 Stage 6: find the most plausible human at the company. Priority
    (a) named in the posting, (b) the company's own team/about page, (c)
    LinkedIn -- never automated, just a ready-made search link for you to
    check yourself. One contact attempt per company+role. Also runs the
    alumni-outreach check: flags a fellow alum spotted in a team-page bio,
    and always hands back a ready-made alumni-targeted search link."""
    init_db()
    resume = _load_master_resume()
    with get_conn() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (args.job_id,)).fetchone()
        if job is None:
            print(f"No job with id {args.job_id}", file=sys.stderr)
            sys.exit(1)
        result = discover_contact(dict(job), conn, resume=resume)
    print(json.dumps(result, indent=2))


def cmd_draft_outreach(args):
    """§9: draft the cover letter (if requested) and/or the hiring manager
    email for a job. Both land in the approval queue -- nothing is sent
    without your explicit approval."""
    init_db()
    resume = _load_master_resume()

    with get_conn() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (args.job_id,)).fetchone()
        if job is None:
            print(f"No job with id {args.job_id}", file=sys.stderr)
            sys.exit(1)
        job = dict(job)

        contact = conn.execute("SELECT * FROM contacts WHERE job_id = ?", (args.job_id,)).fetchone()
        contact = dict(contact) if contact else None
        contact_id = contact["id"] if contact else None
        contact_name = contact["name"] if contact else None
        is_alumni = bool(contact["is_alumni"]) if contact else False
        alumni_school = contact.get("alumni_school") if contact else None

        if args.cover_letter:
            result = draft_cover_letter(resume, job, contact_id, conn)
            print("Cover letter:")
            print(json.dumps(result, indent=2))

        if args.application_ref:
            result = draft_hiring_manager_email(
                resume, job, args.application_ref, contact_id, contact_name, conn,
                is_alumni=is_alumni, alumni_school=alumni_school,
            )
            print("\nHiring manager email:")
            print(json.dumps(result, indent=2))
        elif not args.cover_letter:
            print(
                "Nothing to draft: pass --cover-letter and/or --application-ref REF "
                "(the hiring manager email needs a real application reference from Stage 5).",
                file=sys.stderr,
            )


def cmd_interview(args):
    """§11b — separate command, outside the application pipeline. One
    question at a time, waiting for a real typed answer before moving on.
    Appends a new timestamped session to output/{company}_{role}/interview_prep.md
    every run; never overwrites."""
    init_db()
    with get_conn() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (args.job_id,)).fetchone()
        if job is None:
            print(f"No job with id {args.job_id}", file=sys.stderr)
            sys.exit(1)
        job = dict(job)

        def get_answer(question: str) -> str:
            return input("> ")

        run_interview_session(job, conn, get_answer_fn=get_answer)


def main():
    parser = argparse.ArgumentParser(prog="engine", description="Job application engine CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="Launch the FastAPI intake/review UI").set_defaults(func=cmd_serve)
    sub.add_parser(
        "readiness", help="Run no-charge local startup checks without starting the pipeline"
    ).set_defaults(func=cmd_readiness)

    p = sub.add_parser("parse-resume", help="Parse a resume file into the master JSON (§3)")
    p.add_argument("path", help="Path to a PDF or DOCX resume")
    p.set_defaults(func=cmd_parse_resume)

    sub.add_parser(
        "save-resume", help="Register data/master_resume.json in resume_versions (after manual edits)"
    ).set_defaults(func=cmd_save_resume)

    sub.add_parser("discover", help="Poll ATS APIs for new postings (§4)").set_defaults(func=cmd_discover)

    sub.add_parser("score", help="Dedupe + fit-score DISCOVERED jobs (§5)").set_defaults(func=cmd_score)

    p = sub.add_parser("diagnose", help="Run the Diagnoser blocking gate (§6.1)")
    p.add_argument("--force", action="store_true", help="Re-run even if already CLEARED")
    p.set_defaults(func=cmd_diagnose)

    sub.add_parser(
        "cluster-families", help="Cluster SCORED jobs into role families, no LLM call (§6)"
    ).set_defaults(func=cmd_cluster_families)

    p = sub.add_parser("recruiter", help="Run the Recruiter skill once per stale/new role family (§6.2)")
    p.add_argument("--force", action="store_true", help="Re-run even for already-fresh families")
    p.add_argument(
        "--label",
        help="Comma-separated substrings to filter families by label (case-insensitive), "
        "e.g. --label 'Data Scientist,AI Engineer,Machine Learning,Software Engineer'",
    )
    p.set_defaults(func=cmd_recruiter)

    p = sub.add_parser(
        "tailor", help="Rewriter + truth validator + metrics gaps + ATS re-parse check for one job (§6.3-§6.6)"
    )
    p.add_argument("job_id", type=int, help="jobs.id to tailor")
    p.set_defaults(func=cmd_tailor)

    p = sub.add_parser(
        "prep",
        help="Fill the real application form and stop at submit -- never clicks it (§7 Stage 5)",
    )
    p.add_argument("job_id", type=int, help="jobs.id to prep (must have an APPROVED tailored resume)")
    p.add_argument("--headed", action="store_true", help="Show the browser window instead of running headless")
    p.set_defaults(func=cmd_prep)

    p = sub.add_parser(
        "auto-run",
        help="Discover, score, and fill every eligible job -- stops at PREPPED, submits nothing",
    )
    p.add_argument("--headed", action="store_true", help="Show the browser window instead of running headless")
    p.set_defaults(func=cmd_auto_run)

    p = sub.add_parser(
        "release",
        help="Submit every currently-PREPPED application in one go (bulk alternative to per-item Submit)",
    )
    p.add_argument("--headed", action="store_true", help="Show the browser window instead of running headless")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("discover-contact", help="Find the most plausible human at the company (§8 Stage 6)")
    p.add_argument("job_id", type=int, help="jobs.id to find a contact for")
    p.set_defaults(func=cmd_discover_contact)

    p = sub.add_parser("draft-outreach", help="Draft cover letter and/or hiring manager email (§9 Stage 7)")
    p.add_argument("job_id", type=int, help="jobs.id to draft outreach for")
    p.add_argument("--cover-letter", action="store_true", help="Draft the cover letter")
    p.add_argument("--application-ref", help="Real application reference from Stage 5, to draft the hiring manager email")
    p.set_defaults(func=cmd_draft_outreach)

    p = sub.add_parser(
        "interview", help="Interactive mock interview prep, outside the application pipeline (§11b)"
    )
    p.add_argument("job_id", type=int, help="jobs.id to prep for")
    p.set_defaults(func=cmd_interview)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
