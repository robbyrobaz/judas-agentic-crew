#!/usr/bin/env python3
"""Watchdog for judas-crew.service — the trading scanner.

On 2026-05-25 the scanner hung in `activating (start)` for 14 hours
(IBKR reconnect loop + memory leak) and silently produced zero trades.
The unit now has TimeoutStartSec=10min as the first line of defense.

This script is the second line: runs every 5 min via judas-crew-watchdog.timer
and kills the service if:
  - ActiveState=activating for > 8 minutes (catches the hang shape directly)
  - Resident memory > 350 MB (catches IBKR leak before MemoryMax=400M trips)
  - The service has been running for > 6 minutes total (oneshot should be fast)

Records each kill in the `findings` table so agents can see watchdog
activity in their kickoff briefings.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("JUDAS_DB_PATH", str(REPO_ROOT / "judas_crew.db"))
SERVICE = "judas-crew.service"

# Thresholds — tuned to the actual scanner profile (healthy = <60s, <100MB).
MAX_ACTIVATING_SECONDS = 8 * 60       # 8 min in "activating" → hung
MAX_RUNNING_SECONDS = 6 * 60          # 6 min total runtime → hung
MAX_MEMORY_MB = 350                   # 350MB resident → memory leak


def _show(prop: str) -> str:
    """Read a systemd unit property."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", SERVICE, "-p", prop, "--value"],
            capture_output=True, text=True, timeout=10,
        )
        return (proc.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"<error: {exc}>"


def _service_state() -> dict:
    """Snapshot of the unit's state. Returns dict with active_state, sub_state,
    activating_since (datetime or None), runtime_seconds (float), memory_mb (float)."""
    active_state = _show("ActiveState")
    sub_state = _show("SubState")
    enter_ts = _show("ActiveEnterTimestamp")
    inactive_exit_ts = _show("InactiveExitTimestamp")
    mem_current_bytes = _show("MemoryCurrent")
    main_pid = _show("MainPID")

    # Parse memory.
    try:
        mem_mb = int(mem_current_bytes) / (1024 * 1024) if mem_current_bytes.isdigit() else 0.0
    except (TypeError, ValueError):
        mem_mb = 0.0

    # Parse the timestamp — systemd format like "Mon 2026-05-25 14:40:57 MST"
    # For "activating" we want InactiveExitTimestamp (when it left inactive).
    activating_since = None
    runtime_s = 0.0
    candidates = (inactive_exit_ts if active_state == "activating" else enter_ts,)
    for ts_str in candidates:
        if not ts_str or ts_str in ("n/a", "0"):
            continue
        try:
            # "Mon 2026-05-25 14:40:57 MST" — strip leading day name and rely
            # on `date -d` semantics via fromtimestamp of `date +%s`.
            proc = subprocess.run(
                ["date", "-d", ts_str, "+%s"],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip().isdigit():
                ts_epoch = int(proc.stdout.strip())
                activating_since = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
                runtime_s = time.time() - ts_epoch
                break
        except Exception:  # noqa: BLE001
            continue

    return {
        "active_state": active_state,
        "sub_state": sub_state,
        "main_pid": main_pid,
        "memory_mb": round(mem_mb, 1),
        "runtime_seconds": round(runtime_s, 1),
        "activating_since_utc": activating_since.isoformat() if activating_since else None,
    }


def _should_kill(state: dict) -> tuple[bool, str]:
    """Decide if the scanner should be killed. Returns (kill, reason)."""
    if state["active_state"] == "activating" and state["runtime_seconds"] > MAX_ACTIVATING_SECONDS:
        return True, (
            f"hung in activating for {state['runtime_seconds']:.0f}s "
            f"(threshold {MAX_ACTIVATING_SECONDS}s); pid={state['main_pid']}"
        )
    if state["active_state"] in ("active", "activating") and state["runtime_seconds"] > MAX_RUNNING_SECONDS:
        return True, (
            f"runtime exceeded {MAX_RUNNING_SECONDS}s "
            f"(actual {state['runtime_seconds']:.0f}s); pid={state['main_pid']}"
        )
    if state["memory_mb"] > MAX_MEMORY_MB:
        return True, (
            f"memory leak {state['memory_mb']:.0f}MB > {MAX_MEMORY_MB}MB; "
            f"pid={state['main_pid']}"
        )
    return False, ""


def _kill_service() -> dict:
    """SIGTERM, wait 10s, SIGKILL if still active. Then attempt to clear
    any lingering IBKR clientId 150 lock by waiting before exit (Gateway
    needs ~30s after TCP close to free the slot)."""
    try:
        subprocess.run(["systemctl", "--user", "stop", SERVICE], timeout=15, check=False)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"stop failed: {exc}"}
    # Wait for graceful shutdown.
    for _ in range(10):
        state = _service_state()
        if state["active_state"] in ("inactive", "failed"):
            break
        time.sleep(1)
    else:
        # Still alive — force-kill the process group.
        pid = _show("MainPID")
        if pid and pid.isdigit() and int(pid) > 0:
            try:
                subprocess.run(["kill", "-9", str(pid)], timeout=5, check=False)
            except Exception:  # noqa: BLE001
                pass
    return {"ok": True}


def _record_finding(*, title: str, body: str, refs: dict) -> None:
    """Best-effort finding insert. Don't fail the watchdog if DB unavailable."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            conn.execute(
                """
                INSERT INTO findings (
                    created_at_utc, author, title, body, refs_json, status
                ) VALUES (?, 'scanner_watchdog', ?, ?, ?, 'active')
                """,
                (
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    title,
                    body,
                    json.dumps(refs, default=str),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"watchdog: finding insert failed: {exc}", file=sys.stderr)


def main() -> int:
    state = _service_state()
    kill, reason = _should_kill(state)
    log_payload = {
        "watchdog_check_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state": state,
        "action": "kill" if kill else "noop",
        "reason": reason,
    }
    print(json.dumps(log_payload))
    if not kill:
        return 0
    result = _kill_service()
    _record_finding(
        title=f"SCANNER WATCHDOG: killed judas-crew.service ({state['active_state']})",
        body=(
            f"Reason: {reason}\n"
            f"State at kill: {json.dumps(state, default=str)}\n"
            f"Kill result: {json.dumps(result, default=str)}\n\n"
            "If the next hourly cycle fails with 'clientId 150 already in use', "
            "wait 60s and retry — IBKR Gateway needs time to release the slot."
        ),
        refs={"service": SERVICE, "state_at_kill": state, "reason": reason},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
