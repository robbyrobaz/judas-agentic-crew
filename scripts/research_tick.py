#!/usr/bin/env python3
"""Scheduled research tick with overlap protection and runtime status."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run one scheduled research tick")
    p.add_argument("--symbol", default="MGC")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--reason", default="scheduled")
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
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
        finished_at = _now_utc()
        output_tail = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else ""))[-12000:]

        _write_status(
            {
                "state": "completed" if proc.returncode == 0 else "failed",
                "symbol": args.symbol.upper(),
                "reason": args.reason,
                "started_at_utc": started_at,
                "finished_at_utc": finished_at,
                "returncode": proc.returncode,
                "output_tail": output_tail,
            }
        )
        print(
            json.dumps(
                {
                    "state": "completed" if proc.returncode == 0 else "failed",
                    "symbol": args.symbol.upper(),
                    "reason": args.reason,
                    "started_at_utc": started_at,
                    "finished_at_utc": finished_at,
                    "returncode": proc.returncode,
                },
                indent=2,
            )
        )
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
