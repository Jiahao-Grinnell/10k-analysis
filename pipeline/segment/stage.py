"""Segmentation stage: one segments.jsonl and one record per filing."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from pipeline import state, tokens
from pipeline.corpus import CONVERTED
from pipeline.records import atomic_text, is_current, now, read_json, sha256_file, write_json, write_record
from pipeline.segment.blocks import parse_blocks
from pipeline.segment.chunks import build_chunks

STAGE = "segment"
VERSION = "segment-1"


def settings(cfg) -> dict:
    return {"chunk_max_tokens": cfg.chunk_max_tokens, "encoding": cfg.encoding}


def paths(cfg, filing) -> tuple[Path, Path]:
    base = cfg.outputs_dir / "segments" / filing.filing_id
    return base.with_name(base.name + ".segments.jsonl"), base.with_name(base.name + ".record.json")


def read_markdown(path: Path) -> str:
    # Decode bytes directly so character offsets match the file exactly (no newline translation).
    return path.read_bytes().decode("utf-8")


def load_chunks(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def segment_filing(cfg, filing) -> tuple[list[dict], int]:
    text = read_markdown(filing.markdown_path)
    encoding = tokens.encoding(cfg.encoding, str(cfg.tiktoken_cache))
    count = lambda value: len(encoding.encode_ordinary(value))
    chunks = build_chunks(text, parse_blocks(text), cfg.chunk_max_tokens, count)
    rows = [{"filing_id": filing.filing_id, **c.to_dict()} for c in chunks]
    return rows, sum(c.tokens for c in chunks)


def run(cfg, db, filings: list, force: bool = False, log=print) -> dict:
    counts: Counter = Counter()
    problems = []
    for index, filing in enumerate(filings, 1):
        started = now()
        try:
            if filing.conversion_status not in CONVERTED:
                status = "source_incomplete" if filing.conversion_status == "SOURCE_INCOMPLETE" else "skipped"
                state.set_stage(db, filing.filing_id, STAGE, status, started=started, finished=now(),
                                error=f"No usable Markdown (conversion status {filing.conversion_status or 'none'})")
                counts[status] += 1
                continue
            output, record = paths(cfg, filing)
            inputs = {"markdown": sha256_file(filing.markdown_path)}
            if not force and is_current(record, stage_version=VERSION, inputs=inputs, settings=settings(cfg)):
                counts["already_done"] += 1
                state.set_stage(db, filing.filing_id, STAGE, "done", started=started, finished=now(),
                                tokens_in=read_json(record).get("tokens"))
                continue
            record.unlink(missing_ok=True)  # a filing without a record counts as not done
            rows, total = segment_filing(cfg, filing)
            atomic_text(output, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            write_record(record, stage=STAGE, stage_version=VERSION, filing_id=filing.filing_id, inputs=inputs,
                         settings=settings(cfg), outputs=[output], started=started,
                         extra={"chunks": len(rows), "tokens": total, "segmentation_quality": "LOW"})
            state.set_stage(db, filing.filing_id, STAGE, "done", started=started, finished=now(), tokens_in=total)
            counts["segmented"] += 1
        except Exception as exc:  # one bad filing must not stop the corpus
            error = f"{type(exc).__name__}: {exc}"
            state.set_stage(db, filing.filing_id, STAGE, "failed", started=started, finished=now(), error=error)
            problems.append({"filing_id": filing.filing_id, "error": error})
            counts["failed"] += 1
            log(f"[{index}/{len(filings)}] {filing.filing_id}: FAILED {error}")
        if index % 100 == 0:
            db.commit()
            log(f"[{index}/{len(filings)}] segment: {dict(counts)}")
    db.commit()
    summary = {"stage_version": VERSION, "settings": settings(cfg), "filings": len(filings),
               "counts": dict(counts), "failures": problems}
    write_json(cfg.outputs_dir / "segments" / "segmentation_run.json", summary)
    return summary
