"""Read-only queries over the status DB and the per-filing output files.

File paths are always built from configured folders and IDs stored in the DB,
never from text in the request URL.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from pipeline import corpus, disk

STALE_SECONDS = 600  # an unfinished run with no activity for this long is shown as interrupted
CHUNK_ID = re.compile(r"c\d{5}")
PAGE_SIZE = 200


def _parse(stamp: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def process_alive(pid: int) -> bool:
    from monitor.runner import _process_info
    return _process_info(pid)[0]


def stage_counts(db) -> dict:
    stages: dict = {}
    for row in db.execute("SELECT stage, model_tag, status, COUNT(*) n FROM stage_status GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"):
        key = row["stage"] + (f" [{row['model_tag']}]" if row["model_tag"] else "")
        stages.setdefault(key, {})[row["status"]] = row["n"]
    return stages


def token_totals(db) -> list[dict]:
    """Weighted totals per step. Kept = KEEP + UNCERTAIN chunks; clean files come with TASK-08."""
    base = db.execute("SELECT COUNT(*) n, COALESCE(SUM(raw_tokens), 0) raw, COALESCE(SUM(markdown_tokens), 0) md "
                      "FROM filings WHERE conversion_status IN ('CHECKED_TEXT', 'REVIEW_REQUIRED')").fetchone()
    steps = [{"step": "raw files (converted filings)", "filings": base["n"], "tokens": base["raw"]},
             {"step": "Markdown", "filings": base["n"], "tokens": base["md"]}]
    for row in db.execute("SELECT model_tag, COUNT(*) n, COALESCE(SUM(tokens_in), 0) t_in, COALESCE(SUM(tokens_kept), 0) kept "
                          "FROM stage_status WHERE stage='classify' AND status='done' GROUP BY model_tag ORDER BY model_tag"):
        share = row["kept"] / row["t_in"] if row["t_in"] else None
        steps.append({"step": f"kept by filter [{row['model_tag']}]", "filings": row["n"], "tokens": row["kept"],
                      "note": f"{share:.1%} of these filings' Markdown" if share is not None else ""})
    return steps


def runs(db, limit: int = 10) -> list[dict]:
    return [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,))]


def run_progress(db, run: dict | None) -> dict | None:
    """Progress, speed and time left for one run (filings counted from stage rows finished since it started)."""
    if not run:
        return None
    stage = run["command"]
    info = dict(run)
    progress = db.execute("SELECT * FROM progress WHERE run_id=?", (run["run_id"],)).fetchone()
    info["chunk"] = dict(progress) if progress else None
    if stage in {"convert", "segment", "classify"}:
        sql = "SELECT COUNT(*) FROM stage_status WHERE stage=? AND model_tag=? AND finished >= ? AND status != 'running'"
        params = [stage, run.get("model_tag") or "", run["started"]]
        if run["finished"]:
            sql += " AND finished <= ?"
            params.append(run["finished"])
        info["completed"] = db.execute(sql, params).fetchone()[0]
    else:
        info["completed"] = None
    started, end = _parse(run["started"]), _parse(run["finished"]) or _now()
    elapsed = (end - started).total_seconds() if started else 0
    info["elapsed_seconds"] = round(elapsed)
    done, planned = info["completed"], run.get("planned")
    info["rate_per_hour"] = round(done / elapsed * 3600, 1) if done and elapsed > 0 else None
    if done and planned and elapsed > 0 and not run["finished"]:
        info["eta_seconds"] = round((planned - done) / (done / elapsed))
    else:
        info["eta_seconds"] = None
    last = max(filter(None, [_parse(progress["updated_at"]) if progress else None,
                             _parse(db.execute("SELECT MAX(finished) FROM stage_status WHERE finished >= ?",
                                               (run["started"],)).fetchone()[0]), started]), default=None)
    info["last_update"] = last.isoformat(timespec="seconds") if last else None
    idle = (_now() - last).total_seconds() if last else None
    if run["finished"]:
        info["state"] = {3: "stopped on request", 130: "interrupted"}.get(run["exit_code"], "finished")
    elif run.get("pid") and not process_alive(run["pid"]):
        info["state"] = "interrupted"  # its process is gone (force-stopped, crashed or closed)
    else:
        info["state"] = "interrupted?" if idle is not None and idle > STALE_SECONDS else "running"
    return info


def active_run(db) -> dict | None:
    row = db.execute("SELECT * FROM runs WHERE finished IS NULL ORDER BY run_id DESC LIMIT 1").fetchone()
    info = run_progress(db, dict(row)) if row else None
    return info if info and info["state"] == "running" else None


def overview(cfg, db) -> dict:
    conversion = {r["s"]: r["n"] for r in db.execute(
        "SELECT COALESCE(conversion_status, 'NOT_CONVERTED') s, COUNT(*) n FROM filings GROUP BY 1 ORDER BY 1")}
    forms = {r["f"]: r["n"] for r in db.execute(
        "SELECT COALESCE(NULLIF(form, ''), 'unknown') f, COUNT(*) n FROM filings GROUP BY 1 ORDER BY 2 DESC")}
    scan = {r["s"]: r["n"] for r in db.execute(
        "SELECT COALESCE(scan_status, 'unknown') s, COUNT(*) n FROM filings GROUP BY 1 ORDER BY 1")}
    return {
        "filings": db.execute("SELECT COUNT(*) FROM filings").fetchone()[0],
        "companies": db.execute("SELECT COUNT(DISTINCT company_dir) FROM filings").fetchone()[0],
        "conversion": conversion, "forms": forms, "scan": scan,
        "stages": stage_counts(db), "tokens": token_totals(db),
        "disk": disk.status(cfg.outputs_dir, cfg.min_free_gb),
        "active": active_run(db), "job": active_job(db), "runs": runs(db),
    }


def filing_list(db, q: str = "", year: str = "", form: str = "", stage: str = "", status: str = "",
                page: int = 1) -> dict:
    where, args = [], []
    if q:
        where.append("(lower(f.filing_id) LIKE ? OR lower(f.ticker)=? OR lower(f.company_dir) LIKE ? OR ltrim(f.cik,'0')=ltrim(?,'0'))")
        key = q.lower()
        args += [f"%{key}%", key, f"%{key}%", key]
    if year.isdigit():
        where.append("f.year = ?")
        args.append(int(year))
    if form:
        where.append("upper(f.form) = ?")
        args.append(form.upper())
    if stage:
        if status == "not started":
            where.append("NOT EXISTS (SELECT 1 FROM stage_status s WHERE s.filing_id=f.filing_id AND s.stage=?)")
            args.append(stage)
        else:
            where.append("EXISTS (SELECT 1 FROM stage_status s WHERE s.filing_id=f.filing_id AND s.stage=?"
                         + (" AND s.status=?" if status else "") + ")")
            args += [stage] + ([status] if status else [])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = db.execute(f"SELECT COUNT(*) FROM filings f {clause}", args).fetchone()[0]
    page = max(1, page)
    rows = [dict(r) for r in db.execute(
        f"SELECT f.filing_id, f.company_dir, f.ticker, f.cik, f.year, f.form, f.is_amendment, f.conversion_status, "
        f"f.markdown_tokens FROM filings f {clause} ORDER BY f.filing_id LIMIT ? OFFSET ?",
        args + [PAGE_SIZE, (page - 1) * PAGE_SIZE])]
    ids = [r["filing_id"] for r in rows]
    stages: dict = {}
    structure_detail: dict = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for s in db.execute(f"SELECT filing_id, stage, model_tag, status, detail FROM stage_status WHERE filing_id IN ({marks})", ids):
            label = s["stage"] + (f"[{s['model_tag']}]" if s["model_tag"] else "")
            stages.setdefault(s["filing_id"], []).append((label, s["status"]))
            if s["stage"] == "segment" and s["detail"]:
                structure_detail[s["filing_id"]] = json.loads(s["detail"])
    for row in rows:
        row["stages"] = sorted(stages.get(row["filing_id"], []))
        row["structure"] = structure_detail.get(row["filing_id"])
    return {"rows": rows, "total": total, "page": page, "pages": max(1, -(-total // PAGE_SIZE))}


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def filing_detail(cfg, db, filing_id: str, model: str | None = None) -> dict | None:
    from pipeline.classify import stage as classify_stage
    from pipeline.segment import stage as segment_stage

    row = db.execute("SELECT * FROM filings WHERE filing_id=?", (filing_id,)).fetchone()
    if row is None:
        return None
    filing = corpus._from_row(cfg, row)
    stages = [dict(r) for r in db.execute("SELECT * FROM stage_status WHERE filing_id=? ORDER BY stage, model_tag", (filing_id,))]
    models = [s["model_tag"] for s in stages if s["stage"] == "classify"]
    model = model if model in models else (models[0] if models else None)
    segments_path = segment_stage.paths(cfg, filing)[0]
    chunks = _read_jsonl(segments_path) if segments_path.is_file() and _inside(segments_path, cfg.outputs_dir) else []
    labels, summary = {}, None
    if model:
        out = classify_stage.output_paths(cfg, model, filing)
        if out["labels"].is_file() and _inside(out["labels"], cfg.outputs_dir):
            labels = {r["chunk_id"]: r for r in _read_jsonl(out["labels"])}
        if out["summary"].is_file():
            summary = json.loads(out["summary"].read_text(encoding="utf-8"))
    for chunk in chunks:
        chunk["label"] = labels.get(chunk["chunk_id"])
    tokens = [("raw file", row["raw_tokens"]), ("Markdown", row["markdown_tokens"]),
              (f"kept by filter [{model}]" if model else "kept by filter", summary["tokens_kept"] if summary else None),
              ("clean file (TASK-08)", None)]
    return {"filing": dict(row), "stages": stages, "models": models, "model": model, "chunks": chunks,
            "summary": summary, "tokens": tokens}


def chunk_text(cfg, db, filing_id: str, chunk_id: str, model: str | None = None) -> dict | None:
    if not CHUNK_ID.fullmatch(chunk_id or ""):
        return None
    detail = filing_detail(cfg, db, filing_id, model)
    if detail is None:
        return None
    chunk = next((c for c in detail["chunks"] if c["chunk_id"] == chunk_id), None)
    markdown_path = corpus._from_row(cfg, db.execute("SELECT * FROM filings WHERE filing_id=?", (filing_id,)).fetchone()).markdown_path
    if chunk is None or not markdown_path.is_file() or not _inside(markdown_path, cfg.markdown_dir):
        return None
    text = markdown_path.read_bytes().decode("utf-8")[chunk["start"]:chunk["end"]]
    return {"filing_id": filing_id, "chunk": chunk, "text": text, "model": detail["model"]}


def problems(db, limit: int = 500) -> dict:
    q = lambda sql, *a: [dict(r) for r in db.execute(sql + f" LIMIT {limit}", a)]
    return {
        "input": q("SELECT filing_id, scan_status, conversion_status FROM filings WHERE scan_status IN "
                   "('missing_source', 'source_changed') OR conversion_status IN ('ERROR', 'MISSING_SOURCE') ORDER BY filing_id"),
        "failed": q("SELECT filing_id, stage, model_tag, error, finished FROM stage_status WHERE status='failed' ORDER BY stage, filing_id"),
        "chunk_failures": q("SELECT filing_id, model_tag, error FROM stage_status WHERE stage='classify' AND status='done' "
                            "AND error IS NOT NULL ORDER BY filing_id"),
        "nothing_kept": q("SELECT filing_id, model_tag, tokens_in FROM stage_status WHERE stage='classify' AND status='done' "
                          "AND tokens_kept = 0 AND tokens_in > 0 ORDER BY filing_id"),
        "interrupted": q("SELECT s.filing_id, s.stage, s.model_tag, s.started FROM stage_status s WHERE s.status='running' "
                         "ORDER BY s.started"),
        "compaction_fallbacks": None,  # available with TASK-08
    }


# ---- runs grouped into jobs --------------------------------------------------------------

STEP_ORDER = ["scan", "convert", "segment", "classify"]
STEP_NAMES = {"scan": "Scan", "convert": "Convert", "segment": "Segment", "classify": "Filter"}


def local_time(stamp: str | None) -> str:
    parsed = _parse(stamp)
    return parsed.astimezone().strftime("%m-%d %H:%M:%S") if parsed else ""


def describe_selection(args: dict) -> str:
    """The run's filters in plain words."""
    parts = []
    if args.get("filing"):
        parts.append(f"filing {args['filing']}")
    if args.get("company"):
        parts.append(f"company {args['company']}")
    if args.get("year"):
        parts.append(f"year {args['year']}")
    if args.get("form"):
        parts.append(f"form {args['form']}")
    text = ", ".join(parts) if parts else "all registered filings"
    if args.get("limit"):
        text += f" (first {args['limit']})"
    extras = []
    if args.get("fake_model"):
        extras.append("fake model")
    if args.get("force"):
        extras.append("redo finished")
    if args.get("reconvert_changed"):
        extras.append("reconvert changed sources")
    return text + (" · " + ", ".join(extras) if extras else "")


RESULT = {"running": ("Running", "ok"), "finished": ("Finished", "ok"), "finished with errors": ("Finished with errors", "warn"),
          "stopped on request": ("Stopped on request", "warn"), "interrupted": ("Interrupted", "bad"),
          "interrupted?": ("Possibly interrupted", "warn")}


def _result(run_info: dict) -> str:
    state = run_info["state"]
    if state == "finished" and run_info.get("exit_code") == 1:
        return "finished with errors"
    return state


def jobs(db, limit: int = 40) -> list[dict]:
    """One entry per user action: an `all` command with its steps, or a single command.
    Older `all` runs (recorded before job ids existed) are grouped by identical arguments."""
    rows = [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit * 6,))]
    by_job: dict[int, list[dict]] = {}
    for row in rows:
        if row.get("job_id"):
            by_job.setdefault(row["job_id"], []).append(row)
    entries: list[dict] = []
    legacy: list[dict] = []

    def flush_legacy():
        if legacy:
            entries.append({"head": None, "steps": list(reversed(legacy))})
            legacy.clear()

    for row in rows:  # newest first
        if row.get("job_id"):
            continue
        if row["command"] == "all":
            flush_legacy()
            entries.append({"head": row, "steps": sorted(by_job.get(row["run_id"], []), key=lambda r: r["run_id"])})
            continue
        args = json.loads(row["args"] or "{}")
        if args.get("command") == "all":  # older step of an `all` command without a job id
            if legacy and (legacy[-1]["args"] != row["args"] or legacy[-1]["command"] == "scan"):
                flush_legacy()
            legacy.append(row)
            continue
        flush_legacy()
        entries.append({"head": None, "steps": [row]})
    flush_legacy()

    result = []
    for entry in entries[:limit]:
        steps = [run_progress(db, s) for s in entry["steps"]]
        head = run_progress(db, entry["head"]) if entry["head"] else None
        first = entry["head"] or entry["steps"][0]
        args = json.loads(first["args"] or "{}")
        state_of = head or (steps[-1] if steps else None)
        result_state = _result(state_of) if state_of else "finished"
        if not head and any(s["state"] == "running" for s in steps):
            result_state = "running"
        started = _parse(first["started"])
        last_finish = (head or steps[-1])["finished"] if (head or steps) else None
        end = _parse(last_finish) if last_finish else _now()
        result.append({
            "id": first["run_id"], "kind": "all" if (head or len(steps) > 1) else first["command"],
            "started": local_time(first["started"]), "selection": describe_selection(args),
            "model": first.get("model_tag") or next((s.get("model_tag") for s in steps if s.get("model_tag")), ""),
            "result": result_state, "result_label": RESULT.get(result_state, (result_state, "muted")),
            "duration": (end - started).total_seconds() if started else None,
            "steps": [{**s, "name": STEP_NAMES.get(s["command"], s["command"]), "started_local": local_time(s["started"]),
                       "finished_local": local_time(s["finished"])} for s in sorted(steps, key=lambda s: STEP_ORDER.index(s["command"]) if s["command"] in STEP_ORDER else 9)],
        })
    return result


def active_job(db) -> dict | None:
    for job in jobs(db, limit=5):
        if job["result"] == "running":
            return job
    return None


# ---- structure of one filing --------------------------------------------------------------

LABEL_ORDER = ["KEEP", "UNCERTAIN", "DROP"]


def structure(cfg, db, filing_id: str, model: str | None = None) -> dict | None:
    from pipeline.classify import stage as classify_stage
    from pipeline.segment import stage as segment_stage

    row = db.execute("SELECT * FROM filings WHERE filing_id=?", (filing_id,)).fetchone()
    if row is None:
        return None
    filing = corpus._from_row(cfg, row)
    sections_file = segment_stage.sections_path(cfg, filing)
    segments_file = segment_stage.paths(cfg, filing)[0]
    if not (sections_file.is_file() and _inside(sections_file, cfg.outputs_dir)):
        return {"filing": dict(row), "missing": True}
    data = json.loads(sections_file.read_text(encoding="utf-8"))
    sections = data["sections"]
    chunks = _read_jsonl(segments_file) if segments_file.is_file() else []
    models = [r["model_tag"] for r in db.execute(
        "SELECT model_tag FROM stage_status WHERE filing_id=? AND stage='classify' AND status='done'", (filing_id,))]
    model = model if model in models else (models[0] if models else None)
    labels = {}
    if model:
        out = classify_stage.output_paths(cfg, model, filing)
        if out["labels"].is_file() and _inside(out["labels"], cfg.outputs_dir):
            labels = {r["chunk_id"]: r["final_label"] for r in _read_jsonl(out["labels"])}

    # tokens and labels per section, from the chunks that overlap it (split by character overlap)
    index = {s["section_id"]: s for s in sections}
    for s in sections:
        s.update(tokens=0.0, chunk_ids=[], labels={})
    for chunk in chunks:
        length = max(1, chunk["end"] - chunk["start"])
        for section_id in chunk.get("section_ids") or [chunk["section_id"]]:
            s = index.get(section_id)
            if not s:
                continue
            overlap = max(0, min(chunk["end"], s["end"]) - max(chunk["start"], s["start"]))
            s["tokens"] += chunk["tokens"] * overlap / length
            s["chunk_ids"].append(chunk["chunk_id"])
            label = labels.get(chunk["chunk_id"])
            if label:
                s["labels"][label] = s["labels"].get(label, 0) + 1

    # tree: each heading-path level is a node; sections are the leaves' content
    root = {"title": "", "children": {}, "tokens": 0.0, "chunks": set(), "labels": {}, "sections": [], "start": 0}
    for s in sections:
        node = root
        node["tokens"] += s["tokens"]
        for depth, title in enumerate(s["heading_path"]):
            key = (depth, title)
            child = node["children"].get(key)
            if child is None:
                child = {"title": title, "children": {}, "tokens": 0.0, "chunks": set(), "labels": {},
                         "sections": [], "start": s["start"]}
                node["children"][key] = child
            child["tokens"] += s["tokens"]
            child["chunks"].update(s["chunk_ids"])
            for label, n in s["labels"].items():
                child["labels"][label] = child["labels"].get(label, 0) + n
            node = child
        node["sections"].append(s)

    def finish(node, depth=0):
        return {"title": node["title"], "tokens": round(node["tokens"]), "chunks": len(node["chunks"]),
                "labels": [(l, node["labels"][l]) for l in LABEL_ORDER if node["labels"].get(l)],
                "start": node["start"], "anchor": f"n{node['start']}",
                "sections": [{"id": s["section_id"], "chunk_ids": s["chunk_ids"], "tokens": round(s["tokens"])}
                             for s in node["sections"]],
                "children": [finish(c, depth + 1) for c in node["children"].values()]}

    tree = finish(root)

    # document map: one bar segment per attached document / Item (or Part, Front matter)
    total = max(1, sections[-1]["end"]) if sections else 1
    segments: list[dict] = []
    for s in sections:
        label = (s.get("document") or "") + (" " if s.get("document") else "") + (
            f"Item {s['item']}" if s.get("item") else (s["heading_path"][0] if s["heading_path"] else ""))
        if segments and segments[-1]["label"] == label:
            segments[-1]["chars"] += s["end"] - s["start"]
            segments[-1]["tokens"] += s["tokens"]
        else:
            segments.append({"label": label, "chars": s["end"] - s["start"], "tokens": s["tokens"],
                             "start": s["start"], "anchor": _anchor_for(tree, s["start"])})
    for i, seg in enumerate(segments):
        seg["width"] = round(100 * seg["chars"] / total, 3)
        seg["tokens"] = round(seg["tokens"])
        seg["color"] = i % 8
    chunk_bar = [{"id": c["chunk_id"], "width": round(100 * (c["end"] - c["start"]) / total, 4),
                  "label": labels.get(c["chunk_id"]) or "none"} for c in chunks]
    detail = db.execute("SELECT detail FROM stage_status WHERE filing_id=? AND stage='segment'", (filing_id,)).fetchone()
    return {"filing": dict(row), "missing": False, "quality": data.get("quality"), "info": data.get("info", {}),
            "tree": tree, "segments": segments, "chunk_bar": chunk_bar, "models": models, "model": model,
            "n_sections": len(sections), "n_chunks": len(chunks), "tokens": sum(c["tokens"] for c in chunks),
            "detail": json.loads(detail[0]) if detail and detail[0] else None}


def _anchor_for(tree: dict, start: int) -> str:
    """Anchor of the deepest top-level-ish node that begins at or before `start` (first two levels)."""
    best = "top"
    for child in tree["children"]:
        if child["start"] <= start:
            best = child["anchor"]
            for grandchild in child["children"]:
                if grandchild["start"] <= start:
                    best = grandchild["anchor"]
    return best


def api_status(cfg, db) -> dict:
    active = active_run(db)
    return {"active": active, "stages": stage_counts(db), "disk": disk.status(cfg.outputs_dir, cfg.min_free_gb),
            "time": _now().isoformat(timespec="seconds")}
