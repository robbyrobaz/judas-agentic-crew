#!/usr/bin/env python3
"""Heavy research blitz: cycle through every active symbol, run a full
ResearchCrew pass per symbol, log structured progress. Designed to be
launched via nohup over a weekend or pre-market window so the system
chews through experiments while the operator is away.

Each individual symbol run is wrapped by `scripts/research_tick.py`,
so the existing flock + 45-min timeout + stale-PID reaper protect us.
Sequential per-symbol — never two crews in flight.

Usage:
    nohup .venv/bin/python scripts/research_blitz.py \
        --hours 24 --interval-min 5 \
        > logs/research_blitz.log 2>&1 &

Cadence controls:
    --hours          total wall-clock duration (default 24)
    --interval-min   seconds between symbol attempts (default 5)
    --symbols        comma-separated overrides (default: all distinct
                     non-pair active symbols + MGC, MNQ baseline)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_BASELINE = ["MGC", "MNQ", "MCL", "MET", "MBT", "DX", "ZF", "6J"]
TICK_SCRIPT = REPO_ROOT / "scripts" / "research_tick.py"
DB_PATH = REPO_ROOT / "judas_crew.db"
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

log = logging.getLogger("research_blitz")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _active_symbols() -> list[str]:
    """Distinct non-pair symbols from active_strategies."""
    if not DB_PATH.exists():
        return list(DEFAULT_BASELINE)
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM active_strategies WHERE state='active'"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return list(DEFAULT_BASELINE)
    out: list[str] = []
    for r in rows:
        sym = (r["symbol"] or "").upper()
        if "/" in sym:
            # pair entry — research per-symbol fires for each leg already via baseline
            continue
        if sym not in out:
            out.append(sym)
    for sym in DEFAULT_BASELINE:
        if sym not in out:
            out.append(sym)
    return out


def _run_one(symbol: str) -> dict:
    started = _utc_iso()
    log.info("blitz.symbol_start", extra={"symbol": symbol})
    proc = subprocess.run(
        [str(PYTHON), str(TICK_SCRIPT), "--symbol", symbol, "--reason", "blitz"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    finished = _utc_iso()
    summary = {
        "symbol": symbol,
        "rc": proc.returncode,
        "started": started,
        "finished": finished,
        "stdout_tail": proc.stdout[-400:] if proc.stdout else "",
        "stderr_tail": proc.stderr[-400:] if proc.stderr else "",
    }
    log.info("blitz.symbol_done", extra=summary)
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--interval-min", type=float, default=5.0,
                   help="Idle seconds between symbol runs (in MINUTES)")
    p.add_argument("--symbols", default=None,
                   help="Comma-separated override symbol list")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else _active_symbols()
    )
    deadline = datetime.now(timezone.utc) + timedelta(hours=args.hours)
    summary_path = REPO_ROOT / "outputs" / "research" / "blitz_log.jsonl"
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    log.warning(
        "blitz.start",
        extra={
            "symbols": symbols,
            "hours": args.hours,
            "interval_min": args.interval_min,
            "deadline_utc": deadline.isoformat(),
        },
    )

    cycle = 0
    while datetime.now(timezone.utc) < deadline:
        cycle += 1
        log.info("blitz.cycle_start", extra={"cycle": cycle})
        for sym in symbols:
            if datetime.now(timezone.utc) >= deadline:
                break
            try:
                result = _run_one(sym)
            except Exception as exc:
                result = {
                    "symbol": sym,
                    "rc": -1,
                    "error": str(exc),
                    "finished": _utc_iso(),
                }
                log.exception("blitz.symbol_failed", extra={"symbol": sym})
            with open(summary_path, "a") as f:
                f.write(json.dumps({"cycle": cycle, **result}) + "\n")
            time.sleep(max(0.0, args.interval_min * 60.0))
        log.info("blitz.cycle_done", extra={"cycle": cycle})

    log.warning("blitz.complete", extra={"cycles": cycle})
    return 0


if __name__ == "__main__":
    sys.exit(main())
