"""Free disk space for the output folders (the monitor warns, long runs pause)."""
from __future__ import annotations

from pathlib import Path
import shutil


def free_bytes(path: Path) -> int:
    path = Path(path)
    while not path.exists() and path != path.parent:  # check the nearest existing folder
        path = path.parent
    return shutil.disk_usage(path).free


def status(path: Path, min_free_gb: float) -> dict:
    free = free_bytes(path)
    total = shutil.disk_usage(Path(path).anchor or path).total
    free_gb = free / 1024 ** 3
    return {"free_gb": round(free_gb, 1), "total_gb": round(total / 1024 ** 3, 1),
            "min_free_gb": min_free_gb, "low": free_gb < min_free_gb}
