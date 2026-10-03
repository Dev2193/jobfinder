"""Shared HTTP fetch with backoff for ATS API polling (§1 rule 6, §4)."""

import time

import httpx

USER_AGENT = "application-engine/0.1 (personal job search tool; contact via LinkedIn profile owner)"


def fetch_json(url: str, *, max_retries: int = 4, timeout: float = 15.0) -> dict | list | None:
    """GET a JSON endpoint, backing off on 429/5xx. Returns None on a persistent
    or client (4xx other than 429) error rather than raising, so a single dead
    company slug never takes down a whole discovery run."""
    delay = 1.0
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            try:
                resp = client.get(url)
            except httpx.RequestError:
                if attempt == max_retries:
                    return None
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == max_retries:
                    return None
                retry_after = resp.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else delay)
                delay *= 2
                continue
            return None
    return None
