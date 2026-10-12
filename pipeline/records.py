"""Atomic writes and per-filing record files that decide skip-or-rerun.

A record is written last, after all of a filing's outputs. A filing counts as
done for a stage only when its record exists, matches the current inputs,
stage version and settings, and every listed output still has its hash.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from convertmd import atomic_text


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def settings_sha256(settings: dict) -> str:
    canonical = json.dumps(settings, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_json(path: Path, value) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_record(path: Path, *, stage: str, stage_version: str, filing_id: str, inputs: dict[str, str],
                 settings: dict, outputs: list[Path], status: str = "done", started: str | None = None,
                 error: str | None = None, extra: dict | None = None) -> dict:
    """Outputs are stored relative to the record's folder with their SHA-256."""
    record = {
        "stage": stage, "stage_version": stage_version, "filing_id": filing_id,
        "inputs": inputs, "settings_sha256": settings_sha256(settings), "status": status,
        "outputs": {p.relative_to(path.parent).as_posix(): sha256_file(p) for p in outputs},
        "started": started, "finished": now(), "error": error, **(extra or {}),
    }
    write_json(path, record)
    return record


def is_current(path: Path, *, stage_version: str, inputs: dict[str, str], settings: dict) -> bool:
    if not path.is_file():
        return False
    try:
        record = read_json(path)
    except (OSError, ValueError):
        return False
    if (record.get("status") != "done" or record.get("stage_version") != stage_version
            or record.get("inputs") != inputs or record.get("settings_sha256") != settings_sha256(settings)):
        return False
    for relative, expected in record.get("outputs", {}).items():
        output = path.parent / relative
        if not output.is_file() or sha256_file(output) != expected:
            return False
    return True
