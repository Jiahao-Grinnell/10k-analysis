"""Segmentation stage: per filing a segments.jsonl (chunks), a sections.json (structure) and a record."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from pipeline import control, state, tokens
from pipeline.corpus import CONVERTED
from pipeline.records import atomic_text, is_current, now, read_json, sha256_file, write_json, write_record
from pipeline.segment.blocks import parse_blocks
from pipeline.segment.chunks import build_chunks
from pipeline.segment.structure import analyze

STAGE = "segment"
VERSION = "segment-4"  # 2: Part/Item/Note/subsection structure; 3: stricter fixed-width subsection headings;
                       # 4: "Item 8 of this Form 10-K ..." (prose cut by a page break) is not an Item heading


def settings(cfg) -> dict:
    return {"chunk_max_tokens": cfg.chunk_max_tokens, "encoding": cfg.encoding}


def paths(cfg, filing) -> tuple[Path, Path]:
    base = cfg.outputs_dir / "segments" / filing.filing_id
    return base.with_name(base.name + ".segments.jsonl"), base.with_name(base.name + ".record.json")


def sections_path(cfg, filing) -> Path:
    base = cfg.outputs_dir / "segments" / filing.filing_id
    return base.with_name(base.name + ".sections.json")


def detail(record: dict) -> dict:
    """Short structure summary for the status DB and the monitor."""
    return {"quality": record.get("segmentation_quality"), "items": len(record.get("items") or []),
            "sections": record.get("sections"), "chunks": record.get("chunks")}


def read_markdown(path: Path) -> str:
    # Decode bytes directly so character offsets match the file exactly (no newline translation).
    return path.read_bytes().decode("utf-8")


def load_chunks(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def segment_filing(cfg, filing) -> tuple[list[dict], int, dict]:
    text = read_markdown(filing.markdown_path)
    encoding = tokens.encoding(cfg.encoding, str(cfg.tiktoken_cache))
    count = lambda value: len(encoding.encode_ordinary(value))
    blocks = parse_blocks(text)
    structure = analyze(text, blocks, filing.form)
    chunks = build_chunks(text, blocks, cfg.chunk_max_tokens, count, structure.sections, structure.quality)
    rows = [{"filing_id": filing.filing_id, **c.to_dict()} for c in chunks]
    sections = {"filing_id": filing.filing_id, "quality": structure.quality, "info": structure.info,
                "sections": [{k: v for k, v in vars(s).items()} for s in structure.sections]}
    return rows, sum(c.tokens for c in chunks), sections


def run(cfg, db, filings: list, force: bool = False, log=print) -> dict:
    counts: Counter = Counter()
    problems = []
    stopped = False
    for index, filing in enumerate(filings, 1):
        if control.stop_requested(cfg):
            stopped = True
            log(f"Stop requested: stopping before filing {index} of {len(filings)}")
            break
        started = now()
        try:
            if filing.conversion_status not in CONVERTED:
                status = "source_incomplete" if filing.conversion_status == "SOURCE_INCOMPLETE" else "skipped"
                state.set_stage(db, filing.filing_id, STAGE, status, started=started, finished=now(),
                                error=f"No usable Markdown (conversion status {filing.conversion_status or 'none'})")
                counts[status] += 1
                continue
            output, record = paths(cfg, filing)
            inputs = {"markdown": sha256_file(filing.markdown_path), "form": filing.form}
            if not force and is_current(record, stage_version=VERSION, inputs=inputs, settings=settings(cfg)):
                counts["already_done"] += 1
                done = read_json(record)
                state.set_stage(db, filing.filing_id, STAGE, "done", started=started, finished=now(),
                                tokens_in=done.get("tokens"), detail=detail(done))
                continue
            record.unlink(missing_ok=True)  # a filing without a record counts as not done
            rows, total, sections = segment_filing(cfg, filing)
            atomic_text(output, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            write_json(sections_path(cfg, filing), sections)
            write_record(record, stage=STAGE, stage_version=VERSION, filing_id=filing.filing_id, inputs=inputs,
                         settings=settings(cfg), outputs=[output, sections_path(cfg, filing)], started=started,
                         extra={"chunks": len(rows), "tokens": total, "sections": len(sections["sections"]),
                                "segmentation_quality": sections["quality"],
                                "items": sections["info"]["documents"][0]["items"]})
            counts[f"quality_{sections['quality']}"] += 1
            state.set_stage(db, filing.filing_id, STAGE, "done", started=started, finished=now(), tokens_in=total,
                            detail=detail(read_json(record)))
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
               "counts": dict(counts), "failures": problems, "stopped": stopped}
    write_json(cfg.outputs_dir / "segments" / "segmentation_run.json", summary)
    return summary
