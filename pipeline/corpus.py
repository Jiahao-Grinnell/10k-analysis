"""The filing list, built from the download manifests via convertmd.discover()."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from convertmd import discover


@dataclass(frozen=True)
class Filing:
    filing_id: str          # "<company_dir>/<file stem>", stable across runs
    company_dir: str
    ticker: str
    company_id: str
    year: int | None
    form: str
    raw_path: Path
    markdown_path: Path
    metadata: dict

    @property
    def audit_path(self) -> Path:
        return self.markdown_path.with_suffix(".audit.json")


def _year(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def list_filings(raw_dir: Path, markdown_dir: Path) -> list[Filing]:
    jobs, _ = discover(raw_dir.resolve())
    filings = []
    for source, relative, row in jobs:
        filing_id = relative.with_suffix("").as_posix()
        company_dir = relative.parts[0] if len(relative.parts) > 1 else ""
        filings.append(Filing(
            filing_id=filing_id,
            company_dir=company_dir,
            ticker=row.get("ticker", "") or company_dir.split("_")[0],
            company_id=row.get("company_id", ""),
            year=_year(row.get("report_year")) or _year(relative.stem[:4]),
            form=row.get("form", ""),
            raw_path=source,
            markdown_path=markdown_dir.resolve() / relative,
            metadata=dict(row),
        ))
    ids = [f.filing_id.casefold() for f in filings]
    if len(set(ids)) != len(ids):
        raise ValueError("Filing IDs are not unique")
    return filings


def select(filings: list[Filing], filing: str | None = None, company: str | None = None,
           year: int | None = None, limit: int | None = None) -> list[Filing]:
    """Filter by exact filing ID, ticker or company folder (case-insensitive), and year."""
    chosen = filings
    if filing:
        chosen = [f for f in chosen if f.filing_id.casefold() == filing.replace("\\", "/").casefold()]
    if company:
        key = company.casefold()
        chosen = [f for f in chosen if key in {f.ticker.casefold(), f.company_dir.casefold(), f.company_id.casefold()}]
    if year:
        chosen = [f for f in chosen if f.year == year]
    return chosen[:limit] if limit else chosen
