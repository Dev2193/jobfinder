"""§7 Application reference capture — scrape a confirmation page for a
reference/requisition/application ID after a real submission. Built now so
it's ready the moment a real submit happens; not exercised in this build
since Stage 5 never actually submits (§7: "This stage does not submit.")."""

import re

_PATTERNS = [
    re.compile(r"Application\s*ID[:\s]*([A-Za-z0-9-]+)", re.I),
    re.compile(r"Reference[:\s#]*([A-Za-z0-9-]+)", re.I),
    re.compile(r"\b(REQ-[A-Za-z0-9]+)", re.I),
    re.compile(r"\b(JR-[A-Za-z0-9]+)", re.I),
    re.compile(r"Job\s*ID[:\s]*([A-Za-z0-9-]+)", re.I),
]
_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


def scrape_application_ref(page_text: str, confirmation_url: str = "") -> str | None:
    for pattern in _PATTERNS:
        match = pattern.search(page_text)
        if match:
            return match.group(1)
    match = _UUID_PATTERN.search(confirmation_url)
    if match:
        return match.group(0)
    return None
