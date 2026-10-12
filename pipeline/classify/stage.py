"""Classification stage: one filing at a time; that filing's outputs are written before the next starts.

Within a filing, up to `parallel_requests` chunk requests run concurrently; every
request holds text from that filing only. The record file is written last.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import time

from pipeline import state, tokens
from pipeline.classify import prompt as prompts
from pipeline.classify.client import ClientError
from pipeline.classify.rules import KEPT, RULES_VERSION, InvalidResponse, decide, parse
from pipeline.records import atomic_text, is_current, now, read_json, sha256_file, write_json, write_record
from pipeline.segment import stage as segment_stage
from pipeline.segment.chunks import model_text

STAGE = "classify"
VERSION = "classify-1"
ATTEMPTS = 2  # one retry, then UNCERTAIN


def output_paths(cfg, model_tag: str, filing) -> dict[str, Path]:
    base = cfg.outputs_dir / STAGE / model_tag / filing.filing_id
    return {name: base.with_name(base.name + suffix) for name, suffix in [
        ("labels", ".labels.jsonl"), ("requests", ".requests.jsonl"), ("kept", ".kept.md"),
        ("summary", ".summary.json"), ("record", ".record.json")]}


def settings(cfg, client, prompt) -> dict:
    """Everything that can change a label. Concurrency and timeouts are deliberately excluded."""
    return {"client": client.describe(), "prompt_version": prompt.version, "prompt_sha256": prompt.sha256,
            "context_tokens": int(cfg.classify["context_tokens"]), "rules_version": RULES_VERSION}


def _context(previous: str, limit: int, encoding) -> str:
    if not previous or limit <= 0:
        return ""
    ids = encoding.encode_ordinary(previous)
    return encoding.decode(ids[-limit:]) if len(ids) > limit else previous


def _classify_chunk(client, prompt, filing_id: str, chunk_id: str, messages: list[dict]) -> dict:
    requests, parsed, raw, error = [], None, None, None
    for attempt in range(1, ATTEMPTS + 1):
        started, clock = now(), time.perf_counter()
        try:
            raw = client.complete(messages, prompt.schema)
            parsed, error = parse(raw), None
        except (ClientError, InvalidResponse) as exc:
            error = f"{exc.code}: {exc}" if isinstance(exc, ClientError) else f"invalid_response: {exc}"
        requests.append({"request_id": f"{filing_id}#{chunk_id}#{attempt}", "filing_id": filing_id,
                         "chunk_ids": [chunk_id], "attempt": attempt, "prompt_sha256": prompt.sha256,
                         "started": started, "elapsed_ms": round(1000 * (time.perf_counter() - clock)),
                         "input_chars": sum(len(m["content"]) for m in messages), "ok": error is None,
                         "error": error})
        if parsed is not None:
            break
    return {"parsed": parsed, "raw": raw, "error": error, "requests": requests}


def _kept_markdown(filing_id: str, model_tag: str, prompt_sha: str, markdown: str, rows: list[dict]) -> str:
    parts = [f"<!-- kept text | filing {filing_id} | model {model_tag} | prompt {prompt_sha[:12]} | "
             "original Markdown slices, verbatim; each marker gives the slice length -->\n\n"]
    for row in rows:
        if row["final_label"] in KEPT:
            text = markdown[row["start"]:row["end"]]
            parts.append(f"<!-- chunk {row['chunk_id']} | {' > '.join(row['heading_path'])} | "
                         f"chars {row['start']}-{row['end']} | length {len(text)} | {row['final_label']} -->\n")
            parts.append(text + "\n\n")
    return "".join(parts)


def classify_filing(cfg, db, filing, client, prompt, chunks: list[dict], markdown: str, run_id: int | None,
                    log=print) -> tuple[list[dict], list[dict]]:
    encoding = tokens.encoding(cfg.encoding, str(cfg.tiktoken_cache))
    limit = int(cfg.classify["context_tokens"])
    jobs = []
    for index, chunk in enumerate(chunks):
        previous = markdown[chunks[index - 1]["start"]:chunks[index - 1]["end"]] if index else ""
        text = model_text(markdown, chunk)
        jobs.append(prompts.messages(prompt, filing, chunk, text, _context(previous, limit, encoding)))
    results: list[dict | None] = [None] * len(chunks)
    workers = max(1, int(cfg.classify["parallel_requests"]))
    state.set_progress(db, run_id, filing.filing_id, 0, len(chunks))
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(_classify_chunk, client, prompt, filing.filing_id, chunks[i]["chunk_id"], jobs[i]): i
                   for i in range(len(chunks))}
        for done, future in enumerate(as_completed(futures), 1):
            results[futures[future]] = future.result()
            if done % 5 == 0 or done == len(chunks):
                state.set_progress(db, run_id, filing.filing_id, done, len(chunks))
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    rows, request_rows = [], []
    for chunk, result in zip(chunks, results):
        parsed = result["parsed"]
        final_label, kept_by = decide(parsed)
        rows.append({
            "filing_id": filing.filing_id, "chunk_id": chunk["chunk_id"], "section_id": chunk["section_id"],
            "heading_path": chunk["heading_path"], "item": chunk["item"], "start": chunk["start"], "end": chunk["end"],
            "tokens": chunk["tokens"], "kind": chunk["kind"],
            "model_label": parsed["label"] if parsed else None, "final_label": final_label, "kept_by": kept_by,
            "categories": parsed["categories"] if parsed else [], "reason": parsed["reason"] if parsed else "",
            "error": result["error"], "raw_response": result["raw"],
            "model_tag": client.tag, "prompt_sha256": prompt.sha256})
        request_rows.extend(result["requests"])
    return rows, request_rows


def run(cfg, db, filings: list, client, force: bool = False, run_id: int | None = None, log=print) -> dict:
    prompt = prompts.load(cfg.classify["prompts_dir"], cfg.classify["prompt_version"])
    current_settings = settings(cfg, client, prompt)
    counts: Counter = Counter()
    problems = []
    for index, filing in enumerate(filings, 1):
        started, clock = now(), time.perf_counter()
        segments, segment_record = segment_stage.paths(cfg, filing)
        if not segment_record.is_file() or read_json(segment_record).get("status") != "done":
            incomplete = filing.conversion_status == "SOURCE_INCOMPLETE"
            status = "source_incomplete" if incomplete else "waiting"
            state.set_stage(db, filing.filing_id, STAGE, status, model_tag=client.tag, started=started,
                            finished=now(), error="No usable Markdown" if incomplete else "Not segmented yet")
            counts[status] += 1
            continue
        out = output_paths(cfg, client.tag, filing)
        try:
            inputs = {"segments": sha256_file(segments), "markdown": sha256_file(filing.markdown_path)}
            if not force and is_current(out["record"], stage_version=VERSION, inputs=inputs, settings=current_settings):
                summary = read_json(out["summary"])
                state.set_stage(db, filing.filing_id, STAGE, "done", model_tag=client.tag, started=started,
                                finished=now(), tokens_in=summary["tokens_total"], tokens_kept=summary["tokens_kept"])
                counts["already_done"] += 1
                continue
            out["record"].unlink(missing_ok=True)  # until the new record exists, the filing is not done
            state.set_stage(db, filing.filing_id, STAGE, "running", model_tag=client.tag, started=started)
            db.commit()
            log(f"[{index}/{len(filings)}] classify {filing.filing_id}")
            markdown = segment_stage.read_markdown(filing.markdown_path)
            chunks = segment_stage.load_chunks(segments)
            rows, request_rows = classify_filing(cfg, db, filing, client, prompt, chunks, markdown, run_id, log)
            labels = Counter(r["final_label"] for r in rows)
            tokens_total = sum(r["tokens"] for r in rows)
            tokens_kept = sum(r["tokens"] for r in rows if r["final_label"] in KEPT)
            summary = {
                "filing_id": filing.filing_id, "model_tag": client.tag, "prompt_sha256": prompt.sha256,
                "chunks": len(rows), "final_labels": dict(labels),
                "model_labels": dict(Counter(r["model_label"] or "none" for r in rows)),
                "failures": sum(r["kept_by"] == "failure" for r in rows), "requests": len(request_rows),
                "tokens_total": tokens_total, "tokens_kept": tokens_kept,
                "kept_share": round(tokens_kept / tokens_total, 4) if tokens_total else None,
                "elapsed_seconds": round(time.perf_counter() - clock, 2),
                "note": "Token counts are o200k_base text tokens of the Markdown slices, not billing measurements."}
            atomic_text(out["labels"], "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            atomic_text(out["requests"], "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in request_rows))
            atomic_text(out["kept"], _kept_markdown(filing.filing_id, client.tag, prompt.sha256, markdown, rows))
            write_json(out["summary"], summary)
            write_record(out["record"], stage=STAGE, stage_version=VERSION, filing_id=filing.filing_id,
                         inputs=inputs, settings=current_settings,
                         outputs=[out["labels"], out["requests"], out["kept"], out["summary"]], started=started)
            state.set_stage(db, filing.filing_id, STAGE, "done", model_tag=client.tag, started=started,
                            finished=now(), tokens_in=tokens_total, tokens_kept=tokens_kept)
            counts["classified"] += 1
        except Exception as exc:  # one bad filing must not stop the corpus
            error = f"{type(exc).__name__}: {exc}"
            state.set_stage(db, filing.filing_id, STAGE, "failed", model_tag=client.tag, started=started,
                            finished=now(), error=error)
            problems.append({"filing_id": filing.filing_id, "error": error})
            counts["failed"] += 1
            log(f"[{index}/{len(filings)}] {filing.filing_id}: FAILED {error}")
        db.commit()
    result = {"stage_version": VERSION, "model_tag": client.tag, "settings": current_settings,
              "filings": len(filings), "counts": dict(counts), "failures": problems}
    write_json(cfg.outputs_dir / STAGE / client.tag / "classification_run.json", result)
    return result
