"""§8 email inference: derive the company's address pattern from known
public addresses, verify by MX lookup and SMTP RCPT probe — never by
sending a test email. Confidence score; below 0.7 the address is presented
to the candidate as a labeled guess, never sent to silently.
"""

import re
import smtplib
import socket

import dns.resolver

_PATTERNS = {
    "first.last": lambda first, last: f"{first}.{last}",
    "flast": lambda first, last: f"{first[0]}{last}",
    "first": lambda first, last: first,
    "firstlast": lambda first, last: f"{first}{last}",
    "first_last": lambda first, last: f"{first}_{last}",
}

PROBE_SENDER = "verify-probe@example.com"  # never used to actually send anything
SMTP_TIMEOUT = 8.0


def _split_name(name: str) -> tuple[str, str] | None:
    parts = [p.lower() for p in re.sub(r"[^a-zA-Z '-]", "", name).split() if p]
    if len(parts) < 2:
        return None
    return parts[0], parts[-1]


def infer_pattern_from_known(known_addresses: list[tuple[str, str]], domain: str) -> str | None:
    """known_addresses: [(full_name, email), ...] of 2+ real, already-known
    addresses at this domain. Returns the pattern name that matches all of
    them, or None if they don't agree (or there aren't enough to compare)."""
    if len(known_addresses) < 2:
        return None
    matched_patterns = None
    for name, email in known_addresses:
        local_part = email.split("@")[0].lower()
        parsed = _split_name(name)
        if not parsed:
            return None
        first, last = parsed
        candidates = {p for p, fn in _PATTERNS.items() if fn(first, last) == local_part}
        matched_patterns = candidates if matched_patterns is None else (matched_patterns & candidates)
        if not matched_patterns:
            return None
    return next(iter(matched_patterns)) if matched_patterns else None


def build_candidate_email(name: str, domain: str, pattern: str = "first.last") -> str | None:
    parsed = _split_name(name)
    if not parsed:
        return None
    first, last = parsed
    fn = _PATTERNS.get(pattern, _PATTERNS["first.last"])
    return f"{fn(first, last)}@{domain}"


def check_mx(domain: str) -> bool:
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5.0)
        return len(answers) > 0
    except Exception:
        return False


def check_smtp_rcpt(email: str, domain: str) -> bool | None:
    """SMTP callout verification: connect, HELO, MAIL FROM, RCPT TO, then
    quit WITHOUT sending DATA — no message is ever actually sent. Returns
    True (accepted), False (rejected), or None (server doesn't permit this
    check, e.g. catch-all domains or greylisting — common and not an error)."""
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5.0)
        mx_host = str(sorted(answers, key=lambda r: r.preference)[0].exchange).rstrip(".")
    except Exception:
        return None

    try:
        with smtplib.SMTP(mx_host, 25, timeout=SMTP_TIMEOUT) as smtp:
            smtp.helo("verify.local")
            smtp.mail(PROBE_SENDER)
            code, _ = smtp.rcpt(email)
            return code == 250
    except (smtplib.SMTPException, socket.error, TimeoutError, ConnectionError):
        return None


def compute_confidence(pattern_source: str, mx_ok: bool, smtp_result: bool | None) -> float:
    """pattern_source: 'known' (derived from 2+ verified addresses) or
    'guessed' (fell back to the most common default pattern)."""
    score = 0.5 if pattern_source == "known" else 0.3
    if mx_ok:
        score += 0.2
    if smtp_result is True:
        score += 0.3
    elif smtp_result is False:
        score -= 0.3
    return max(0.0, min(1.0, score))


def infer_and_verify_email(
    name: str,
    domain: str,
    known_addresses: list[tuple[str, str]] | None = None,
) -> dict:
    """Full pipeline: derive pattern (from known addresses if we have 2+,
    else guess the most common default), build the candidate address,
    verify via MX + SMTP RCPT (never sending a real message), and score
    confidence. Returns {"email", "confidence", "pattern_source", "mx_ok", "smtp_result"}."""
    known_addresses = known_addresses or []
    pattern = infer_pattern_from_known(known_addresses, domain)
    pattern_source = "known" if pattern else "guessed"
    pattern = pattern or "first.last"

    email = build_candidate_email(name, domain, pattern)
    if email is None:
        return {"email": None, "confidence": 0.0, "pattern_source": pattern_source, "mx_ok": False, "smtp_result": None}

    mx_ok = check_mx(domain)
    smtp_result = check_smtp_rcpt(email, domain) if mx_ok else None
    confidence = compute_confidence(pattern_source, mx_ok, smtp_result)

    return {
        "email": email,
        "confidence": confidence,
        "pattern_source": pattern_source,
        "mx_ok": mx_ok,
        "smtp_result": smtp_result,
    }
