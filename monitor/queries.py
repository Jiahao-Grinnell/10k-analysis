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
        "active": active_run(db), "runs": runs(db),
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
    if ids:
        marks = ",".join("?" * len(ids))
        for s in db.execute(f"SELECT filing_id, stage, model_tag, status FROM stage_status WHERE filing_id IN ({marks})", ids):
            label = s["stage"] + (f"[{s['model_tag']}]" if s["model_tag"] else "")
            stages.setdefault(s["filing_id"], []).append((label, s["status"]))
    for row in rows:
        row["stages"] = sorted(stages.get(row["filing_id"], []))
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


def api_status(cfg, db) -> dict:
    active = active_run(db)
    return {"active": active, "stages": stage_counts(db), "disk": disk.status(cfg.outputs_dir, cfg.min_free_gb),
            "time": _now().isoformat(timespec="seconds")}
