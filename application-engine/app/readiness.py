"""No-charge startup checks for the local application pipeline.

This module deliberately performs only local reads and a loopback HTTP
request.  It never calls Anthropic, an ATS, or an application site, and it
never includes secret values in its result.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Literal
from urllib.error import URLError
from urllib.request import Request, urlopen

from pydantic import ValidationError

from app.answers_bank import load_answers_bank
from app.config import load_config
from app.models import MasterResume


ReadinessStatus = Literal["go", "blocked", "needs review"]
CheckStatus = Literal["pass", "blocked", "needs review"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_STATUS_URL = "http://127.0.0.1:8000/answers-bank"
_PROVIDER_STATUS_KEY = "AUTO_APPLY_PROVIDER_STATUS"


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    status: CheckStatus
    detail: str
    remediation: str | None = None


def _check(
    name: str,
    status: CheckStatus,
    detail: str,
    remediation: str | None = None,
) -> ReadinessCheck:
    return ReadinessCheck(name, status, detail, remediation)


def _dotenv_value(path: Path, key: str) -> str | None:
    """Read one non-secret setting without exposing or logging any value."""
    if not path.is_file():
        return None
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for raw in lines:
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line.startswith(f"{key}="):
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    return None


def _setting(root: Path, key: str) -> str | None:
    process_value = os.environ.get(key)
    if process_value is not None:
        return process_value.strip()
    return _dotenv_value(root / ".env", key)


def _is_present(value) -> bool:
    # A stored False is still a deliberate answer to a yes/no question.
    return isinstance(value, bool) or (value is not None and bool(str(value).strip()))


def _answer_fields(bank) -> list[tuple[str, object]]:
    data = bank.model_dump()
    fields: list[tuple[str, object]] = []
    fields.extend((key, value) for key, value in data.items() if key not in {"demographics", "other"})
    fields.extend((f"demographics.{key}", value) for key, value in data.get("demographics", {}).items())
    fields.extend((f"other.{key}", value) for key, value in data.get("other", {}).items())
    return fields


def _configuration_check(root: Path) -> ReadinessCheck:
    config_path = root / "config.yaml"
    if not config_path.is_file():
        return _check(
            "configuration",
            "blocked",
            "config.yaml is missing.",
            "Restore config.yaml before starting the application pipeline.",
        )
    try:
        cfg = load_config(config_path)
    except (OSError, ValueError, ValidationError):
        return _check(
            "configuration",
            "blocked",
            "config.yaml is present but does not pass local validation.",
            "Correct the configuration through the intake page, then rerun readiness.",
        )

    missing = []
    if not (cfg.target_role or cfg.keywords):
        missing.append("a target role or keyword")
    if not cfg.employment_type:
        missing.append("an employment type")
    if missing:
        return _check(
            "configuration",
            "blocked",
            "Required non-secret run settings are incomplete.",
            "Set " + ", ".join(missing) + " in the intake page, then rerun readiness.",
        )

    if not _setting(root, "ANTHROPIC_API_KEY"):
        return _check(
            "configuration",
            "blocked",
            "The Anthropic credential setting is absent; its value was not read into the report.",
            "Restore the existing credential setting without pasting it into chat, then rerun readiness.",
        )
    if importlib.util.find_spec("anthropic") is None:
        return _check(
            "configuration",
            "blocked",
            "The Anthropic SDK is not installed in the active environment.",
            "Install the project requirements in the active virtual environment.",
        )
    return _check("configuration", "pass", "config.yaml validates and required local backend settings are present.")


def _answers_check(root: Path) -> ReadinessCheck:
    path = root / "data" / "answers_bank.json"
    if not path.is_file():
        return _check(
            "answers bank",
            "blocked",
            "The local Answers Bank file is missing.",
            "Open /answers-bank and save the standard answers before starting a run.",
        )
    try:
        fields = _answer_fields(load_answers_bank(path))
    except (OSError, ValueError, ValidationError):
        return _check(
            "answers bank",
            "blocked",
            "The local Answers Bank is present but cannot be validated.",
            "Open /answers-bank, correct the invalid entry, and save it again.",
        )
    answered = sum(_is_present(value) for _, value in fields)
    total = len(fields)
    missing = [name for name, value in fields if not _is_present(value)]
    if missing:
        return _check(
            "answers bank",
            "blocked",
            f"Answers Bank coverage is {answered}/{total}; {len(missing)} fields are unanswered.",
            "Complete the unanswered fields in /answers-bank, then rerun readiness.",
        )
    return _check("answers bank", "pass", f"Answers Bank coverage is {answered}/{total}.")


def _resume_check(root: Path, conn: sqlite3.Connection) -> ReadinessCheck:
    resume_path = root / "data" / "master_resume.json"
    if not resume_path.is_file():
        return _check(
            "resume",
            "blocked",
            "The master resume file is missing.",
            "Restore the master resume through the intake page before starting a run.",
        )
    try:
        MasterResume.model_validate(json.loads(resume_path.read_text()))
    except (OSError, ValueError, ValidationError):
        return _check(
            "resume",
            "blocked",
            "The master resume is present but does not pass local validation.",
            "Review and save the master resume through the intake page.",
        )

    row = conn.execute(
        "SELECT source_file_path FROM resume_versions "
        "WHERE source_file_path IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row or not row[0] or not Path(row[0]).is_file():
        return _check(
            "resume attachment",
            "blocked",
            "No existing uploaded resume file is available for application forms.",
            "Upload and save the original resume through the intake page before starting a run.",
        )
    return _check("resume", "pass", "The validated master resume and its uploaded source file are available.")


def _diagnosis_check(conn: sqlite3.Connection) -> ReadinessCheck:
    row = conn.execute("SELECT status FROM diagnoses ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return _check(
            "diagnosis gate",
            "needs review",
            "No local resume diagnosis record exists.",
            "Review the diagnosis gate before allowing an application run.",
        )
    status = str(row[0]).upper()
    if status != "CLEARED":
        return _check(
            "diagnosis gate",
            "blocked",
            "The latest local diagnosis gate is not cleared.",
            "Resolve or waive the blocking diagnosis items, then rerun readiness.",
        )
    return _check("diagnosis gate", "pass", "The latest local diagnosis gate is cleared.")


def _service_check(url: str) -> ReadinessCheck:
    request = Request(url, headers={"Accept": "text/html"}, method="GET")
    try:
        with urlopen(request, timeout=2) as response:
            if response.status != 200:
                return _check(
                    "local service",
                    "blocked",
                    "The local Answers Bank status page did not return HTTP 200.",
                    "Start the local application service on 127.0.0.1:8000, then rerun readiness.",
                )
    except (OSError, URLError):
        return _check(
            "local service",
            "blocked",
            "The local application service is not reachable over loopback HTTP.",
            "Start the local application service on 127.0.0.1:8000, then rerun readiness.",
        )
    return _check("local service", "pass", "The local Answers Bank status page is reachable over loopback HTTP.")


def _provider_check(root: Path) -> ReadinessCheck:
    """Check only a non-secret local marker; never probe the paid provider."""
    status = (_setting(root, _PROVIDER_STATUS_KEY) or "").lower()
    if status in {"ready", "confirmed", "ok"}:
        return _check("provider access", "pass", "Provider access was explicitly confirmed by a local non-secret marker.")
    if status in {"blocked", "disabled"}:
        return _check(
            "provider access",
            "blocked",
            "The local non-secret provider marker says backend access is disabled.",
            "Confirm the intended organization and access state in the provider billing console; do not enable auto-reload automatically.",
        )
    return _check(
        "provider access",
        "needs review",
        "Provider access and billing state cannot be verified locally without a provider request.",
        "Visit the provider billing console, confirm the intended organization has usable credits, then set a non-secret local readiness marker before rerunning.",
    )


def evaluate_readiness(
    conn: sqlite3.Connection,
    *,
    root: Path = PROJECT_ROOT,
    status_url: str = LOCAL_STATUS_URL,
) -> dict:
    """Return a non-secret, no-charge readiness report for a pipeline start."""
    checks = [
        _configuration_check(root),
        _service_check(status_url),
        _answers_check(root),
        _resume_check(root, conn),
        _diagnosis_check(conn),
        _provider_check(root),
    ]
    if any(item.status == "blocked" for item in checks):
        status: ReadinessStatus = "blocked"
    elif any(item.status == "needs review" for item in checks):
        status = "needs review"
    else:
        status = "go"

    remediation = []
    for item in checks:
        if item.remediation and item.remediation not in remediation:
            remediation.append(item.remediation)
    return {
        "status": status,
        "can_start": status == "go",
        "checks": [asdict(item) for item in checks],
        "remediation": remediation,
        "billing_console_unknown": (
            "The provider console is the only place that can confirm whether the $5 one-time purchase "
            "posted to the intended organization and whether API access is currently enabled."
        ),
    }
