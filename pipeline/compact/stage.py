"""Compaction stage: kept text of one filing -> compacted body, validated, per model tag.

The kept text is assembled from the classify labels: the kept chunks, in document order,
cut at section boundaries. A heading line with the path to the enclosing Part / Item /
note / attached document is written when that unit changes or after dropped text;
subsection titles are already in the text itself. Fixed-width
text cut in the middle gets its fence re-opened / closed, so every piece stays valid
Markdown. The same assembly without rules is the uncompacted kept text; the compacted
text must pass validate.check against it, and must save tokens, or the uncompacted text
is used and the reason is recorded.
"""
from __future__ import annotations

import bisect
from collections import Counter
import json
from pathlib import Path
import random
import time

from pipeline import control, state, tokens
from pipeline.classify import stage as classify_stage
from pipeline.compact import rules as R
from pipeline.compact.validate import check
from pipeline.records import atomic_text, is_current, now, read_json, sha256_file, write_json, write_record
from pipeline.segment import stage as segment_stage
from pipeline.segment.blocks import parse_blocks

STAGE = "compact"
VERSION = "compact-1"


def output_paths(cfg, model_tag: str, filing) -> dict[str, Path]:
    base = cfg.outputs_dir / STAGE / model_tag / filing.filing_id
    return {name: base.with_name(base.name + suffix) for name, suffix in [
        ("body", ".body.md"), ("pieces", ".pieces.json"), ("summary", ".summary.json"), ("record", ".record.json")]}


def enabled_rules(cfg) -> list[str]:
    chosen = list(cfg.compact["rules"])
    unknown = sorted(set(chosen) - set(R.RULES))
    if unknown:
        raise SystemExit(f"Unknown compaction rules in config.toml: {unknown}. Known: {R.RULES}")
    return [r for r in R.RULES if r in chosen]


def settings(cfg) -> dict:
    return {"rules": enabled_rules(cfg), "rules_version": R.RULES_VERSION, "encoding": cfg.encoding}


def _pieces(markdown: str, sections: list[dict], rows: list[dict]) -> list[dict]:
    """Kept text cut at section boundaries: [{start, end, section, rows}] in document order."""
    kept = [r for r in rows if r["kept"]]
    spans: list[list] = []
    for row in kept:
        if spans and spans[-1][1] == row["start"]:
            spans[-1][1] = row["end"]
            spans[-1][2].append(row)
        else:
            spans.append([row["start"], row["end"], [row]])
    starts = [s["start"] for s in sections]
    pieces = []
    for start, end, span_rows in spans:
        i = max(bisect.bisect_right(starts, start) - 1, 0)
        while i < len(sections) and sections[i]["start"] < end:
            s = sections[i]
            a, b = max(start, s["start"]), min(end, s["end"])
            if a < b:
                pieces.append({"start": a, "end": b, "section": s,
                               "rows": [r for r in span_rows if r["start"] < b and a < r["end"]]})
            i += 1
    return pieces


def assemble(markdown: str, blocks, sections: list[dict], rows: list[dict], rules: set[str],
             counts: Counter | None = None) -> tuple[str, list[dict]]:
    """Body text and its map entries. With an empty rule set this is the uncompacted kept text."""
    counts = counts if counts is not None else Counter()
    block_starts = [b.start for b in blocks]
    out: list[str] = []
    length = 0
    entries = []
    previous_end, previous_group = None, None

    def emit(value: str) -> None:
        nonlocal length
        out.append(value)
        length += len(value)

    for piece in _pieces(markdown, sections, rows):
        section = piece["section"]
        if section["group"] != previous_group or piece["start"] != previous_end:
            path = section["heading_path"][:-1] if section.get("subsection") else section["heading_path"]
            emit(("\n" if out else "") + "## " + " > ".join(path or section["heading_path"]) + "\n\n")
        out_start = length
        position = piece["start"]
        index = max(bisect.bisect_right(block_starts, position) - 1, 0)
        while position < piece["end"]:
            block = blocks[index]
            stop = min(block.end, piece["end"])
            text = markdown[position:stop]
            whole = position == block.start and stop == block.end
            if block.kind == "fenced":
                if position > block.start:  # re-open a fence cut at the start
                    emit(block.fence + "text\n")
                emit(text)
                if stop < block.end:        # close a fence cut at the end
                    emit(("" if text.endswith("\n") else "\n") + block.fence + "\n")
            elif whole:
                body = text.rstrip("\n")
                emit(R.compact_block(body, block, rules, counts) + text[len(body):])
            elif block.kind == "html":  # part of a large HTML block: only tables wholly inside the part
                body = text.rstrip("\n")
                emit(R.compact_html(body, rules, counts) + text[len(body):])
            else:
                emit(text)
            position = stop
            index += 1
        if not out[-1].endswith("\n"):
            emit("\n")
        entries.append({"section_id": section["section_id"], "heading_path": section["heading_path"],
                        "markdown_start": piece["start"], "markdown_end": piece["end"],
                        "body_start": out_start, "body_end": length,
                        "chunks": [{"chunk_id": r["chunk_id"], "final_label": r["final_label"], "decided_by": r["kept_by"],
                                    "kept_reason": r["kept_reason"], "kept_detail": r.get("kept_detail"),
                                    "reason": r.get("reason", ""), "categories": r.get("categories", [])}
                                   for r in piece["rows"]]})
        previous_end, previous_group = piece["end"], section["group"]
    return "".join(out), entries


def compact_filing(cfg, filing, rows: list[dict], sections: list[dict], markdown: str, rules: list[str], count) -> dict:
    blocks = parse_blocks(markdown)
    plain, plain_entries = assemble(markdown, blocks, sections, rows, set())
    counts: Counter = Counter()
    clock = time.perf_counter()
    compacted, entries = assemble(markdown, blocks, sections, rows, set(rules), counts)
    tokens_plain = count(plain)
    result = {"tokens_kept_text": tokens_plain, "rule_counts": dict(sorted(counts.items())), "problems": []}
    if not plain:
        result.update(body=plain, entries=plain_entries, compaction="nothing_kept", tokens_body=0)
        return result
    problems = check(plain, compacted) if compacted != plain else []
    tokens_compacted = count(compacted) if not problems else None
    if problems:
        result.update(body=plain, entries=plain_entries, compaction="fallback", problems=problems,
                      reason="validation failed: " + problems[0], tokens_body=tokens_plain)
    elif tokens_compacted >= tokens_plain:
        result.update(body=plain, entries=plain_entries, compaction="no_gain", reason="compaction saved no tokens",
                      tokens_body=tokens_plain)
    else:
        result.update(body=compacted, entries=entries, compaction="applied", tokens_body=tokens_compacted)
    result["seconds"] = round(time.perf_counter() - clock, 2)
    return result


def classified(record: Path) -> bool:
    """A finished filter result made by the current classify stage (older results lack the keep decisions)."""
    if not record.is_file():
        return False
    value = read_json(record)
    return value.get("status") == "done" and value.get("stage_version") == classify_stage.VERSION


def _paths(cfg, model_tag, filing) -> dict[str, Path]:
    classified = classify_stage.output_paths(cfg, model_tag, filing)
    return {"labels": classified["labels"], "classify_record": classified["record"],
            "sections": segment_stage.sections_path(cfg, filing), "markdown": filing.markdown_path}


def run(cfg, db, filings: list, model_tag: str, force: bool = False, log=print) -> dict:
    rules = enabled_rules(cfg)
    current = settings(cfg)
    encoding = tokens.encoding(cfg.encoding, str(cfg.tiktoken_cache))
    count = lambda value: len(encoding.encode_ordinary(value))
    counts: Counter = Counter()
    problems, stopped = [], False
    for index, filing in enumerate(filings, 1):
        if control.stop_requested(cfg):
            stopped = True
            log(f"Stop requested: stopping before filing {index} of {len(filings)}")
            break
        started = now()
        out = output_paths(cfg, model_tag, filing)
        try:
            paths = _paths(cfg, model_tag, filing)
            record = paths["classify_record"]
            if not classified(record):
                incomplete = filing.conversion_status == "SOURCE_INCOMPLETE"
                status = "source_incomplete" if incomplete else "waiting"
                state.set_stage(db, filing.filing_id, STAGE, status, model_tag=model_tag, started=started, finished=now(),
                                error="No usable Markdown" if incomplete else "Not classified yet with the current filter version")
                counts[status] += 1
                continue
            inputs = {k: sha256_file(v) for k, v in paths.items() if k != "classify_record"}
            if not force and is_current(out["record"], stage_version=VERSION, inputs=inputs, settings=current):
                summary = read_json(out["summary"])
                state.set_stage(db, filing.filing_id, STAGE, "done", model_tag=model_tag, started=started, finished=now(),
                                tokens_in=summary["tokens_kept_text"], tokens_kept=summary["tokens_body"],
                                detail=_detail(summary))
                counts["already_done"] += 1
                continue
            out["record"].unlink(missing_ok=True)
            markdown = segment_stage.read_markdown(filing.markdown_path)
            rows = [json.loads(line) for line in paths["labels"].read_text(encoding="utf-8").splitlines() if line]
            sections = read_json(paths["sections"])["sections"]
            result = compact_filing(cfg, filing, rows, sections, markdown, rules, count)
            summary = {k: v for k, v in result.items() if k not in {"body", "entries"}}
            summary.update({"filing_id": filing.filing_id, "model_tag": model_tag, "rules": rules,
                            "note": "Token counts are o200k_base text tokens, not billing measurements."})
            atomic_text(out["body"], result["body"])
            write_json(out["pieces"], result["entries"])
            write_json(out["summary"], summary)
            write_record(out["record"], stage=STAGE, stage_version=VERSION, filing_id=filing.filing_id, inputs=inputs,
                         settings=current, outputs=[out["body"], out["pieces"], out["summary"]], started=started)
            note = summary.get("reason") if summary["compaction"] == "fallback" else None
            state.set_stage(db, filing.filing_id, STAGE, "done", model_tag=model_tag, started=started, finished=now(),
                            tokens_in=summary["tokens_kept_text"], tokens_kept=summary["tokens_body"], error=note,
                            detail=_detail(summary))
            counts[summary["compaction"]] += 1
            if index % 50 == 0 or len(filings) <= 50:
                log(f"[{index}/{len(filings)}] compact {filing.filing_id}: {summary['compaction']}")
        except Exception as exc:  # one bad filing must not stop the corpus
            error = f"{type(exc).__name__}: {exc}"
            state.set_stage(db, filing.filing_id, STAGE, "failed", model_tag=model_tag, started=started, finished=now(),
                            error=error)
            problems.append({"filing_id": filing.filing_id, "error": error})
            counts["failed"] += 1
            log(f"[{index}/{len(filings)}] {filing.filing_id}: FAILED {error}")
        if index % 20 == 0:
            db.commit()
    db.commit()
    result = {"stage_version": VERSION, "model_tag": model_tag, "settings": current, "filings": len(filings),
              "counts": dict(counts), "failures": problems, "stopped": stopped}
    write_json(cfg.outputs_dir / STAGE / model_tag / "compaction_run.json", result)
    return result


def _detail(summary: dict) -> dict:
    return {"compaction": summary["compaction"], "reason": summary.get("reason"),
            "tokens_kept_text": summary["tokens_kept_text"], "tokens_body": summary["tokens_body"]}


def profile(cfg, db, filings: list, model_tag: str, sample: int = 150, log=print) -> dict:
    """Tokens each rule saves on its own (and all together), per raw format, on a sample of classified filings."""
    encoding = tokens.encoding(cfg.encoding, str(cfg.tiktoken_cache))
    count = lambda value: len(encoding.encode_ordinary(value))
    ready = [f for f in filings if classified(classify_stage.output_paths(cfg, model_tag, f)["record"])]
    if len(ready) > sample:
        ready = sorted(random.Random(20261026).sample(ready, sample), key=lambda f: f.filing_id)
    formats = {r["filing_id"]: r["raw_format"] for r in db.execute("SELECT filing_id, raw_format FROM filings")}
    variants = {rule: {rule} for rule in R.RULES}
    variants["all rules"] = set(R.RULES)
    totals: dict[str, Counter] = {}
    failures: Counter = Counter()
    for index, filing in enumerate(ready, 1):
        out = classify_stage.output_paths(cfg, model_tag, filing)
        rows = [json.loads(line) for line in out["labels"].read_text(encoding="utf-8").splitlines() if line]
        sections = read_json(segment_stage.sections_path(cfg, filing))["sections"]
        markdown = segment_stage.read_markdown(filing.markdown_path)
        blocks = parse_blocks(markdown)
        plain, _ = assemble(markdown, blocks, sections, rows, set())
        base = count(plain)
        fmt = formats.get(filing.filing_id) or "unknown"
        for key in (fmt, "all formats"):
            totals.setdefault(key, Counter())["uncompacted"] += base
            totals[key]["filings"] += 1
        for name, chosen in variants.items():
            text, _ = assemble(markdown, blocks, sections, rows, chosen)
            failed = bool(check(plain, text)) if text != plain else False
            failures[name] += failed
            saved = 0 if failed else base - count(text)
            for key in (fmt, "all formats"):
                totals[key][name] += saved
        if index % 10 == 0:
            log(f"profile: {index}/{len(ready)} filings")
    report = {"model_tag": model_tag, "filings": len(ready), "rules_version": R.RULES_VERSION,
              "by_format": {fmt: {"filings": c["filings"], "uncompacted_tokens": c["uncompacted"],
                                  "saved": {name: {"tokens": c[name], "share": round(c[name] / c["uncompacted"], 4)
                                                   if c["uncompacted"] else None} for name in variants}}
                            for fmt, c in sorted(totals.items())},
              "validation_failures": dict(failures),
              "note": "Savings of each rule applied alone (table rules include re-writing the table without "
                      "inter-tag spaces); a variant that fails validation counts as saving nothing. "
                      "o200k_base text tokens, not billing measurements."}
    write_json(cfg.outputs_dir / STAGE / model_tag / "compact_profile.json", report)
    return report
