"""Restore per-stage status in the DB from the record files on disk (used by rebuild-index)."""
from __future__ import annotations

from collections import Counter

from pipeline import state
from pipeline.records import read_json


def reindex_records(cfg, db) -> dict:
    counts: Counter = Counter()
    for record in (cfg.outputs_dir / "segments").rglob("*.record.json"):
        value = read_json(record)
        state.set_stage(db, value["filing_id"], "segment", value.get("status", "failed"), started=value.get("started"),
                        finished=value.get("finished"), tokens_in=value.get("tokens"), error=value.get("error"))
        counts["segment"] += 1
    # Filings without usable Markdown never get a segment record. If segmentation has run at all,
    # restore their status from the registry, as the segment stage would have set it.
    rows = db.execute("SELECT filing_id FROM filings WHERE conversion_status='SOURCE_INCOMPLETE' AND filing_id NOT IN "
                      "(SELECT filing_id FROM stage_status WHERE stage='segment')").fetchall() if counts["segment"] else []
    for row in rows:
        state.set_stage(db, row["filing_id"], "segment", "source_incomplete", error="No usable Markdown")
        counts["segment source_incomplete"] += 1
    classify_root = cfg.outputs_dir / "classify"
    if classify_root.is_dir():
        for model_dir in (p for p in classify_root.iterdir() if p.is_dir()):
            for record in model_dir.rglob("*.record.json"):
                value = read_json(record)
                summary_path = record.with_name(record.name.replace(".record.json", ".summary.json"))
                summary = read_json(summary_path) if summary_path.is_file() else {}
                state.set_stage(db, value["filing_id"], "classify", value.get("status", "failed"),
                                model_tag=model_dir.name, started=value.get("started"), finished=value.get("finished"),
                                tokens_in=summary.get("tokens_total"), tokens_kept=summary.get("tokens_kept"),
                                error=value.get("error"))
                counts[f"classify [{model_dir.name}]"] += 1
    db.commit()
    return dict(counts)
