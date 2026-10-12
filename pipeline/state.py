"""SQLite status database. It is an index rebuilt from files, never the source of truth."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from pipeline.records import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    filing_id TEXT PRIMARY KEY, company_dir TEXT, ticker TEXT, company_id TEXT, year INTEGER,
    form TEXT, raw_path TEXT, markdown_path TEXT, conversion_status TEXT, raw_format TEXT,
    markdown_tokens INTEGER);
CREATE TABLE IF NOT EXISTS stage_status (
    filing_id TEXT NOT NULL, stage TEXT NOT NULL, model_tag TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, started TEXT, finished TEXT, tokens_in INTEGER, tokens_kept INTEGER,
    error TEXT, PRIMARY KEY (filing_id, stage, model_tag));
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, command TEXT, args TEXT, started TEXT,
    finished TEXT, exit_code INTEGER);
CREATE TABLE IF NOT EXISTS progress (
    run_id INTEGER PRIMARY KEY, filing_id TEXT, chunks_done INTEGER, chunks_total INTEGER,
    updated_at TEXT);
"""


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(SCHEMA)
    connection.row_factory = sqlite3.Row
    return connection


def upsert_filing(db: sqlite3.Connection, filing, conversion_status: str, raw_format: str | None,
                  markdown_tokens: int | None) -> None:
    db.execute(
        """INSERT INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(filing_id) DO UPDATE SET company_dir=excluded.company_dir, ticker=excluded.ticker,
           company_id=excluded.company_id, year=excluded.year, form=excluded.form, raw_path=excluded.raw_path,
           markdown_path=excluded.markdown_path, conversion_status=excluded.conversion_status,
           raw_format=excluded.raw_format, markdown_tokens=excluded.markdown_tokens""",
        (filing.filing_id, filing.company_dir, filing.ticker, filing.company_id, filing.year, filing.form,
         str(filing.raw_path), str(filing.markdown_path), conversion_status, raw_format, markdown_tokens))


def set_stage(db: sqlite3.Connection, filing_id: str, stage: str, status: str, *, model_tag: str = "",
              started: str | None = None, finished: str | None = None, tokens_in: int | None = None,
              tokens_kept: int | None = None, error: str | None = None) -> None:
    db.execute(
        """INSERT INTO stage_status VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(filing_id, stage, model_tag) DO UPDATE SET status=excluded.status,
           started=excluded.started, finished=excluded.finished, tokens_in=excluded.tokens_in,
           tokens_kept=excluded.tokens_kept, error=excluded.error""",
        (filing_id, stage, model_tag, status, started, finished, tokens_in, tokens_kept, error))


def start_run(db: sqlite3.Connection, command: str, args: dict) -> int:
    cursor = db.execute("INSERT INTO runs (command, args, started) VALUES (?,?,?)",
                        (command, json.dumps(args, default=str), now()))
    db.commit()
    return cursor.lastrowid


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
        conversion = {r["conversion_status"]: r["n"] for r in db.execute(
            "SELECT conversion_status, COUNT(*) n FROM filings GROUP BY 1 ORDER BY 1")}
        totals = db.execute("SELECT COUNT(*) n, COALESCE(SUM(markdown_tokens), 0) t FROM filings "
                            "WHERE conversion_status IN ('CHECKED_TEXT', 'REVIEW_REQUIRED')").fetchone()
        last = db.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        return {
            "filings": db.execute("SELECT COUNT(*) FROM filings").fetchone()[0],
            "converted": totals["n"], "markdown_tokens": totals["t"],
            "conversion_status": conversion, "stages": stages,
            "last_run": dict(last) if last else None,
        }
