"""SQLite status database. It is an index rebuilt from files, never the source of truth."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

from pipeline.records import now

# Registry columns (filled by `scan`) followed by conversion columns (filled by `convert`).
FILING_COLUMNS = {
    "filing_id": "TEXT PRIMARY KEY", "company_dir": "TEXT", "ticker": "TEXT", "company_id": "TEXT",
    "cik": "TEXT", "year": "INTEGER", "form": "TEXT", "accession": "TEXT", "filing_date": "TEXT",
    "period": "TEXT", "is_amendment": "INTEGER", "amends_filing_id": "TEXT", "raw_path": "TEXT",
    "markdown_path": "TEXT", "raw_sha256": "TEXT", "manifest_json": "TEXT", "scan_status": "TEXT",
    "first_seen": "TEXT", "last_seen": "TEXT",
    "conversion_status": "TEXT", "raw_format": "TEXT", "markdown_tokens": "INTEGER", "raw_tokens": "INTEGER",
}
REGISTRY_FIELDS = [c for c in FILING_COLUMNS
                   if c not in {"conversion_status", "raw_format", "markdown_tokens", "raw_tokens"}]

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (""" + ", ".join(f"{k} {v}" for k, v in FILING_COLUMNS.items()) + """);
CREATE TABLE IF NOT EXISTS stage_status (
    filing_id TEXT NOT NULL, stage TEXT NOT NULL, model_tag TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, started TEXT, finished TEXT, tokens_in INTEGER, tokens_kept INTEGER,
    error TEXT, detail TEXT, PRIMARY KEY (filing_id, stage, model_tag));
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, command TEXT, args TEXT, started TEXT,
    finished TEXT, exit_code INTEGER, planned INTEGER, model_tag TEXT, pid INTEGER, job_id INTEGER);
CREATE TABLE IF NOT EXISTS progress (
    run_id INTEGER PRIMARY KEY, filing_id TEXT, chunks_done INTEGER, chunks_total INTEGER,
    updated_at TEXT);
CREATE INDEX IF NOT EXISTS stage_status_by_status ON stage_status (stage, status);
"""
INDEXES = """
CREATE INDEX IF NOT EXISTS filings_by_year ON filings (year);
CREATE INDEX IF NOT EXISTS filings_by_form ON filings (form);
CREATE INDEX IF NOT EXISTS filings_by_company ON filings (company_dir);
"""


def _migrate(connection: sqlite3.Connection) -> None:
    """Add registry columns to a database created by an older version (the DB is rebuildable)."""
    existing = {row[1] for row in connection.execute("PRAGMA table_info(filings)")}
    for name, kind in FILING_COLUMNS.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE filings ADD COLUMN {name} {kind.replace(' PRIMARY KEY', '')}")
    run_columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    for name, kind in {"planned": "INTEGER", "model_tag": "TEXT", "pid": "INTEGER", "job_id": "INTEGER"}.items():
        if name not in run_columns:
            connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")
    if "detail" not in {row[1] for row in connection.execute("PRAGMA table_info(stage_status)")}:
        connection.execute("ALTER TABLE stage_status ADD COLUMN detail TEXT")
    connection.executescript(INDEXES)


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(SCHEMA)
        _migrate(connection)
    connection.row_factory = sqlite3.Row
    return connection


def register(db: sqlite3.Connection, values: dict) -> None:
    """Insert or update a filing's registry fields; conversion fields are left untouched."""
    columns = [c for c in REGISTRY_FIELDS if c in values]
    updates = ", ".join(f"{c}=excluded.{c}" for c in columns if c not in {"filing_id", "first_seen"})
    db.execute(f"INSERT INTO filings ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "
               f"ON CONFLICT(filing_id) DO UPDATE SET {updates}", [values[c] for c in columns])


def set_conversion(db: sqlite3.Connection, filing_id: str, status: str, raw_format: str | None,
                   markdown_tokens: int | None, raw_tokens: int | None = None) -> None:
    db.execute("UPDATE filings SET conversion_status=?, raw_format=?, markdown_tokens=?, raw_tokens=? WHERE filing_id=?",
               (status, raw_format, markdown_tokens, raw_tokens, filing_id))


def set_stage(db: sqlite3.Connection, filing_id: str, stage: str, status: str, *, model_tag: str = "",
              started: str | None = None, finished: str | None = None, tokens_in: int | None = None,
              tokens_kept: int | None = None, error: str | None = None, detail: dict | None = None) -> None:
    """`detail` is a small JSON summary shown by the monitor (e.g. segmentation quality and counts)."""
    db.execute(
        """INSERT INTO stage_status VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(filing_id, stage, model_tag) DO UPDATE SET status=excluded.status,
           started=excluded.started, finished=excluded.finished, tokens_in=excluded.tokens_in,
           tokens_kept=excluded.tokens_kept, error=excluded.error, detail=excluded.detail""",
        (filing_id, stage, model_tag, status, started, finished, tokens_in, tokens_kept, error,
         json.dumps(detail) if detail else None))


def start_run(db: sqlite3.Connection, command: str, args: dict, planned: int | None = None,
              model_tag: str = "", job_id: int | None = None) -> int:
    """`planned` is the number of filings selected, used by the monitor for progress and time left.
    The process ID lets the monitor tell a live run from one whose process is gone.
    `job_id` groups the steps of one `all` command (the run row of that `all` command)."""
    cursor = db.execute("INSERT INTO runs (command, args, started, planned, model_tag, pid, job_id) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (command, json.dumps(args, default=str), now(), planned, model_tag, os.getpid(), job_id))
    db.commit()
    return cursor.lastrowid


def set_progress(db: sqlite3.Connection, run_id: int | None, filing_id: str, done: int, total: int) -> None:
    if run_id is None:
        return
    db.execute("""INSERT INTO progress VALUES (?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
                  filing_id=excluded.filing_id, chunks_done=excluded.chunks_done,
                  chunks_total=excluded.chunks_total, updated_at=excluded.updated_at""",
               (run_id, filing_id, done, total, now()))
    db.commit()


def finish_run(db: sqlite3.Connection, run_id: int, exit_code: int) -> None:
    db.execute("UPDATE runs SET finished=?, exit_code=? WHERE run_id=?", (now(), exit_code, run_id))
    db.commit()


def summary(path: Path) -> dict | None:
    """Counts for `pipeline status`; None when the database does not exist yet."""
    if not path.is_file():
        return None
    with closing(connect(path, readonly=True)) as db:
        stages: dict = {}
        for row in db.execute("SELECT stage, model_tag, status, COUNT(*) n FROM stage_status GROUP BY 1,2,3 ORDER BY 1,2,3"):
            key = row["stage"] + (f" [{row['model_tag']}]" if row["model_tag"] else "")
            stages.setdefault(key, {})[row["status"]] = row["n"]
        conversion = {r["s"]: r["n"] for r in db.execute(
            "SELECT COALESCE(conversion_status, 'NOT_CONVERTED') s, COUNT(*) n FROM filings GROUP BY 1 ORDER BY 1")}
        scan = {r["s"]: r["n"] for r in db.execute(
            "SELECT COALESCE(scan_status, 'unknown') s, COUNT(*) n FROM filings GROUP BY 1 ORDER BY 1")}
        forms = {r["form"] or "unknown": r["n"] for r in db.execute(
            "SELECT form, COUNT(*) n FROM filings GROUP BY 1 ORDER BY 2 DESC")}
        totals = db.execute("SELECT COUNT(*) n, COALESCE(SUM(markdown_tokens), 0) t FROM filings "
                            "WHERE conversion_status IN ('CHECKED_TEXT', 'REVIEW_REQUIRED')").fetchone()
        last = db.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        return {
            "filings": db.execute("SELECT COUNT(*) FROM filings").fetchone()[0],
            "converted": totals["n"], "markdown_tokens": totals["t"],
            "conversion_status": conversion, "scan_status": scan, "forms": forms, "stages": stages,
            "last_run": dict(last) if last else None,
        }
