"""Start and stop a pipeline run from the monitor.

The run is the same `python -m pipeline all ...` command as on the command line,
launched as a separate process with an argument list (never a shell). Its PID and
log path are kept in runs/web_run.json, so the state survives a monitor restart.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from pipeline import control
from pipeline.records import atomic_text

ROOT = Path(__file__).resolve().parent.parent
LOG_TAIL_LINES = 60


class RunRefused(Exception):
    """Start was refused; the message explains why."""


def _state_file(cfg) -> Path:
    return cfg.runs_dir / "web_run.json"


def _kernel32():
    """kernel32 with explicit signatures (handles are pointer-sized on 64-bit Windows)."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def _process_info(pid: int) -> tuple[bool, int | None]:
    """(alive, creation time). The creation time tells our process apart from a later one reusing its PID."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True, None
        except OSError:
            return False, None
    # os.kill(pid, 0) would terminate the process on Windows; query it instead.
    import ctypes
    from ctypes import wintypes
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False, None
    try:
        code = wintypes.DWORD()
        alive = bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        times = [wintypes.FILETIME() for _ in range(4)]
        ok = kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times))
        created = (times[0].dwHighDateTime << 32 | times[0].dwLowDateTime) if ok else None
        return alive, created
    finally:
        kernel32.CloseHandle(handle)


def _alive(info: dict) -> bool:
    alive, created = _process_info(info["pid"])
    return alive and (info.get("created") is None or created == info["created"])


def _terminate(pid: int) -> None:
    """End the process and its children. On Windows a venv's python.exe is a launcher that
    runs the real interpreter as a child, so killing only the launcher would leave the run going."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    else:
        os.killpg(os.getpgid(pid), 15)


def current(cfg) -> dict | None:
    """The last run started from the monitor, with `alive` and the tail of its log."""
    path = _state_file(cfg)
    if not path.is_file():
        return None
    info = json.loads(path.read_text(encoding="utf-8"))
    info["alive"] = _alive(info)
    info["stop_requested"] = control.stop_requested(cfg)
    log = Path(info["log"])
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.is_file() else []
    info["log_tail"] = "\n".join(lines[-LOG_TAIL_LINES:])
    return info


def command(cfg, company: str = "", year: str = "", form: str = "", limit: str = "", fake_model: bool = False,
            fake_delay: str = "") -> list[str]:
    """Build the argument list; values are validated, never interpreted by a shell."""
    args = [sys.executable, "-m", "pipeline", "--config", str(cfg.path), "all"]
    for flag, value in [("--company", company), ("--form", form)]:
        value = value.strip()
        if value:
            if len(value) > 100 or value.startswith("-"):
                raise RunRefused(f"Invalid {flag[2:]} value")
            args += [flag, value]
    for flag, value in [("--year", year), ("--limit", limit)]:
        value = value.strip()
        if value:
            if not value.isdigit() or int(value) <= 0:
                raise RunRefused(f"{flag[2:].capitalize()} must be a positive whole number")
            args += [flag, value]
    if fake_model:
        args.append("--fake-model")
        delay = fake_delay.strip()
        if delay:
            try:
                if not 0 <= float(delay) <= 10:
                    raise ValueError
            except ValueError:
                raise RunRefused("Fake delay must be between 0 and 10 seconds") from None
            args += ["--fake-delay", delay]
    return args


def start(cfg, args: list[str], db_active_run: dict | None) -> dict:
    running = current(cfg)
    if running and running["alive"]:
        raise RunRefused(f"A run started from this page is still active (PID {running['pid']}).")
    if db_active_run:
        raise RunRefused(f"Run #{db_active_run['run_id']} ({db_active_run['command']}) is still active, "
                         "probably started from the command line. Wait for it or stop it there.")
    control.clear_stop(cfg)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log = cfg.runs_dir / "logs" / f"web_{stamp}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    with log.open("w", encoding="utf-8") as stream:
        stream.write("$ " + " ".join(args[1:]) + "\n\n")
        stream.flush()
        process = subprocess.Popen(args, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, creationflags=flags, env=env)
    info = {"pid": process.pid, "created": _process_info(process.pid)[1], "command": args[3:], "log": str(log),
            "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    atomic_text(_state_file(cfg), json.dumps(info, indent=2))
    return info


def request_stop(cfg) -> None:
    """Ask the run to stop after the filing in progress."""
    control.request_stop(cfg)


def force_stop(cfg) -> bool:
    running = current(cfg)
    if not running or not running["alive"]:  # alive also checks the creation time, so a reused PID is never killed
        return False
    _terminate(running["pid"])
    return True
