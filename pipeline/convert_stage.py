"""Conversion stage: index existing Markdown_10K results; convert only filings with no output.

convertmd.py is reused unchanged. An existing Markdown file is never rewritten, except
for a filing whose raw file changed, and only when the user asks (--reconvert-changed).
A damaged or incomplete output is reported as failed for manual review.
"""
from __future__ import annotations

from collections import Counter
import csv
import json
from pathlib import Path

from convertmd import conversion_job
from pipeline import state, tokens
from pipeline.corpus import CONVERTED
from pipeline.records import now, read_json, sha256_file, write_json

STAGE = "convert"


def _from_audit(filing, encoding: str) -> tuple[str, str | None, int | None, str | None]:
    """(conversion_status, raw_format, markdown_tokens, error) for a filing with existing outputs."""
    report = read_json(filing.audit_path)
    if report.get("markdown_sha256") != sha256_file(filing.markdown_path):
        return "ERROR", None, None, "Markdown hash differs from its audit record; not rewritten"
    documents = report.get("documents") or []
    raw_format = documents[0].get("format") if documents else None
    markdown_tokens = report.get("tokens", {}).get(encoding, {}).get("markdown")
    return report.get("quality_status", "ERROR"), raw_format, markdown_tokens, None


def _source_changed(filing) -> bool:
    if filing.scan_status == "source_changed":
        return True
    audited = read_json(filing.audit_path).get("source_sha256")
    return bool(filing.raw_sha256 and audited and audited != filing.raw_sha256)


def run(cfg, db, filings: list, convert_missing: bool = True, reconvert_changed: bool = False,
        tokenizers: dict | None = None, log=print) -> dict:
    """Index (and, if allowed, convert) each filing; record status in the database.

    Existing Markdown is rewritten only for a filing whose raw file changed, and only
    when the user passes reconvert_changed.
    """
    rows = []

    def convert(filing):
        nonlocal tokenizers
        if tokenizers is None:
            tokenizers = {cfg.encoding: tokens.encoding(cfg.encoding, str(cfg.tiktoken_cache))}
        conversion_job(filing.raw_path, filing.markdown_path, tokenizers, filing.metadata)
        db.execute("UPDATE filings SET scan_status='registered' WHERE filing_id=?", (filing.filing_id,))
        return _from_audit(filing, next(iter(tokenizers)))

    for index, filing in enumerate(filings, 1):
        started = now()
        md_exists, audit_exists = filing.markdown_path.is_file(), filing.audit_path.is_file()
        try:
            if filing.scan_status == "missing_source" or not filing.raw_path.is_file():
                status, raw_format, md_tokens, action = "MISSING_SOURCE", None, None, "indexed"
                error = "Raw file listed in filings.csv is missing"
            elif md_exists and audit_exists:
                if not _source_changed(filing):
                    status, raw_format, md_tokens, error = _from_audit(filing, cfg.encoding)
                    action = "indexed"
                elif reconvert_changed:
                    status, raw_format, md_tokens, error = convert(filing)
                    action = "reconverted"
                else:
                    status, raw_format, md_tokens, action = "ERROR", None, None, "indexed"
                    error = "Raw file changed since conversion; run `convert --reconvert-changed` to rebuild it"
            elif md_exists or audit_exists:
                status, raw_format, md_tokens = "ERROR", None, None
                error = "Partial conversion output (Markdown or audit missing); not rewritten"
                action = "indexed"
            elif convert_missing:
                status, raw_format, md_tokens, error = convert(filing)
                action = "converted"
            else:
                status, raw_format, md_tokens, error, action = "MISSING", None, None, "Not converted yet", "indexed"
        except Exception as exc:  # one bad filing must not stop the corpus
            status, raw_format, md_tokens, action = "ERROR", None, None, "failed"
            error = f"{type(exc).__name__}: {exc}"
        stage_status = {"SOURCE_INCOMPLETE": "source_incomplete", "MISSING": "pending",
                        "MISSING_SOURCE": "missing_source"}.get(status, "done" if status in CONVERTED else "failed")
        state.set_conversion(db, filing.filing_id, status, raw_format, md_tokens)
        state.set_stage(db, filing.filing_id, STAGE, stage_status, started=started, finished=now(),
                        tokens_in=md_tokens, error=error)
        rows.append({"filing_id": filing.filing_id, "action": action, "conversion_status": status,
                     "raw_format": raw_format or "", "markdown_tokens": md_tokens if md_tokens is not None else "",
                     "error": error or ""})
        if action in {"converted", "reconverted"} or error:
            log(f"[{index}/{len(filings)}] {filing.filing_id}: {action} {status} {error or ''}".rstrip())
        if index % 200 == 0:
            db.commit()
            log(f"[{index}/{len(filings)}] indexed")
    db.commit()
    return write_summary(cfg.outputs_dir / STAGE, rows)


def write_summary(folder: Path, rows: list[dict]) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    fields = ["filing_id", "action", "conversion_status", "raw_format", "markdown_tokens", "error"]
    temp = folder / "conversion_index.csv.tmp"
    with temp.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(folder / "conversion_index.csv")
    converted = [r for r in rows if r["conversion_status"] in CONVERTED]
    summary = {
        "filings": len(rows),
        "converted": len(converted),
        "newly_converted": sum(r["action"] in {"converted", "reconverted"} for r in rows),
        "conversion_status": dict(Counter(r["conversion_status"] for r in rows)),
        "markdown_tokens": sum(int(r["markdown_tokens"] or 0) for r in converted),
        "errors": [{"filing_id": r["filing_id"], "error": r["error"]} for r in rows
                   if r["conversion_status"] in {"ERROR", "MISSING", "MISSING_SOURCE"}],
        "note": "Token counts are text tokens from the conversion audit records, not billing measurements.",
    }
    write_json(folder / "conversion_index.json", summary)
    return summary
