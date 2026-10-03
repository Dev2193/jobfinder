"""Stage 3 orchestration (§5): dedupe DISCOVERED jobs, auto-skip junk, fit-score
the rest, and let only the top-N (up to the weekly cap) proceed to SCORED.
Everything else stays in the DB as SKIPPED with a reason, and is reviewable.
"""

import sqlite3
from collections import defaultdict

from app.config import SearchFilters
from app.db import log_event
from app.models import MasterResume
from app.scoring.dedupe import content_hash, dedupe_key, is_fuzzy_duplicate, simhash, title_token_overlap
from app.scoring.fit_score import classify_auto_skip, compute_fit_score

FUZZY_HAMMING_THRESHOLD = 3
FUZZY_TITLE_OVERLAP_THRESHOLD = 0.5


def _mark_skipped(conn: sqlite3.Connection, job_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'SKIPPED', skip_reason = ? WHERE id = ?",
        (reason, job_id),
    )
    log_event(conn, "job_skipped", payload=reason, job_id=job_id)


def refresh_scored_jobs(conn: sqlite3.Connection, resume: MasterResume, cfg: SearchFilters) -> dict:
    """Fit scores go stale: a job's score is only ever computed once, at the
    moment it's promoted to SCORED, and is never recomputed even as the
    resume or config filters change afterward. A job scored under a looser
    config keeps ranking as if it still matched, indefinitely — e.g. a
    completely unrelated posting sitting at a leftover score of 50 from
    weeks ago, ranking above genuinely relevant fresh matches.

    Called at the start of every run_dedupe_and_score so a SCORED job's
    rank always reflects today's resume/config, not whenever it happened to
    be scored. Jobs that would now be auto-skipped outright (a filter
    tightened since) are demoted to SKIPPED rather than left sitting in
    SCORED with a misleading score."""
    rows = conn.execute("SELECT * FROM jobs WHERE status = 'SCORED'").fetchall()
    jobs = [dict(r) for r in rows]

    rescored = 0
    demoted = 0
    for job in jobs:
        reason = classify_auto_skip(job, cfg, resume)
        if reason:
            _mark_skipped(conn, job["id"], reason)
            demoted += 1
            continue
        score = compute_fit_score(job, resume, cfg)
        if score != job["fit_score"]:
            conn.execute("UPDATE jobs SET fit_score = ? WHERE id = ?", (score, job["id"]))
            rescored += 1

    conn.commit()
    return {"rescored": rescored, "demoted_stale": demoted}


def run_dedupe_and_score(
    conn: sqlite3.Connection,
    resume: MasterResume,
    cfg: SearchFilters,
) -> dict:
    refresh_summary = refresh_scored_jobs(conn, resume, cfg)

    rows = conn.execute("SELECT * FROM jobs WHERE status = 'DISCOVERED'").fetchall()
    jobs = [dict(r) for r in rows]

    summary = {
        "total_discovered": len(jobs),
        "exact_duplicates": 0,
        "fuzzy_duplicates": 0,
        "excluded_company": 0,
        "auto_skipped": 0,
        "scored": 0,
        "below_cap": 0,
        "rescored_existing": refresh_summary["rescored"],
        "demoted_stale": refresh_summary["demoted_stale"],
    }

    # 1. Exact dedupe: same normalized company + title + location.
    exact_groups: dict[tuple, list[dict]] = defaultdict(list)
    for job in jobs:
        key = dedupe_key(job["company"], job["title"], job["location"])
        exact_groups[key].append(job)

    canonical: list[dict] = []
    for group in exact_groups.values():
        group.sort(key=lambda j: j["discovered_at"])
        keeper, *dupes = group
        canonical.append(keeper)
        for dupe in dupes:
            _mark_skipped(conn, dupe["id"], "duplicate_exact")
            summary["exact_duplicates"] += 1

    # 2. Fuzzy dedupe on description SimHash, scoped to same normalized company
    #    (bounds the pairwise comparison cost and matches the real failure mode:
    #    the same company posting a near-identical req across two boards).
    by_company: dict[str, list[dict]] = defaultdict(list)
    for job in canonical:
        by_company[dedupe_key(job["company"], "", "")[0]].append(job)

    survivors: list[dict] = []
    for company_jobs in by_company.values():
        kept: list[tuple[dict, int]] = []
        for job in company_jobs:
            h = simhash(job.get("description_raw", ""))
            duplicate_of = next(
                (
                    other for other, oh in kept
                    if is_fuzzy_duplicate(h, oh, FUZZY_HAMMING_THRESHOLD)
                    and title_token_overlap(job["title"], other["title"]) >= FUZZY_TITLE_OVERLAP_THRESHOLD
                ),
                None,
            )
            if duplicate_of is not None:
                _mark_skipped(conn, job["id"], "duplicate_fuzzy")
                summary["fuzzy_duplicates"] += 1
            else:
                kept.append((job, h))
                survivors.append(job)

    # 3. Exclusion list.
    excluded_norms = {c.lower() for c in cfg.exclusion_list}
    remaining = []
    for job in survivors:
        if job["company"].lower() in excluded_norms:
            _mark_skipped(conn, job["id"], "excluded_company")
            summary["excluded_company"] += 1
        else:
            remaining.append(job)

    # 4. Auto-skip junk patterns + undergrad-appropriate hard filters (§5).
    clean = []
    for job in remaining:
        reason = classify_auto_skip(job, cfg, resume)
        if reason:
            _mark_skipped(conn, job["id"], reason)
            summary["auto_skipped"] += 1
        else:
            clean.append(job)

    # 5. Fit score everything that survived, store content_hash for future dedupe.
    for job in clean:
        score = compute_fit_score(job, resume, cfg)
        chash = content_hash(job.get("description_raw", ""))
        conn.execute(
            "UPDATE jobs SET fit_score = ?, content_hash = ? WHERE id = ?",
            (score, chash, job["id"]),
        )
        job["fit_score"] = score

    # 6. Only the top N (weekly cap) proceed to SCORED; the rest stay SKIPPED.
    clean.sort(key=lambda j: j["fit_score"], reverse=True)
    cap = cfg.weekly_application_cap
    for job in clean[:cap]:
        conn.execute("UPDATE jobs SET status = 'SCORED' WHERE id = ?", (job["id"],))
        log_event(conn, "job_scored", payload=str(job["fit_score"]), job_id=job["id"])
        summary["scored"] += 1
    for job in clean[cap:]:
        _mark_skipped(conn, job["id"], "below_weekly_cap")
        summary["below_cap"] += 1

    conn.commit()
    return summary
