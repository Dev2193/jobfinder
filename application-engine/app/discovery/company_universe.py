"""Load the `ats:slug` company universe list that discovery polls (§4)."""

from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "company_universe.txt"


@dataclass
class CompanyEntry:
    ats: str
    slug: str


def load_company_universe(path: Path = DEFAULT_PATH) -> list[CompanyEntry]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        ats, slug = line.split(":", 1)
        entries.append(CompanyEntry(ats=ats.strip().lower(), slug=slug.strip()))
    return entries
