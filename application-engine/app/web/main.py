"""FastAPI intake UI + resume review + answers bank + diagnosis gate (§3, §6.1)."""

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.answers_bank import AnswersBank, load_answers_bank, prefill_from_master_resume, save_answers_bank
from app.approval import queue as approval_queue
from app.approval import outreach_queue
from app.auto_pipeline import discard_one, release_all_prepped, run_full_pipeline, submit_one
from app.shippy_export import load_shippy_tracked_companies
from app.config import LocationFilter, SalaryFilter, SearchFilters, load_config, save_config
from app.dashboard import metrics as dashboard_metrics
from app.dashboard.export import export_csv
from app.db import get_conn, init_db, log_event
from app.diagnoser.diagnoser import needs_diagnosis, render_markdown, resolve_gate, run_diagnoser, set_item_state
from app.models import MasterResume
from app.outreach.contact_discovery import confirm_alumni
from app.outreach.smtp_sender import SMTPNotConfigured, send_outreach
from app.readiness import evaluate_readiness
from app.resume_parser import content_hash, parse_resume_file, save_master_resume
from app.site_chat import apply_edit, discard_edit, get_history, run_chat_turn

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "uploads"

app = FastAPI(title="Application Engine")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# The Shippy dashboard is a local file:// page, so it needs a narrow,
# read-only cross-origin route to pull the engine's status without a manual
# export. The route below still rejects non-loopback requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):
    return PlainTextResponse(f"Invalid input:\n{exc}", status_code=400)


init_db()


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _load_master_resume() -> MasterResume:
    path = Path(__file__).resolve().parent.parent.parent / "data" / "master_resume.json"
    return MasterResume.model_validate_json(path.read_text())


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
# The chat assistant can write to the app's own source — never allow it over
# the public tunnel. A plain IP check isn't enough: traffic proxied through
# Cloudflare Tunnel still shows up as 127.0.0.1 at the TCP level (cloudflared
# connects to the origin locally), so tunnel-proxied requests carry
# Cloudflare's own headers even when the peer address looks local — check
# for those too.
_TUNNEL_HEADER_MARKERS = ("cf-connecting-ip", "cf-ray", "cf-visitor")


def _is_local_request(request: Request) -> bool:
    client_host = request.client.host if request.client else None
    if client_host not in _LOOPBACK_HOSTS:
        return False
    return not any(h in request.headers for h in _TUNNEL_HEADER_MARKERS)


@app.get("/")
def intake_form(request: Request):
    cfg = load_config()
    return templates.TemplateResponse(request, "intake.html", {"cfg": cfg})


@app.post("/intake")
async def submit_intake(
    request: Request,
    target_role: str = Form(""),
    industry: str = Form(""),
    keywords: str = Form(""),
    cities: str = Form(""),
    countries: str = Form(""),
    remote: bool = Form(False),
    hybrid: bool = Form(False),
    radius_miles: str = Form(""),
    excluded_countries: str = Form(""),
    employment_type: list[str] = Form(default_factory=list),
    seniority: list[str] = Form(default_factory=list),
    salary_min: str = Form(""),
    currency: str = Form("USD"),
    include_unlisted: bool = Form(False),
    posted_within: str = Form("7d"),
    weekly_application_cap: int = Form(80),
    exclusion_list: str = Form(""),
    max_years_experience: str = Form(""),
    allow_advanced_degree_required: bool = Form(False),
    resume_file: UploadFile | None = None,
):
    cfg = SearchFilters(
        target_role=_split(target_role),
        industry=_split(industry),
        keywords=_split(keywords),
        location=LocationFilter(
            cities=_split(cities),
            countries=_split(countries),
            remote=remote,
            hybrid=hybrid,
            radius_miles=int(radius_miles) if radius_miles.strip() else None,
            excluded_countries=_split(excluded_countries),
        ),
        employment_type=employment_type or ["internship", "full-time"],
        seniority=seniority or ["intern", "entry"],
        salary=SalaryFilter(
            min=int(salary_min) if salary_min.strip() else None,
            currency=currency,
            include_unlisted=include_unlisted,
        ),
        posted_within=posted_within,
        weekly_application_cap=weekly_application_cap,
        exclusion_list=_split(exclusion_list),
        max_years_experience=int(max_years_experience) if max_years_experience.strip() else None,
        allow_advanced_degree_required=allow_advanced_degree_required,
    )
    save_config(cfg)

    if resume_file is not None and resume_file.filename:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = UPLOAD_DIR / resume_file.filename
        dest.write_bytes(await resume_file.read())
        with get_conn() as conn:
            resume = parse_resume_file(dest, conn=conn)
            log_event(conn, "resume_parsed", payload=resume_file.filename)
            # Save immediately so the upload is never lost to a missed or
            # unnoticed "Confirm" click — the review page below still lets
            # you catch/fix parser mistakes and re-save the correction.
            save_master_resume(resume, conn, source_file_path=str(dest))
            bank = prefill_from_master_resume(load_answers_bank(), resume)
            save_answers_bank(bank)
        resume_json = json.dumps(resume.model_dump(), indent=2)
        return templates.TemplateResponse(
            request, "resume_review.html", {"resume_json": resume_json, "source_file_path": str(dest)}
        )

    return RedirectResponse("/answers-bank", status_code=303)


@app.post("/resume/confirm")
def confirm_resume(request: Request, resume_json: str = Form(...), source_file_path: str = Form("")):
    resume = MasterResume.model_validate(json.loads(resume_json))
    with get_conn() as conn:
        save_master_resume(resume, conn, source_file_path=source_file_path or None)
        log_event(conn, "resume_confirmed")

        # Prefill the answers bank with anything the master resume already answers.
        bank = prefill_from_master_resume(load_answers_bank(), resume)
        save_answers_bank(bank)

    return RedirectResponse("/answers-bank", status_code=303)


@app.get("/answers-bank")
def answers_bank_form(request: Request):
    bank = load_answers_bank()
    return templates.TemplateResponse(request, "answers_bank.html", {"bank": bank})


@app.post("/answers-bank")
async def submit_answers_bank(request: Request):
    form = await request.form()

    def b(name: str) -> bool:
        return name in form

    def s(name: str, default: str = "") -> str:
        return form.get(name, default)

    bank = AnswersBank(
        years_experience_overall=s("years_experience_overall"),
        years_experience_primary_skill=s("years_experience_primary_skill"),
        currently_employed=b("currently_employed"),
        reason_for_interest_template=s("reason_for_interest_template"),
        work_authorized=b("work_authorized"),
        requires_sponsorship=b("requires_sponsorship"),
        willing_to_relocate=b("willing_to_relocate"),
        open_to_remote=b("open_to_remote"),
        open_to_hybrid=b("open_to_hybrid"),
        at_least_18=b("at_least_18"),
        has_non_compete=b("has_non_compete"),
        desired_salary=s("desired_salary"),
        notice_period=s("notice_period"),
        earliest_start_date=s("earliest_start_date"),
        referral_source=s("referral_source"),
        has_referral=b("has_referral"),
        referral_name=s("referral_name"),
        highest_education_level=s("highest_education_level"),
        willing_background_check=b("willing_background_check"),
        willing_drug_test=b("willing_drug_test"),
        has_felony_conviction=b("has_felony_conviction"),
        previously_employed_here=b("previously_employed_here"),
        requires_accommodations=b("requires_accommodations"),
        linkedin_url=s("linkedin_url"),
        portfolio_url=s("portfolio_url"),
        github_url=s("github_url"),
        demographics={
            "gender": s("gender", "prefer not to say"),
            "race_ethnicity": s("race_ethnicity", "prefer not to say"),
            "veteran_status": s("veteran_status", "prefer not to say"),
            "disability_status": s("disability_status", "prefer not to say"),
            "lgbtq_identification": s("lgbtq_identification", "prefer not to say"),
            "pronouns": s("pronouns"),
        },
    )
    save_answers_bank(bank)
    return RedirectResponse("/run", status_code=303)


def _answer_is_present(value) -> bool:
    if isinstance(value, bool):
        return True
    return value is not None and bool(str(value).strip())


def _answers_bank_summary(bank: AnswersBank) -> dict:
    data = bank.model_dump()
    demographics = data.pop("demographics", {})
    other = data.pop("other", {})
    fields = [*data.values(), *demographics.values(), *other.values()]
    answered = sum(_answer_is_present(value) for value in fields)
    total = len(fields)
    return {
        "answered_count": answered,
        "total_fields": total,
        "coverage_percent": round((answered / total) * 100) if total else 0,
        "ready": answered == total,
    }


def _date_label(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%b %-d")
    except (TypeError, ValueError):
        return value[:10]


@app.get("/api/dashboard-sync")
def dashboard_sync(request: Request, since: str = "2026-08-01"):
    """Read-only local bridge for the Shippy dashboard.

    It intentionally returns application metadata and Answers Bank coverage,
    not the answer values themselves. The date boundary keeps the dashboard's
    existing Aug-2026 onward view intact while future engine runs continue to
    appear automatically.
    """
    if not _is_local_request(request):
        return PlainTextResponse("Dashboard sync is available only on this machine.", status_code=403)

    bank = load_answers_bank()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                a.id AS application_id,
                a.job_id,
                a.status AS engine_status,
                a.application_ref,
                a.submitted_at,
                a.blocked_reason,
                a.unmapped_fields,
                a.filled_fields,
                a.created_at,
                a.updated_at,
                j.company,
                j.title,
                j.location,
                j.url,
                j.fit_score,
                COALESCE(a.submitted_at, a.created_at, j.discovered_at) AS recorded_at
            FROM applications a
            JOIN jobs j ON j.id = a.job_id
            WHERE date(COALESCE(a.submitted_at, a.created_at, j.discovered_at)) >= date(?)
            ORDER BY recorded_at DESC, a.id DESC
            """,
            (since,),
        ).fetchall()

    applications = []
    for row in rows:
        item = dict(row)
        blocked_reason = item.get("blocked_reason") or ""
        engine_status = item["engine_status"]
        if engine_status == "SUBMITTED":
            dashboard_status = "applied"
        elif engine_status == "NEEDS_HUMAN":
            dashboard_status = "needs-human"
        else:
            dashboard_status = "applied"
        note = f"Application Engine: {engine_status.replace('_', ' ').title()}."
        if item.get("application_ref"):
            note += f" Reference: {item['application_ref']}."
        if blocked_reason:
            note += f" {blocked_reason}"
        applications.append(
            {
                "id": f"application-engine-{item['job_id']}",
                "company": item["company"],
                "role": item["title"],
                "location": item.get("location") or "Location not set",
                "status": dashboard_status,
                "engineStatus": engine_status,
                "round": "",
                "date": "",
                "updated": _date_label(item.get("updated_at") or item.get("recorded_at")),
                "score": item.get("fit_score") or 0,
                "initials": (item["company"] or "?")[:1].upper(),
                "color": "#4d6bfe",
                "requirements": [],
                "tips": [],
                "notes": note,
                "url": item["url"],
                "source": "application-engine",
                "sourceUrl": item["url"],
            }
        )

    return {
        "source": "application-engine",
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "since": since,
        "answers_bank": _answers_bank_summary(bank),
        "applications": applications,
    }


@app.get("/api/readiness")
def readiness_api(request: Request):
    """Non-secret, no-charge startup status for the local auto-apply agent."""
    if not _is_local_request(request):
        return PlainTextResponse("Readiness is available only on this machine.", status_code=403)
    with get_conn() as conn:
        return evaluate_readiness(conn)


def _run_diagnoser_if_needed(conn) -> tuple[int | None, str]:
    resume_path = Path(__file__).resolve().parent.parent.parent / "data" / "master_resume.json"
    if not resume_path.exists():
        return None, "NO_RESUME"
    resume = MasterResume.model_validate(json.loads(resume_path.read_text()))
    cfg = load_config()

    if needs_diagnosis(resume, conn):
        diagnosis_id = run_diagnoser(resume, cfg, conn)
    else:
        resume_hash = content_hash(resume)
        version_row = conn.execute(
            "SELECT id FROM resume_versions WHERE content_hash = ?", (resume_hash,)
        ).fetchone()
        diag_row = conn.execute(
            "SELECT id FROM diagnoses WHERE resume_version_id = ? ORDER BY id DESC LIMIT 1",
            (version_row["id"],),
        ).fetchone()
        diagnosis_id = diag_row["id"]

    status_row = conn.execute("SELECT status FROM diagnoses WHERE id = ?", (diagnosis_id,)).fetchone()
    return diagnosis_id, status_row["status"]


@app.get("/diagnosis")
def diagnosis_page(request: Request):
    with get_conn() as conn:
        diagnosis_id, status = _run_diagnoser_if_needed(conn)
        if diagnosis_id is None:
            return templates.TemplateResponse(
                request, "diagnosis.html", {"diagnosis_id": None, "status": "NO_RESUME"}
            )
        items = conn.execute(
            "SELECT * FROM diagnosis_items WHERE diagnosis_id = ? ORDER BY ordinal", (diagnosis_id,)
        ).fetchall()

    by_category = {"ats_killer": [], "section_diagnosis": [], "missing_signal": [], "top_fix": []}
    for item in items:
        by_category[item["category"]].append(dict(item))

    return templates.TemplateResponse(
        request,
        "diagnosis.html",
        {
            "diagnosis_id": diagnosis_id,
            "status": status,
            "ats_killers": by_category["ats_killer"],
            "section_diagnosis": by_category["section_diagnosis"],
            "missing_signals": by_category["missing_signal"],
            "top_fixes": by_category["top_fix"],
        },
    )


@app.post("/diagnosis/item/{item_id}")
async def update_diagnosis_item(item_id: int, request: Request):
    form = await request.form()
    with get_conn() as conn:
        set_item_state(
            conn,
            item_id,
            resolved="resolved" in form,
            waived="waived" in form,
        )
    return RedirectResponse("/diagnosis", status_code=303)


@app.post("/diagnosis/{diagnosis_id}/clear")
def clear_diagnosis_gate(diagnosis_id: int):
    with get_conn() as conn:
        resolve_gate(conn, diagnosis_id)
    return RedirectResponse("/diagnosis", status_code=303)


@app.get("/tailoring-issues")
def tailoring_issues(request: Request):
    """Read-only: jobs where the Rewriter + truth validator loop rejected
    twice. Not an approval queue — there's nothing to approve here, just
    genuine safety escalations that need a person to look at."""
    with get_conn() as conn:
        needs_human = approval_queue.list_needs_human(conn)
    return templates.TemplateResponse(request, "tailoring_issues.html", {"needs_human": needs_human})


@app.get("/tailoring-issues/{tailored_resume_id}")
def tailoring_issue_detail(request: Request, tailored_resume_id: int):
    with get_conn() as conn:
        item = approval_queue.get_item(conn, tailored_resume_id)
        if item is None:
            return PlainTextResponse("Not found", status_code=404)
        validator_log = json.loads(item["validator_log"]) if item["validator_log"] else []
    return templates.TemplateResponse(
        request, "tailoring_issue_detail.html", {"item": item, "validator_log": validator_log}
    )


@app.get("/run")
def run_page(request: Request):
    with get_conn() as conn:
        scored_waiting = conn.execute(
            """
            SELECT COUNT(*) AS n FROM jobs j
            LEFT JOIN applications a ON a.job_id = j.id
            WHERE j.status IN ('SCORED', 'TAILORED')
              AND (a.id IS NULL OR a.status NOT IN ('PREPPED', 'SUBMITTED', 'NEEDS_HUMAN'))
            """
        ).fetchone()["n"]
        readiness = evaluate_readiness(conn)
    return templates.TemplateResponse(
        request, "run.html", {"scored_waiting": scored_waiting, "readiness": readiness}
    )


@app.post("/run")
def run_pipeline(
    request: Request,
    limit: str = Form(""),
    exclude_shippy: bool = Form(False),
):
    """Discover -> score -> fill -> cluster -> Recruiter, all in one click.
    Stops at PREPPED — nothing is submitted here. Your original resume file
    is attached to every filled application exactly as uploaded. Each
    filled application then waits in the progress report for an explicit
    per-item Submit or Discard.

    limit caps how many jobs get filled this run (highest fit score
    first). exclude_shippy skips companies already tracked in the Shippy
    dashboard, to avoid duplicate applications."""
    resume = _load_master_resume()
    cfg = load_config()
    exclude_companies = load_shippy_tracked_companies() if exclude_shippy else None
    with get_conn() as conn:
        summary = run_full_pipeline(
            conn, resume, cfg, headless=True,
            limit=int(limit) if limit.strip() else None,
            exclude_companies=exclude_companies,
        )
    return templates.TemplateResponse(request, "run_result.html", {"summary": summary})


@app.get("/ready-to-submit")
def ready_to_submit(request: Request):
    """The progress report: every filled-but-not-yet-decided application,
    by company and role, each with its own Submit / Discard action."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.job_id, a.screenshot_path, j.company, j.title, j.location, j.fit_score
            FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE a.status = 'PREPPED'
            ORDER BY j.fit_score DESC
            """
        ).fetchall()
    return templates.TemplateResponse(request, "ready_to_submit.html", {"applications": [dict(r) for r in rows]})


@app.post("/ready-to-submit/{job_id}/submit")
def submit_one_route(job_id: int):
    """Per-application Submit — the only place besides the bulk release
    button that clicks a real submit button, and only for the one job id
    given, only if it's currently PREPPED."""
    resume = _load_master_resume()
    answers_bank = load_answers_bank()
    with get_conn() as conn:
        submit_one(conn, resume, answers_bank, job_id)
    return RedirectResponse("/ready-to-submit", status_code=303)


@app.post("/ready-to-submit/{job_id}/discard")
def discard_one_route(job_id: int):
    """Per-application Discard — never submits; removes the job from the
    progress report and from future auto-fill runs."""
    with get_conn() as conn:
        discard_one(conn, job_id)
    return RedirectResponse("/ready-to-submit", status_code=303)


@app.post("/ready-to-submit/release")
def release_all_route(request: Request):
    """Bulk convenience action: submit everything currently PREPPED in one
    go, for anyone who's reviewed the whole progress report already."""
    resume = _load_master_resume()
    answers_bank = load_answers_bank()
    with get_conn() as conn:
        summary = release_all_prepped(conn, resume, answers_bank, headless=True)
    return templates.TemplateResponse(request, "release_result.html", {"summary": summary})


@app.get("/applications")
def applications_list(request: Request):
    """The submission log: every job that reached PREPPED or beyond, newest
    first — which companies it's applied to, for what role, and where."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.*, j.company, j.title, j.location
            FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE a.status IN ('PREPPED', 'NEEDS_HUMAN', 'SUBMITTED', 'DISCARDED')
            ORDER BY a.updated_at DESC
            """
        ).fetchall()
    return templates.TemplateResponse(request, "applications_list.html", {"applications": [dict(r) for r in rows]})


@app.get("/applications/{job_id}")
def application_detail(request: Request, job_id: int):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT a.*, j.company, j.title, j.url, j.location
            FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE a.job_id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return PlainTextResponse("Not found", status_code=404)
        item = dict(row)
        item["unmapped_fields_list"] = json.loads(item["unmapped_fields"]) if item.get("unmapped_fields") else []
        item["filled_fields_list"] = json.loads(item["filled_fields"]) if item.get("filled_fields") else []
    return templates.TemplateResponse(request, "application_detail.html", {"item": item})


@app.get("/applications/{job_id}/screenshot")
def application_screenshot(job_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT screenshot_path FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    if row is None or not row["screenshot_path"]:
        return PlainTextResponse("No screenshot available", status_code=404)
    path = Path(row["screenshot_path"])
    if not path.exists():
        return PlainTextResponse("Screenshot missing on disk", status_code=404)
    return FileResponse(path, media_type="image/png")


VALID_OUTCOMES = {"REPLIED", "INTERVIEW", "OFFER", "REJECTED_BY_COMPANY"}


@app.post("/applications/{job_id}/outcome")
def mark_outcome(job_id: int, outcome: str = Form(...)):
    """No inbox integration exists, so post-submission outcomes are marked
    manually — this is what feeds the dashboard's funnel and response-rate
    numbers (§11 Stage 9)."""
    if outcome not in VALID_OUTCOMES:
        return PlainTextResponse("Invalid outcome", status_code=400)
    with get_conn() as conn:
        job = conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return PlainTextResponse("Job not found", status_code=404)
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (outcome, job_id))
        log_event(conn, "outcome_marked", payload=outcome, job_id=job_id)
        conn.commit()
    return RedirectResponse(f"/applications/{job_id}", status_code=303)


@app.get("/outreach")
def outreach_list(request: Request):
    with get_conn() as conn:
        pending = outreach_queue.list_pending(conn)
        needs_human = outreach_queue.list_needs_human(conn)
        approved = outreach_queue.list_approved(conn)
    return templates.TemplateResponse(
        request, "outreach_list.html", {"pending": pending, "needs_human": needs_human, "approved": approved}
    )


@app.get("/outreach/{outreach_id}")
def outreach_detail(request: Request, outreach_id: int):
    with get_conn() as conn:
        item = outreach_queue.get_item(conn, outreach_id)
    if item is None:
        return PlainTextResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        request, "outreach_detail.html", {"item": item, "error": request.query_params.get("error")}
    )


@app.post("/outreach/{outreach_id}/approve")
def approve_outreach(outreach_id: int):
    with get_conn() as conn:
        outreach_queue.approve(conn, outreach_id)
    return RedirectResponse(f"/outreach/{outreach_id}", status_code=303)


@app.post("/outreach/{outreach_id}/reject")
async def reject_outreach(outreach_id: int, request: Request):
    form = await request.form()
    with get_conn() as conn:
        outreach_queue.reject(conn, outreach_id, form.get("reason", ""))
    return RedirectResponse("/outreach", status_code=303)


@app.post("/outreach/{outreach_id}/edit")
async def edit_outreach(outreach_id: int, subject: str = Form(...), body: str = Form(...)):
    with get_conn() as conn:
        outreach_queue.edit_then_approve(conn, outreach_id, subject, body)
    return RedirectResponse(f"/outreach/{outreach_id}", status_code=303)


@app.post("/outreach/{outreach_id}/confirm-alumni")
async def confirm_alumni_route(outreach_id: int, request: Request):
    """A human checked the alumni_search_url themselves (LinkedIn's alumni
    tool can't be queried automatically) and is recording what they found."""
    form = await request.form()
    contact_id = form.get("contact_id")
    name = form.get("name", "")
    if contact_id:
        with get_conn() as conn:
            confirm_alumni(conn, int(contact_id), name)
    return RedirectResponse(f"/outreach/{outreach_id}", status_code=303)


@app.post("/outreach/{outreach_id}/send")
def send_outreach_route(outreach_id: int):
    """The only place that can trigger a real SMTP send. Mirrors the resume
    pipeline's approve-then-separately-submit gate (§1 rule 1): approving an
    outreach draft unlocks this button, but actually sending the email to a
    real person still takes one more explicit human click."""
    with get_conn() as conn:
        try:
            send_outreach(conn, outreach_id)
        except (SMTPNotConfigured, RuntimeError, ValueError) as exc:
            return RedirectResponse(f"/outreach/{outreach_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/outreach/{outreach_id}", status_code=303)


@app.get("/dashboard")
def dashboard(request: Request):
    cfg = load_config()
    with get_conn() as conn:
        context = {
            "applications": dashboard_metrics.applications_summary(conn, cfg.weekly_application_cap),
            "funnel": dashboard_metrics.funnel_counts(conn),
            "response_by_source": dashboard_metrics.response_rate_by_source(conn),
            "response_by_role_family": dashboard_metrics.response_rate_by_role_family(conn),
            "response_by_outreach": dashboard_metrics.response_rate_by_outreach_sent(conn),
            "tokens": dashboard_metrics.token_spend_summary(conn),
            "queue": dashboard_metrics.queue_summary(conn),
        }
    return templates.TemplateResponse(request, "dashboard.html", context)


@app.get("/dashboard/export.csv")
def dashboard_export(request: Request):
    with get_conn() as conn:
        csv_text = export_csv(conn)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=application_engine_export.csv"},
    )


@app.get("/chat")
def chat_page(request: Request):
    """In-app coding assistant — local machine only (see _is_local_request):
    it can propose edits to this app's own source, so it's never reachable
    over the public tunnel, only from a browser on this machine."""
    if not _is_local_request(request):
        return PlainTextResponse(
            "The site assistant only works from this machine, not over the public link.", status_code=403
        )
    with get_conn() as conn:
        history = get_history(conn)
    return templates.TemplateResponse(request, "chat.html", {"history": history})


@app.post("/chat/message")
def chat_message(request: Request, message: str = Form(...)):
    if not _is_local_request(request):
        return PlainTextResponse("Forbidden", status_code=403)
    with get_conn() as conn:
        run_chat_turn(conn, message)
    return RedirectResponse("/chat", status_code=303)


@app.post("/chat/apply/{edit_id}")
def chat_apply(request: Request, edit_id: int):
    if not _is_local_request(request):
        return PlainTextResponse("Forbidden", status_code=403)
    with get_conn() as conn:
        apply_edit(conn, edit_id)
    return RedirectResponse("/chat", status_code=303)


@app.post("/chat/discard/{edit_id}")
def chat_discard(request: Request, edit_id: int):
    if not _is_local_request(request):
        return PlainTextResponse("Forbidden", status_code=403)
    with get_conn() as conn:
        discard_edit(conn, edit_id)
    return RedirectResponse("/chat", status_code=303)
