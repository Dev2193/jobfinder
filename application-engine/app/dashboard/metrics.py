"""§11 Stage 9 — Tally and dashboard. Funnel counts are cumulative ("how
many jobs ever reached this stage"), derived from existence of downstream
artifacts (a tailored_resumes row, an applications row past a given status,
etc.) rather than jobs.status alone, since a job's status field only holds
its *current* stage and moves past earlier ones as it progresses.

Post-submission outcomes (replied/interview/offer) have no automatic
detection — there's no inbox integration — so they're read from jobs.status
values set manually via the application detail page's outcome buttons.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

RESPONDED_STATUSES = ("REPLIED", "INTERVIEW", "OFFER", "REJECTED_BY_COMPANY")
SUBMITTED_OR_BEYOND = ("SUBMITTED", "REPLIED", "INTERVIEW", "OFFER", "REJECTED_BY_COMPANY")


def applications_summary(conn: sqlite3.Connection, weekly_cap: int) -> dict:
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    this_week = conn.execute(
        "SELECT COUNT(*) AS n FROM applications WHERE status IN (%s) AND submitted_at >= ?"
        % ",".join("?" * len(SUBMITTED_OR_BEYOND)),
        (*SUBMITTED_OR_BEYOND, week_ago),
    ).fetchone()["n"]
    all_time = conn.execute(
        "SELECT COUNT(*) AS n FROM applications WHERE status IN (%s)" % ",".join("?" * len(SUBMITTED_OR_BEYOND)),
        SUBMITTED_OR_BEYOND,
    ).fetchone()["n"]
    return {"this_week": this_week, "cap": weekly_cap, "all_time": all_time}


def funnel_counts(conn: sqlite3.Connection) -> list[dict]:
    discovered = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    scored = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE fit_score IS NOT NULL").fetchone()["n"]
    tailored = conn.execute("SELECT COUNT(DISTINCT job_id) AS n FROM tailored_resumes").fetchone()["n"]
    prepped = conn.execute(
        "SELECT COUNT(DISTINCT job_id) AS n FROM applications WHERE status IN (%s)"
        % ",".join("?" * (len(SUBMITTED_OR_BEYOND) + 1)),
        ("PREPPED", *SUBMITTED_OR_BEYOND),
    ).fetchone()["n"]
    submitted = conn.execute(
        "SELECT COUNT(DISTINCT job_id) AS n FROM applications WHERE status IN (%s)"
        % ",".join("?" * len(SUBMITTED_OR_BEYOND)),
        SUBMITTED_OR_BEYOND,
    ).fetchone()["n"]
    outreach_sent = conn.execute(
        "SELECT COUNT(DISTINCT job_id) AS n FROM outreach WHERE status = 'SENT'"
    ).fetchone()["n"]
    replied = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE status IN (%s)" % ",".join("?" * len(RESPONDED_STATUSES)),
        RESPONDED_STATUSES,
    ).fetchone()["n"]
    interview = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('INTERVIEW', 'OFFER')").fetchone()["n"]
    offer = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status = 'OFFER'").fetchone()["n"]

    return [
        {"stage": "Discovered", "count": discovered},
        {"stage": "Scored", "count": scored},
        {"stage": "Tailored", "count": tailored},
        {"stage": "Prepped", "count": prepped},
        {"stage": "Submitted", "count": submitted},
        {"stage": "Outreach sent", "count": outreach_sent},
        {"stage": "Replied", "count": replied},
        {"stage": "Interview", "count": interview},
        {"stage": "Offer", "count": offer},
    ]


def _response_rate_rows(conn: sqlite3.Connection, group_by_sql: str, joins: str = "") -> list[dict]:
    placeholders = ",".join("?" * len(RESPONDED_STATUSES))
    rows = conn.execute(
        f"""
        SELECT {group_by_sql} AS grp,
               COUNT(*) AS submitted_count,
               SUM(CASE WHEN j.status IN ({placeholders}) THEN 1 ELSE 0 END) AS responded_count
        FROM jobs j
        JOIN applications a ON a.job_id = j.id
        {joins}
        WHERE a.status IN ({",".join("?" * len(SUBMITTED_OR_BEYOND))})
        GROUP BY grp
        """,
        (*RESPONDED_STATUSES, *SUBMITTED_OR_BEYOND),
    ).fetchall()
    return [
        {
            "group": r["grp"],
            "submitted": r["submitted_count"],
            "responded": r["responded_count"],
            "rate": round(r["responded_count"] / r["submitted_count"] * 100, 1) if r["submitted_count"] else 0.0,
        }
        for r in rows
    ]


def response_rate_by_source(conn: sqlite3.Connection) -> list[dict]:
    return _response_rate_rows(conn, "j.source")


def response_rate_by_role_family(conn: sqlite3.Connection) -> list[dict]:
    return _response_rate_rows(
        conn, "COALESCE(rf.label, '(unclustered)')", "LEFT JOIN role_families rf ON rf.id = j.role_family_id"
    )


def response_rate_by_outreach_sent(conn: sqlite3.Connection) -> list[dict]:
    return _response_rate_rows(
        conn,
        "CASE WHEN o.id IS NOT NULL THEN 'Outreach sent' ELSE 'No outreach' END",
        "LEFT JOIN outreach o ON o.job_id = j.id AND o.status = 'SENT'",
    )


def token_spend_summary(conn: sqlite3.Connection) -> dict:
    by_stage = conn.execute(
        """
        SELECT stage, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
               SUM(cached_tokens) AS cached_tokens, COUNT(*) AS calls
        FROM token_ledger GROUP BY stage ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC
        """
    ).fetchall()

    total_tokens = conn.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS n FROM token_ledger"
    ).fetchone()["n"]
    total_cached = conn.execute("SELECT COALESCE(SUM(cached_tokens), 0) AS n FROM token_ledger").fetchone()["n"]
    completed_applications = conn.execute(
        "SELECT COUNT(*) AS n FROM tailored_resumes WHERE status IN ('VALIDATED', 'APPROVED')"
    ).fetchone()["n"]
    per_application = round(total_tokens / completed_applications, 0) if completed_applications else 0

    # Marginal per-job cost: just the stages that run once per job in steady
    # state (Rewriter + Truth Validator) — excludes one-time/shared costs
    # (Diagnoser once per resume, Recruiter once per role family) and any
    # testing/iteration overhead, so it's the more representative number for
    # judging whether the pipeline is within budget as it scales to 80/week.
    marginal_row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS n FROM token_ledger "
        "WHERE stage IN ('rewriter', 'truth_validator')"
    ).fetchone()
    marginal_per_job = round(marginal_row["n"] / completed_applications, 0) if completed_applications else 0

    trend = []
    for i in range(6, -1, -1):
        day_start = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        day_end = (datetime.now(timezone.utc) - timedelta(days=i - 1)).strftime("%Y-%m-%d")
        day_total = conn.execute(
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS n FROM token_ledger WHERE ts >= ? AND ts < ?",
            (day_start, day_end),
        ).fetchone()["n"]
        trend.append({"date": day_start, "tokens": day_total})

    return {
        "by_stage": [dict(r) for r in by_stage],
        "total_all_time": total_tokens,
        "total_cached": total_cached,
        "completed_applications": completed_applications,
        "per_application": per_application,
        "marginal_per_job": marginal_per_job,
        "seven_day_trend": trend,
    }


def queue_summary(conn: sqlite3.Connection) -> dict:
    ready_to_submit = conn.execute(
        "SELECT COUNT(*) AS n FROM applications WHERE status = 'PREPPED'"
    ).fetchone()["n"]
    awaiting_outreach_approval = conn.execute(
        "SELECT COUNT(*) AS n FROM outreach WHERE status = 'DRAFT'"
    ).fetchone()["n"]
    needs_human = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status = 'NEEDS_HUMAN'").fetchone()["n"]

    seven_days_ago_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    follow_ups_due = conn.execute(
        """
        SELECT COUNT(*) AS n FROM outreach
        WHERE status = 'SENT' AND follow_up_sent_at IS NULL AND sent_at IS NOT NULL AND sent_at <= ?
        """,
        (seven_days_ago_cutoff,),
    ).fetchone()["n"]

    return {
        "ready_to_submit": ready_to_submit,
        "awaiting_outreach_approval": awaiting_outreach_approval,
        "needs_human": needs_human,
        "follow_ups_due": follow_ups_due,
    }
