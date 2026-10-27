"""Publish stage: clean file + map per filing, and the work list for the scheduled tasks, per model tag.

- `<file>.clean.md`: a short YAML header, then the compacted kept text.
- `<file>.clean.map.json`: where every part of the clean file came from in the original Markdown.
- `clean_index.csv` / `.jsonl`: one row per registered filing, with status, paths and token counts.
- `clean_summary.json`: weighted token savings at each step.

A filing with nothing kept says so in its header and in the index: missing text is not zero.
"""
from __future__ import annotations

from collections import Counter
import csv
import io
import json
from pathlib import Path
import re
import statistics

from pipeline import control, state
from pipeline.compact import stage as compact_stage
from pipeline.corpus import CONVERTED
from pipeline.records import (atomic_text, is_current, now, read_json, settings_sha256, sha256_file, write_json,
                              write_record)

STAGE = "publish"
VERSION = "publish-1"
NOTHING_KEPT = "no relevant sections kept by the local filter - not evidence of zero"
TOKEN_NOTE = "o200k_base text tokens, not billing measurements"
INDEX_FIELDS = ["filing_id", "company", "cik", "ticker", "form", "fiscal_year", "period", "filing_date", "accession",
                "is_amendment", "amends", "conversion_quality", "status", "clean_path", "map_path", "tokens_raw",
                "tokens_markdown", "tokens_kept", "tokens_clean", "compaction", "settings_sha256", "updated"]


def output_paths(cfg, model_tag: str, filing) -> dict[str, Path]:
    base = cfg.outputs_dir / "clean" / model_tag / filing.filing_id
    return {name: base.with_name(base.name + suffix) for name, suffix in [
        ("clean", ".clean.md"), ("map", ".clean.map.json"), ("record", ".record.json")]}


def company_name(company_dir: str | None) -> str:
    """'AAPL_Apple_Inc_CIK0000320193' -> 'Apple Inc'."""
    if not company_dir:
        return ""
    parts = company_dir.split("_")
    parts = parts[1:] if len(parts) > 2 else parts
    if parts and re.fullmatch(r"CIK\d+", parts[-1]):
        parts = parts[:-1]
    return " ".join(parts)


def header(filing, row, model_tag: str, sections: int, tokens_clean: int, compaction: str,
           segmentation_quality: str | None) -> str:
    """`row` is the filing's registry row (source path, token counts)."""
    values = {
        "filing_id": filing.filing_id, "company": company_name(filing.company_dir), "cik": filing.cik,
        "ticker": filing.ticker, "form": filing.form, "fiscal_year": filing.year, "period": filing.period,
        "filing_date": filing.filing_date, "accession": filing.accession, "is_amendment": bool(filing.is_amendment),
        "amends": filing.amends_filing_id, "source": row["raw_path"], "conversion_quality": filing.conversion_status,
        "segmentation_quality": segmentation_quality, "filter_model": model_tag, "kept_sections": sections,
        "tokens_markdown": row["markdown_tokens"], "tokens_clean": tokens_clean, "compaction": compaction,
    }
    if sections == 0:
        values["note"] = NOTHING_KEPT
    lines = ["---"] + [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in values.items()] + ["---", ""]
    return "\n".join(lines) + "\n"


def settings(cfg) -> dict:
    return {"compact": compact_stage.settings(cfg), "version": VERSION}


def _filing_settings_sha(cfg, model_tag: str) -> str:
    return settings_sha256({"model_tag": model_tag, **settings(cfg)})


def publish_filing(cfg, filing, row, model_tag: str) -> dict:
    compacted = compact_stage.output_paths(cfg, model_tag, filing)
    body = compacted["body"].read_bytes().decode("utf-8")
    entries = read_json(compacted["pieces"])
    summary = read_json(compacted["summary"])
    sections_file = cfg.outputs_dir / "segments" / (filing.filing_id + ".sections.json")
    quality = read_json(sections_file).get("quality") if sections_file.is_file() else None
    top = header(filing, row, model_tag, len(entries), summary["tokens_body"], summary["compaction"], quality)
    offset = len(top)
    mapping = {"filing_id": filing.filing_id, "model_tag": model_tag, "markdown": row["markdown_path"],
               "header_chars": offset, "compaction": summary["compaction"], "compaction_reason": summary.get("reason"),
               "rules": summary.get("rules"),
               "note": "clean_start/clean_end are character offsets in the .clean.md file; markdown_start/"
                       "markdown_end are offsets in the original Markdown file.",
               "parts": [{**{k: v for k, v in e.items() if k not in {"body_start", "body_end"}},
                          "clean_start": e["body_start"] + offset, "clean_end": e["body_end"] + offset} for e in entries]}
    return {"text": top + body, "map": mapping, "summary": summary,
            "status": "ready" if entries else "nothing_kept", "sections": len(entries)}


def run(cfg, db, filings: list, model_tag: str, force: bool = False, log=print) -> dict:
    current = settings(cfg)
    counts: Counter = Counter()
    problems, stopped = [], False
    for index, filing in enumerate(filings, 1):
        if control.stop_requested(cfg):
            stopped = True
            log(f"Stop requested: stopping before filing {index} of {len(filings)}")
            break
        started = now()
        out = output_paths(cfg, model_tag, filing)
        compacted = compact_stage.output_paths(cfg, model_tag, filing)
        try:
            if not compacted["record"].is_file() or read_json(compacted["record"]).get("status") != "done":
                incomplete = filing.conversion_status == "SOURCE_INCOMPLETE"
                status = "source_incomplete" if incomplete else "waiting"
                state.set_stage(db, filing.filing_id, STAGE, status, model_tag=model_tag, started=started,
                                finished=now(), error="No usable Markdown" if incomplete else "Not compacted yet")
                counts[status] += 1
                continue
            row = db.execute("SELECT * FROM filings WHERE filing_id=?", (filing.filing_id,)).fetchone()
            inputs = {"compact_record": sha256_file(compacted["record"]),
                      "metadata": settings_sha256({k: row[k] for k in (
                          "company_dir", "cik", "ticker", "form", "year", "period", "filing_date", "accession",
                          "is_amendment", "amends_filing_id", "conversion_status", "markdown_tokens", "raw_path")})}
            if not force and is_current(out["record"], stage_version=VERSION, inputs=inputs, settings=current):
                done = read_json(out["record"])
                state.set_stage(db, filing.filing_id, STAGE, "done", model_tag=model_tag, started=started, finished=now(),
                                tokens_in=done["tokens_kept_text"], tokens_kept=done["tokens_clean"], detail=done["detail"])
                counts["already_done"] += 1
                continue
            out["record"].unlink(missing_ok=True)
            result = publish_filing(cfg, filing, row, model_tag)
            atomic_text(out["clean"], result["text"])
            write_json(out["map"], result["map"])
            summary = result["summary"]
            detail = {"status": result["status"], "compaction": summary["compaction"], "sections": result["sections"],
                      "settings_sha256": _filing_settings_sha(cfg, model_tag)}
            write_record(out["record"], stage=STAGE, stage_version=VERSION, filing_id=filing.filing_id, inputs=inputs,
                         settings=current, outputs=[out["clean"], out["map"]], started=started,
                         extra={"tokens_kept_text": summary["tokens_kept_text"], "tokens_clean": summary["tokens_body"],
                                "detail": detail})
            state.set_stage(db, filing.filing_id, STAGE, "done", model_tag=model_tag, started=started, finished=now(),
                            tokens_in=summary["tokens_kept_text"], tokens_kept=summary["tokens_body"], detail=detail)
            counts[result["status"]] += 1
        except Exception as exc:  # one bad filing must not stop the corpus
            error = f"{type(exc).__name__}: {exc}"
            state.set_stage(db, filing.filing_id, STAGE, "failed", model_tag=model_tag, started=started, finished=now(),
                            error=error)
            problems.append({"filing_id": filing.filing_id, "error": error})
            counts["failed"] += 1
            log(f"[{index}/{len(filings)}] {filing.filing_id}: FAILED {error}")
        if index % 50 == 0:
            db.commit()
            write_index(cfg, db, model_tag)
            log(f"[{index}/{len(filings)}] publish: {dict(counts)}")
    db.commit()
    index_info = write_index(cfg, db, model_tag)
    result = {"stage_version": VERSION, "model_tag": model_tag, "filings": len(filings), "counts": dict(counts),
              "failures": problems, "stopped": stopped, "index": index_info}
    write_json(cfg.outputs_dir / "clean" / model_tag / "publish_run.json", result)
    log(f"index [{model_tag}]: {index_info['rows']:,} rows, {index_info['by_status']} -> {index_info['path']}")
    return result


def _csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=INDEX_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def index_rows(cfg, db, model_tag: str) -> list[dict]:
    """One row per registered filing, built from the status database (no file reads)."""
    stages: dict[str, dict] = {}
    for row in db.execute("SELECT * FROM stage_status WHERE model_tag IN ('', ?)", (model_tag,)):
        stages.setdefault(row["filing_id"], {})[row["stage"]] = dict(row)
    root = cfg.outputs_dir / "clean" / model_tag
    rows = []
    for f in db.execute("SELECT * FROM filings ORDER BY filing_id"):
        done = stages.get(f["filing_id"], {})
        published = done.get("publish") or {}
        detail = json.loads(published["detail"]) if published.get("detail") else {}
        failed = [s for s, v in done.items() if v["status"] == "failed"]
        if published.get("status") == "done":
            status = detail.get("status", "ready")
        elif f["conversion_status"] == "SOURCE_INCOMPLETE":
            status = "source_incomplete"
        elif failed or (f["conversion_status"] and f["conversion_status"] not in CONVERTED):
            status = "failed"
        else:
            status = "pending"
        base = f["filing_id"]
        ready = published.get("status") == "done"
        classified = done.get("classify") or {}
        rows.append({
            "filing_id": base, "company": company_name(f["company_dir"]), "cik": f["cik"], "ticker": f["ticker"],
            "form": f["form"], "fiscal_year": f["year"], "period": f["period"], "filing_date": f["filing_date"],
            "accession": f["accession"], "is_amendment": bool(f["is_amendment"]), "amends": f["amends_filing_id"],
            "conversion_quality": f["conversion_status"], "status": status,
            "clean_path": (root / (base + ".clean.md")).relative_to(cfg.outputs_dir.parent).as_posix() if ready else "",
            "map_path": (root / (base + ".clean.map.json")).relative_to(cfg.outputs_dir.parent).as_posix() if ready else "",
            "tokens_raw": f["raw_tokens"], "tokens_markdown": f["markdown_tokens"],
            "tokens_kept": classified.get("tokens_kept") if classified.get("status") == "done" else None,
            "tokens_clean": published.get("tokens_kept") if ready else None,
            "compaction": detail.get("compaction", ""), "settings_sha256": detail.get("settings_sha256", ""),
            "updated": published.get("finished") if ready else ""})
    return rows


def write_index(cfg, db, model_tag: str) -> dict:
    rows = index_rows(cfg, db, model_tag)
    root = cfg.outputs_dir / "clean" / model_tag
    atomic_text(root / "clean_index.csv", _csv(rows))
    atomic_text(root / "clean_index.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    summary = savings(rows, db, model_tag)
    write_json(root / "clean_summary.json", summary)
    return {"rows": len(rows), "by_status": dict(Counter(r["status"] for r in rows)),
            "path": str(root / "clean_index.csv")}


def _share(saved: float, whole: float) -> float | None:
    return round(saved / whole, 4) if whole else None


def savings(rows: list[dict], db, model_tag: str) -> dict:
    """Weighted savings over published filings (ready or nothing kept)."""
    published = [r for r in rows if r["status"] in {"ready", "nothing_kept"}]
    kept_text = {r["filing_id"]: r["tokens_in"] for r in db.execute(
        "SELECT filing_id, tokens_in FROM stage_status WHERE stage='publish' AND model_tag=? AND status='done'", (model_tag,))}
    formats = {r["filing_id"]: r["raw_format"] for r in db.execute("SELECT filing_id, raw_format FROM filings")}
    compaction = {r["filing_id"]: json.loads(r["detail"] or "{}").get("compaction") for r in db.execute(
        "SELECT filing_id, detail FROM stage_status WHERE stage='compact' AND model_tag=? AND status='done'", (model_tag,))}

    def block(items: list[dict]) -> dict:
        raw = sum(r["tokens_raw"] or 0 for r in items)
        md = sum(r["tokens_markdown"] or 0 for r in items)
        kept = sum(r["tokens_kept"] or 0 for r in items)
        before = sum(kept_text.get(r["filing_id"]) or 0 for r in items)
        clean = sum(r["tokens_clean"] or 0 for r in items)
        ratios = sorted((r["tokens_clean"] or 0) / r["tokens_markdown"] for r in items if r["tokens_markdown"])
        pick = lambda q: round(ratios[min(len(ratios) - 1, int(q * len(ratios)))], 4) if ratios else None
        return {"filings": len(items), "tokens": {"raw": raw, "markdown": md, "kept": kept,
                                                  "kept_text_with_headings": before, "clean": clean},
                "saved_share": {"markdown_to_kept": _share(md - kept, md), "kept_to_clean": _share(before - clean, before),
                                "markdown_to_clean": _share(md - clean, md), "raw_to_clean": _share(raw - clean, raw)},
                "clean_over_markdown_per_filing": {"p10": pick(0.1), "median": round(statistics.median(ratios), 4)
                                                   if ratios else None, "p90": pick(0.9)},
                "compaction": dict(Counter(compaction.get(r["filing_id"]) for r in items))}

    by_format: dict[str, list] = {}
    for r in published:
        by_format.setdefault(formats.get(r["filing_id"]) or "unknown", []).append(r)
    return {"model_tag": model_tag, "all": block(published),
            "by_raw_format": {k: block(v) for k, v in sorted(by_format.items())},
            "index_status": dict(Counter(r["status"] for r in rows)),
            "note": "Weighted totals over published filings. kept = KEEP/UNCERTAIN chunk tokens from the filter; "
                    "kept_text_with_headings adds the heading lines and is the base for kept -> clean. "
                    + TOKEN_NOTE + "."}
