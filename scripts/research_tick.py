#!/usr/bin/env python3
"""Scheduled research tick with overlap protection and runtime status."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import fcntl

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

from src.config import load_config
from src.db.models import init_db

load_dotenv(REPO_ROOT / ".env")

LOCK_PATH = REPO_ROOT / "outputs" / "research" / ".research.lock"
STATUS_PATH = REPO_ROOT / "outputs" / "research" / "runtime_status.json"
DEFAULT_TIMEOUT_MINUTES = int(os.environ.get("JUDAS_RESEARCH_TIMEOUT_MINUTES", "45"))


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_status(payload: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(payload, indent=2))


def _read_status() -> dict:
    if not STATUS_PATH.exists():
        return {}
    try:
        return json.loads(STATUS_PATH.read_text())
    except Exception:
        return {}


def _parse_utc(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _pid_exists(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _coerce_pid(raw: object) -> int | None:
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.isdigit():
        value = int(raw)
        return value if value > 0 else None
    return None


def _child_pids(parent_pid: int) -> list[int]:
    proc = subprocess.run(
        ["pgrep", "-P", str(parent_pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _runtime_is_stale(current: dict, timeout_minutes: int) -> bool:
    if current.get("state") != "running":
        return False
    started_at = _parse_utc(current.get("started_at_utc"))
    pid = _coerce_pid(current.get("pid"))
    if pid and not _pid_exists(pid):
        return True
    if not started_at:
        return True
    age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
    return age_seconds > timeout_minutes * 60


def _terminate_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(1.0)
    if _pid_exists(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def _terminate_process_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(1.0)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _heal_stale_run(current: dict, timeout_minutes: int, reason: str) -> dict:
    wrapper_pid = _coerce_pid(current.get("pid"))
    child_pid = _coerce_pid(current.get("child_pid"))
    child_pgid = _coerce_pid(current.get("child_pgid"))
    if isinstance(child_pgid, int) and child_pgid > 0:
        _terminate_process_group(child_pgid)
    elif isinstance(child_pid, int) and child_pid > 0:
        _terminate_pid(child_pid)
    elif isinstance(wrapper_pid, int) and wrapper_pid > 0:
        for inferred_child in _child_pids(wrapper_pid):
            _terminate_pid(inferred_child)
    if isinstance(wrapper_pid, int) and wrapper_pid > 0:
        _terminate_pid(wrapper_pid)
    healed = {
        "state": "stale_run_terminated",
        "symbol": current.get("symbol"),
        "reason": reason,
        "checked_at_utc": _now_utc(),
        "timeout_minutes": timeout_minutes,
        "previous_run": current,
    }
    return healed


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run one scheduled research tick")
    p.add_argument("--symbol", default="MGC")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--reason", default="scheduled")
    p.add_argument("--timeout-minutes", type=int, default=DEFAULT_TIMEOUT_MINUTES)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config()
    db_path = REPO_ROOT / cfg.db_path
    init_db(db_path)
    os.environ["JUDAS_DB_PATH"] = str(db_path)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(LOCK_PATH, "w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            current = _read_status()
            if _runtime_is_stale(current, args.timeout_minutes):
                healed = _heal_stale_run(current, args.timeout_minutes, args.reason)
                time.sleep(1.0)
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    payload = {
                        "state": "already_running",
                        "symbol": args.symbol.upper(),
                        "reason": args.reason,
                        "checked_at_utc": _now_utc(),
                        "current_run": current,
                        "stale_heal_attempt": healed,
                    }
                    print(json.dumps(payload, indent=2))
                    return 0
            else:
                payload = {
                    "state": "already_running",
                    "symbol": args.symbol.upper(),
                    "reason": args.reason,
                    "checked_at_utc": _now_utc(),
                    "current_run": current,
                }
                print(json.dumps(payload, indent=2))
                return 0

        started_at = _now_utc()
        _write_status(
            {
                "state": "running",
                "symbol": args.symbol.upper(),
                "reason": args.reason,
                "started_at_utc": started_at,
                "pid": os.getpid(),
                "timeout_minutes": args.timeout_minutes,
            }
        )

        cmd = [
            str(REPO_ROOT / ".venv" / "bin" / "python"),
            str(REPO_ROOT / "scripts" / "run_research.py"),
            "--symbol",
            args.symbol.upper(),
            "--log-level",
            args.log_level,
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        _write_status(
            {
                "state": "running",
                "symbol": args.symbol.upper(),
                "reason": args.reason,
                "started_at_utc": started_at,
                "pid": os.getpid(),
                "child_pid": proc.pid,
                "child_pgid": proc.pid,
                "timeout_minutes": args.timeout_minutes,
            }
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=args.timeout_minutes * 60)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(proc.pid)
            stdout, stderr = proc.communicate(timeout=5)
        finished_at = _now_utc()
        returncode = 124 if timed_out else int(proc.returncode or 0)
        output_tail = ((stdout or "") + ("\n" + stderr if stderr else ""))[-12000:]
        state = "timed_out" if timed_out else ("completed" if returncode == 0 else "failed")

        _write_status(
            {
                "state": state,
                "symbol": args.symbol.upper(),
                "reason": args.reason,
                "started_at_utc": started_at,
                "finished_at_utc": finished_at,
                "returncode": returncode,
                "timeout_minutes": args.timeout_minutes,
                "output_tail": output_tail,
            }
        )
        print(
            json.dumps(
                {
                    "state": state,
                    "symbol": args.symbol.upper(),
                    "reason": args.reason,
                    "started_at_utc": started_at,
                    "finished_at_utc": finished_at,
                    "returncode": returncode,
                    "timeout_minutes": args.timeout_minutes,
                },
                indent=2,
            )
        )
        return returncode


if __name__ == "__main__":
    raise SystemExit(main())
