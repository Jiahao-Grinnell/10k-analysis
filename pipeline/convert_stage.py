"""Conversion stage: index existing Markdown_10K results; convert only filings with no output.

convertmd.py is reused unchanged. An existing Markdown file is never rewritten:
if it is damaged or incomplete, the filing is reported as failed for manual review.
"""
from __future__ import annotations

from collections import Counter
import csv
import json
from pathlib import Path

from convertmd import conversion_job
from pipeline import state, tokens
from pipeline.records import now, read_json, sha256_file, write_json

STAGE = "convert"
CONVERTED = {"CHECKED_TEXT", "REVIEW_REQUIRED"}


def _from_audit(filing, encoding: str) -> tuple[str, str | None, int | None, str | None]:
    """(conversion_status, raw_format, markdown_tokens, error) for a filing with existing outputs."""
    report = read_json(filing.audit_path)
    if report.get("markdown_sha256") != sha256_file(filing.markdown_path):
        return "ERROR", None, None, "Markdown hash differs from its audit record; not rewritten"
    documents = report.get("documents") or []
    raw_format = documents[0].get("format") if documents else None
    markdown_tokens = report.get("tokens", {}).get(encoding, {}).get("markdown")
    return report.get("quality_status", "ERROR"), raw_format, markdown_tokens, None


def run(cfg, db, filings: list, convert_missing: bool = True, tokenizers: dict | None = None,
        log=print) -> dict:
    """Index (and, if allowed, convert) each filing; record status in the database."""
    rows = []
    for index, filing in enumerate(filings, 1):
        started = now()
        md_exists, audit_exists = filing.markdown_path.is_file(), filing.audit_path.is_file()
        try:
            if md_exists and audit_exists:
                status, raw_format, md_tokens, error = _from_audit(filing, cfg.encoding)
                action = "indexed"
            elif md_exists or audit_exists:
                status, raw_format, md_tokens = "ERROR", None, None
                error = "Partial conversion output (Markdown or audit missing); not rewritten"
                action = "indexed"
            elif convert_missing:
                if tokenizers is None:
                    tokenizers = {cfg.encoding: tokens.encoding(cfg.encoding, str(cfg.tiktoken_cache))}
                conversion_job(filing.raw_path, filing.markdown_path, tokenizers, filing.metadata)
                status, raw_format, md_tokens, error = _from_audit(filing, next(iter(tokenizers)))
                action = "converted"
            else:
                status, raw_format, md_tokens, error, action = "MISSING", None, None, "Not converted yet", "indexed"
        except Exception as exc:  # one bad filing must not stop the corpus
            status, raw_format, md_tokens, action = "ERROR", None, None, "failed"
            error = f"{type(exc).__name__}: {exc}"
        stage_status = ("done" if status in CONVERTED else "source_incomplete" if status == "SOURCE_INCOMPLETE"
                        else "pending" if status == "MISSING" else "failed")
        state.upsert_filing(db, filing, status, raw_format, md_tokens)
        state.set_stage(db, filing.filing_id, STAGE, stage_status, started=started, finished=now(),
                        tokens_in=md_tokens, error=error)
        rows.append({"filing_id": filing.filing_id, "action": action, "conversion_status": status,
                     "raw_format": raw_format or "", "markdown_tokens": md_tokens if md_tokens is not None else "",
                     "error": error or ""})
        if action == "converted" or error:
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
        "newly_converted": sum(r["action"] == "converted" for r in rows),
        "conversion_status": dict(Counter(r["conversion_status"] for r in rows)),
        "markdown_tokens": sum(int(r["markdown_tokens"] or 0) for r in converted),
        "errors": [{"filing_id": r["filing_id"], "error": r["error"]} for r in rows
                   if r["conversion_status"] in {"ERROR", "MISSING"}],
        "note": "Token counts are text tokens from the conversion audit records, not billing measurements.",
    }
    write_json(folder / "conversion_index.json", summary)
    return summary
