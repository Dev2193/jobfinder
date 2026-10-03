"""Role-family clustering (§6): group postings sharing a normalised title and
seniority into families — "Backend Engineer Intern" and "Software Engineer,
Backend (Intern)" become one family — so the Recruiter skill runs once per
family instead of once per job. Plain code, no LLM call.
"""

import re
import sqlite3
from datetime import datetime, timezone

from app.scoring.fit_score import detect_seniority
from app.scoring.dedupe import normalize_title

# Seniority words are stripped before clustering on title tokens — seniority
# is tracked as its own axis (role_families is keyed on family + seniority),
# so leaving these in would fracture one role into per-level "families".
_SENIORITY_TOKENS = {
    "intern", "internship", "co", "op", "coop", "entry", "level", "new", "grad",
    "graduate", "junior", "jr", "mid", "ii", "iii", "iv", "2", "3", "4",
    "senior", "sr", "staff", "principal", "lead",
}
_FILLER_TOKENS = {
    "of", "and", "the", "for", "in", "on", "a", "an", "to", "with", "at", "i",
}

JACCARD_THRESHOLD = 0.5


def family_tokens(title: str) -> frozenset[str]:
    """Tokenize a title for family clustering: normalize, then strip
    seniority markers and filler words so only the core role identity
    remains (e.g. "backend", "engineer")."""
    normalized = normalize_title(title)
    tokens = set(re.findall(r"[a-z0-9]+", normalized))
    tokens -= _SENIORITY_TOKENS
    tokens -= _FILLER_TOKENS
    return frozenset(t for t in tokens if t)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def cluster_jobs(jobs: list[dict]) -> list[list[dict]]:
    """Cluster jobs by (seniority bucket, title-token similarity). Returns a
    list of clusters, each a list of job dicts. Clustering is scoped to jobs
    sharing the same detected seniority so an "Intern" and a "Senior" posting
    of the same title never merge into one family."""
    buckets: dict[str, list[dict]] = {}
    for job in jobs:
        seniority = detect_seniority(job["title"]) or "unspecified"
        buckets.setdefault(seniority, []).append(job)

    clusters: list[list[dict]] = []
    for seniority, bucket_jobs in buckets.items():
        tokens = [family_tokens(j["title"]) for j in bucket_jobs]
        uf = _UnionFind(len(bucket_jobs))
        for i in range(len(bucket_jobs)):
            for j in range(i + 1, len(bucket_jobs)):
                if _jaccard(tokens[i], tokens[j]) >= JACCARD_THRESHOLD:
                    uf.union(i, j)

        groups: dict[int, list[int]] = {}
        for i in range(len(bucket_jobs)):
            groups.setdefault(uf.find(i), []).append(i)

        for indices in groups.values():
            clusters.append([bucket_jobs[i] for i in indices])

    return clusters


def _family_key_and_label(cluster: list[dict]) -> tuple[frozenset[str], str]:
    token_sets = [family_tokens(j["title"]) for j in cluster]
    core = token_sets[0]
    for t in token_sets[1:]:
        core = core & t
    if not core:
        # Disjoint chain (rare, only possible with >2 members linked
        # transitively) — fall back to the union so the family still has a
        # meaningful label rather than an empty one.
        core = frozenset().union(*token_sets)
    label = " ".join(word.capitalize() for word in sorted(core))
    return core, label


def assign_role_families(conn: sqlite3.Connection, statuses: tuple[str, ...] = ("SCORED",)) -> dict:
    """Cluster jobs in the given statuses into role families, upsert
    role_families rows, and set jobs.role_family_id. Returns a summary for
    display — this step is free (no LLM), so it's meant to be inspected
    before running the Recruiter (§12 build order: "verify the families are
    sensible before spending on keyword extraction")."""
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT id, title FROM jobs WHERE status IN ({placeholders})", statuses
    ).fetchall()
    jobs = [dict(r) for r in rows]

    clusters = cluster_jobs(jobs)
    now = datetime.now(timezone.utc).isoformat()
    families = []

    for cluster in clusters:
        core_tokens, label = _family_key_and_label(cluster)
        seniority = detect_seniority(cluster[0]["title"]) or "unspecified"
        family_key = f"{'|'.join(sorted(core_tokens))}::{seniority}"

        existing = conn.execute(
            "SELECT id FROM role_families WHERE family_key = ?", (family_key,)
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                "INSERT INTO role_families (family_key, label, seniority, created_at) VALUES (?, ?, ?, ?)",
                (family_key, label, seniority, now),
            )
            family_id = cursor.lastrowid
        else:
            family_id = existing["id"]

        job_ids = [j["id"] for j in cluster]
        conn.executemany(
            "UPDATE jobs SET role_family_id = ? WHERE id = ?",
            [(family_id, jid) for jid in job_ids],
        )
        families.append(
            {
                "family_id": family_id,
                "label": label,
                "seniority": seniority,
                "job_count": len(cluster),
                "sample_titles": [j["title"] for j in cluster[:5]],
            }
        )

    conn.commit()
    families.sort(key=lambda f: f["job_count"], reverse=True)
    return {"families": families, "total_jobs": len(jobs), "total_families": len(clusters)}
