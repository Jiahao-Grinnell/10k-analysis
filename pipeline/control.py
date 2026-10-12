"""Clean stop between filings: a stop-request file that every stage checks before each filing."""
from __future__ import annotations

from pathlib import Path

EXIT_STOPPED = 3  # exit code of a command that stopped on request


def stop_file(cfg) -> Path:
    return cfg.runs_dir / "stop.request"


def request_stop(cfg) -> None:
    path = stop_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop after the current filing\n", encoding="utf-8")


def stop_requested(cfg) -> bool:
    return stop_file(cfg).exists()


def clear_stop(cfg) -> None:
    stop_file(cfg).unlink(missing_ok=True)
