"""Dedupe helpers (§5): normalize(company)+normalize(title)+location, plus a
fuzzy match on description content via SimHash for near-duplicate descriptions
(the same role posted with slightly different title/location text per board).
"""

import hashlib
import re

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|gmbh|plc)\b\.?",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_LOCATION_TAG = re.compile(r"\s*[\(\[-].*?(remote|hybrid|onsite|on-site).*?[\)\]]?\s*$", re.IGNORECASE)


def normalize_company(name: str) -> str:
    name = name.lower().strip()
    name = _COMPANY_SUFFIXES.sub("", name)
    name = _NON_ALNUM.sub(" ", name).strip()
    return re.sub(r"\s+", " ", name)


def normalize_title(title: str) -> str:
    title = title.lower().strip()
    title = _LOCATION_TAG.sub("", title)
    title = _NON_ALNUM.sub(" ", title)
    return re.sub(r"\s+", " ", title).strip()


def normalize_location(location: str | None) -> str:
    if not location:
        return ""
    location = location.lower().strip()
    location = _NON_ALNUM.sub(" ", location)
    return re.sub(r"\s+", " ", location).strip()


def content_hash(description: str) -> str:
    return hashlib.sha256((description or "").encode("utf-8")).hexdigest()


def _shingles(text: str, k: int = 3) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def simhash(text: str, bits: int = 64) -> int:
    """64-bit SimHash over word 3-shingles, for near-duplicate description matching."""
    weights = [0] * bits
    for shingle in _shingles(text):
        h = int(hashlib.md5(shingle.encode("utf-8")).hexdigest(), 16)
        for i in range(bits):
            weights[i] += 1 if (h >> i) & 1 else -1
    result = 0
    for i, w in enumerate(weights):
        if w > 0:
            result |= 1 << i
    return result


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def is_fuzzy_duplicate(hash_a: int, hash_b: int, threshold: int = 3) -> bool:
    return hamming_distance(hash_a, hash_b) <= threshold


def dedupe_key(company: str, title: str, location: str | None) -> tuple[str, str, str]:
    return normalize_company(company), normalize_title(title), normalize_location(location)


def title_token_overlap(title_a: str, title_b: str) -> float:
    """Jaccard overlap of normalized-title tokens. Description SimHash alone is
    dominated by shared company boilerplate (benefits, EEO text), so it flags
    unrelated roles at the same company as duplicates; gating on title overlap
    too keeps fuzzy dedupe scoped to what it's actually meant to catch — the
    same role reposted with slightly different formatting."""
    tokens_a = set(normalize_title(title_a).split())
    tokens_b = set(normalize_title(title_b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
