"""Filing registry: an incremental scan of 10K_Filings/*/filings.csv into the status DB.

Only `scan` reads the manifests. Every other command selects filings from the DB.
Layout rules follow convertmd.discover(): rows with status downloaded/already_present,
paths must stay inside the raw folder. Unlike discover(), known rows cost only a dict
lookup and a missing file fails only that filing.
"""
from __future__ import annotations

from collections import Counter
import csv
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath

from pipeline import state
from pipeline.records import now, sha256_file

LISTED = {"downloaded", "already_present"}
CONVERTED = {"CHECKED_TEXT", "REVIEW_REQUIRED"}  # conversion statuses with usable Markdown
SUPPORTED = {".htm", ".html", ".txt", ".sgml", ".sgm"}


@dataclass(frozen=True)
class Filing:
    filing_id: str          # "<company_dir>/<file stem>", stable across runs
    company_dir: str
    ticker: str
    company_id: str
    cik: str
    year: int | None
    form: str
    accession: str
    filing_date: str
    period: str
    is_amendment: bool
    amends_filing_id: str | None
    raw_path: Path
    markdown_path: Path
    raw_sha256: str
    scan_status: str
    metadata: dict
    conversion_status: str | None = None

    @property
    def audit_path(self) -> Path:
        return self.markdown_path.with_suffix(".audit.json")


def _year(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _relative(company_dir: str, name: str) -> PurePosixPath | None:
    """The filing's path relative to the raw folder, or None if it would escape it."""
    path = PurePosixPath(company_dir) / PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in name:
        return None
    return path


def scan(cfg, db, log=print) -> dict:
    known = {row["filing_id"]: (row["raw_sha256"], row["scan_status"])
             for row in db.execute("SELECT filing_id, raw_sha256, scan_status FROM filings")}
    counts: Counter = Counter()
    listed: set[str] = set()
    seen_casefold: dict[str, str] = {}
    problems = []
    stamp = now()
    manifests = sorted(cfg.raw_dir.glob("*/filings.csv"))
    for manifest in manifests:
        company_dir = manifest.parent.name
        with manifest.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            if row.get("status") not in LISTED:
                continue
            relative = _relative(company_dir, row.get("file", ""))
            if relative is None or relative.suffix.lower() not in SUPPORTED:
                problems.append({"manifest": str(manifest), "file": row.get("file"), "problem": "path outside raw folder or unsupported type"})
                counts["rejected"] += 1
                continue
            filing_id = relative.with_suffix("").as_posix()
            folded = filing_id.casefold()
            if folded in seen_casefold:
                problems.append({"filing_id": filing_id, "problem": f"collides with {seen_casefold[folded]}"})
                counts["rejected"] += 1
                continue
            seen_casefold[folded] = filing_id
            listed.add(filing_id)
            manifest_sha = (row.get("sha256") or "").lower()
            previous = known.get(filing_id)
            # Rows registered by an older version have no sha yet and are re-registered once.
            if previous and previous[0] and previous[1] != "missing_source" and (not manifest_sha or manifest_sha == previous[0]):
                counts["unchanged"] += 1
                continue
            raw = cfg.raw_dir / Path(*relative.parts)
            exists = raw.is_file()
            if not manifest_sha and exists:
                manifest_sha = sha256_file(raw)
            if not exists:
                status = "missing_source"
            elif previous and previous[0] and manifest_sha != previous[0]:
                status = "source_changed"
            else:
                status = "registered"
            form = (row.get("form") or "").strip().upper()
            state.register(db, {
                "filing_id": filing_id, "company_dir": company_dir,
                "ticker": row.get("ticker") or company_dir.split("_")[0], "company_id": row.get("company_id", ""),
                "cik": row.get("cik", ""), "year": _year(row.get("report_year")) or _year(relative.stem[:4]),
                "form": form, "accession": row.get("accession", ""), "filing_date": row.get("filing_date", ""),
                "period": row.get("report_date", ""), "is_amendment": int(form.endswith("/A")),
                "raw_path": relative.as_posix(), "markdown_path": relative.with_suffix(".md").as_posix(),
                "raw_sha256": manifest_sha, "manifest_json": json.dumps(row, ensure_ascii=False),
                "scan_status": status, "first_seen": stamp, "last_seen": stamp})
            counts["new" if not previous else "updated"] += 1
            if status != "registered":
                counts[status] += 1
    linked = link_amendments(db)
    not_listed = len(set(known) - listed)
    db.commit()
    result = {"manifests": len(manifests), "listed": len(listed), "counts": dict(counts),
              "amendments_linked": linked, "registered_but_no_longer_listed": not_listed,
              "missing_source": db.execute("SELECT COUNT(*) FROM filings WHERE scan_status='missing_source'").fetchone()[0],
              "problems": problems[:50]}
    for problem in problems[:20]:
        log(f"  ! {problem}")
    return result


def link_amendments(db) -> int:
    """Link each unlinked amendment to the latest original of the same company and period."""
    rows = db.execute("SELECT filing_id, company_dir, period, filing_date FROM filings "
                      "WHERE is_amendment=1 AND amends_filing_id IS NULL AND period != ''").fetchall()
    linked = 0
    for row in rows:
        original = db.execute(
            "SELECT filing_id FROM filings WHERE company_dir=? AND period=? AND is_amendment=0 "
            "AND (filing_date <= ? OR ?='') ORDER BY filing_date DESC LIMIT 1",
            (row["company_dir"], row["period"], row["filing_date"], row["filing_date"])).fetchone()
        if original:
            db.execute("UPDATE filings SET amends_filing_id=? WHERE filing_id=?", (original["filing_id"], row["filing_id"]))
            linked += 1
    return linked


def _from_row(cfg, row) -> Filing:
    return Filing(
        filing_id=row["filing_id"], company_dir=row["company_dir"], ticker=row["ticker"] or "",
        company_id=row["company_id"] or "", cik=row["cik"] or "", year=row["year"], form=row["form"] or "",
        accession=row["accession"] or "", filing_date=row["filing_date"] or "", period=row["period"] or "",
        is_amendment=bool(row["is_amendment"]), amends_filing_id=row["amends_filing_id"],
        raw_path=cfg.raw_dir / row["raw_path"], markdown_path=cfg.markdown_dir / row["markdown_path"],
        raw_sha256=row["raw_sha256"] or "", scan_status=row["scan_status"] or "",
        metadata=json.loads(row["manifest_json"] or "{}"), conversion_status=row["conversion_status"])


def select(cfg, db, filing: str | None = None, company: str | None = None, year: int | None = None,
           form: str | None = None, limit: int | None = None) -> list[Filing]:
    """Filings from the registry. Company matches ticker, company folder, company_id or CIK (case-insensitive)."""
    where, args = ["raw_path IS NOT NULL"], []
    if filing:
        where.append("lower(filing_id) = ?")
        args.append(filing.replace("\\", "/").lower())
    if company:
        key = company.lower()
        where.append("(lower(ticker)=? OR lower(company_dir)=? OR lower(company_id)=? OR ltrim(cik,'0')=ltrim(?,'0'))")
        args += [key, key, key, key]
    if year:
        where.append("year = ?")
        args.append(year)
    if form:
        where.append("upper(form) = ?")
        args.append(form.upper())
    sql = f"SELECT * FROM filings WHERE {' AND '.join(where)} ORDER BY filing_id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [_from_row(cfg, row) for row in db.execute(sql, args)]


def registered_count(db) -> int:
    return db.execute("SELECT COUNT(*) FROM filings WHERE raw_path IS NOT NULL").fetchone()[0]
