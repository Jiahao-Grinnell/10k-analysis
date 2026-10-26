"""Filtering reports: what was kept, per filing and for the corpus, per model tag.

Token counts are o200k_base text tokens of the Markdown slices, not billing measurements.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import io
import json
from pathlib import Path

from pipeline.records import atomic_text, read_json, write_json

TOKEN_NOTE = "Token counts are o200k_base text tokens of the Markdown slices, not billing measurements."
REASONS = ("label", "section", "reference")
RESOLVED = ("follow", "kind_off")


def part_key(row: dict) -> str:
    """Where a chunk sits, for reporting: 'Item 7', 'EX-13', 'Front matter', ..."""
    if row.get("document"):
        return row["document"].split()[0]
    if row.get("item"):
        return f"Item {row['item']}"
    return row["heading_path"][0] if row.get("heading_path") else "Unknown"


def financial_items(form: str | None) -> tuple[str, str]:
    """(MD&A Item, financial statements Item); 10-KSB numbers them 6 and 7."""
    return ("6", "7") if (form or "").upper().startswith("10-KSB") else ("7", "8")


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def filing_summary(filing, rows: list[dict], references: list[dict], quality: str) -> dict:
    tokens_total = sum(r["tokens"] for r in rows)
    tokens_kept = sum(r["tokens"] for r in rows if r["kept"])
    parts: dict[str, dict] = {}
    for row in rows:
        p = parts.setdefault(part_key(row), {"part": part_key(row), "chunks": 0, "chunks_kept": 0,
                                             "tokens_total": 0, "tokens_kept": 0, "failures": 0})
        p["chunks"] += 1
        p["tokens_total"] += row["tokens"]
        p["failures"] += row["kept_by"] == "failure"
        if row["kept"]:
            p["chunks_kept"] += 1
            p["tokens_kept"] += row["tokens"]
    by_reason = {reason: sum(r["tokens"] for r in rows if r["kept_reason"] == reason) for reason in REASONS}
    statuses = Counter(f"{r['kind']}:{r['status']}" for r in references)
    alerts = []
    for item in financial_items(filing.form):
        p = parts.get(f"Item {item}")
        exhibit_13 = sum(v["tokens_kept"] for k, v in parts.items() if k.upper().startswith("EX-13"))
        if p is None:
            alerts.append({"item": item, "problem": "not found", "ex13_tokens_kept": exhibit_13})
        elif p["tokens_kept"] == 0:
            alerts.append({"item": item, "problem": "nothing kept", "tokens_total": p["tokens_total"],
                           "ex13_tokens_kept": exhibit_13})
    return {
        "filing_id": filing.filing_id, "form": filing.form, "year": filing.year, "segmentation_quality": quality,
        "chunks": len(rows), "chunks_kept": sum(r["kept"] for r in rows),
        "final_labels": dict(Counter(r["final_label"] for r in rows)),
        "model_labels": dict(Counter(r["model_label"] or "none" for r in rows)),
        "failures": sum(r["kept_by"] == "failure" for r in rows),
        "tokens_total": tokens_total, "tokens_kept": tokens_kept, "kept_share": _ratio(tokens_kept, tokens_total),
        "tokens_kept_by_reason": by_reason, "by_part": list(parts.values()),
        "references": {"found": len(references), "by_status": dict(sorted(statuses.items())),
                       "followed": sum(r["status"] == "follow" for r in references),
                       "unresolved": sum(r["status"] == "unresolved" for r in references),
                       "chunks_kept_by_reference": sum(len(r["newly_kept_chunks"]) for r in references)},
        "alerts": alerts, "note": TOKEN_NOTE,
    }


def _finished_summaries(folder: Path):
    for summary in sorted(folder.rglob("*.summary.json")):
        record = summary.with_name(summary.name.replace(".summary.json", ".record.json"))
        if not record.is_file():
            continue
        value = read_json(summary)
        if "by_part" in value and read_json(record).get("status") == "done":
            yield summary, value


def _csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(path, buffer.getvalue())


def write_filter_report(cfg, model_tag: str, log=print) -> dict:
    """Corpus report over every finished filing of this model tag (not only the last run)."""
    folder = cfg.outputs_dir / "classify" / model_tag
    if not folder.is_dir():
        return {}
    filings, alerts, unresolved = [], [], []
    parts: dict[str, Counter] = defaultdict(Counter)
    quality: dict[str, Counter] = defaultdict(Counter)
    statuses: dict[str, Counter] = defaultdict(Counter)
    reasons: Counter = Counter()
    for path, s in _finished_summaries(folder):
        refs = s["references"]
        filings.append({"filing_id": s["filing_id"], "form": s["form"], "year": s["year"],
                        "segmentation_quality": s["segmentation_quality"], "chunks": s["chunks"],
                        "chunks_kept": s["chunks_kept"], "failures": s["failures"],
                        "tokens_total": s["tokens_total"], "tokens_kept": s["tokens_kept"],
                        "kept_share": s["kept_share"], "references_followed": refs["followed"],
                        "references_unresolved": refs["unresolved"],
                        "chunks_kept_by_reference": refs["chunks_kept_by_reference"],
                        "alerts": "; ".join(f"Item {a['item']} {a['problem']}" for a in s["alerts"])})
        for p in s["by_part"]:
            key = p["part"] if p["part"].startswith(("Item ", "EX-13")) or p["part"] == "Front matter" else (
                "Other exhibits" if p["part"].upper().startswith("EX-") else "Other")
            if key.startswith("EX-13"):
                key = "EX-13"
            parts[key].update({"tokens_total": p["tokens_total"], "tokens_kept": p["tokens_kept"],
                               "chunks": p["chunks"], "failures": p["failures"]})
        quality[s["segmentation_quality"]].update({"filings": 1, "tokens_total": s["tokens_total"],
                                                   "tokens_kept": s["tokens_kept"]})
        reasons.update(s["tokens_kept_by_reason"])
        for key, n in refs["by_status"].items():
            kind, status = key.split(":")
            statuses[kind][status] += n
        for alert in s["alerts"]:
            alerts.append({"filing_id": s["filing_id"], "form": s["form"], "year": s["year"],
                           "segmentation_quality": s["segmentation_quality"], **alert})
        if refs["unresolved"]:
            references = path.with_name(path.name.replace(".summary.json", ".references.jsonl"))
            for line in references.read_text(encoding="utf-8").splitlines():
                ref = json.loads(line)
                if ref["status"] == "unresolved":
                    unresolved.append({"filing_id": s["filing_id"], "reference": ref["label"], "text": ref["text"],
                                       "source_chunk": ref["source_chunk"], "position": ref["position"]})
    total = sum(f["tokens_total"] for f in filings)
    kept = sum(f["tokens_kept"] for f in filings)

    def order(key: str):
        if key.startswith("Item "):
            number = key[5:]
            return (1, int("".join(c for c in number if c.isdigit()) or 0), number)
        return ({"Front matter": 0, "EX-13": 2, "Other exhibits": 3}.get(key, 4), 0, key)

    report = {
        "model_tag": model_tag, "filings": len(filings), "tokens_total": total, "tokens_kept": kept,
        "kept_share_weighted": _ratio(kept, total),
        "tokens_kept_by_reason": dict(reasons),
        "by_part": [{"part": k, **parts[k], "kept_share": _ratio(parts[k]["tokens_kept"], parts[k]["tokens_total"])}
                    for k in sorted(parts, key=order)],
        "by_segmentation_quality": {k: dict(v) for k, v in sorted(quality.items())},
        "references": {kind: {"by_status": dict(v),
                              "resolved_share": _ratio(sum(v[s] for s in RESOLVED), sum(v[s] for s in RESOLVED) + v["unresolved"])}
                       for kind, v in sorted(statuses.items())},
        "alerts": {"filings": len({a["filing_id"] for a in alerts}), "by_problem": dict(Counter(
            f"Item {a['item']} {a['problem']}" for a in alerts))},
        "files": {"per_filing": "filter_report.csv", "alerts": "filter_alerts.csv",
                  "unresolved_references": "unresolved_references.csv"},
        "note": TOKEN_NOTE,
    }
    write_json(folder / "filter_report.json", report)
    _csv(folder / "filter_report.csv", list(filings[0]) if filings else ["filing_id"], filings)
    _csv(folder / "filter_alerts.csv", ["filing_id", "form", "year", "segmentation_quality", "item", "problem",
                                        "tokens_total", "ex13_tokens_kept"], alerts)
    _csv(folder / "unresolved_references.csv", ["filing_id", "reference", "text", "source_chunk", "position"], unresolved)
    log(f"filter report [{model_tag}]: {len(filings)} filings, kept {kept:,} of {total:,} tokens; "
        f"{report['alerts']['filings']} filings with an MD&A / financial statements alert -> {folder / 'filter_report.json'}")
    return report
