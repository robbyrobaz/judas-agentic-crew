"""1-minute NT-truth position reconciler (Rob's directive, 2026-08-13).

Every minute: read the LIVE NT positions for the configured account (sqlite
Positions table + per-instrument position files) and compare against open DB
trades. Any unmanaged contracts are queued as a high-urgency trader task via
the existing _orphan_body path (which dedups/refreshes), and the trader
specialist is kicked IMMEDIATELY instead of waiting for its hourly timer.

Why this exists: on 2026-08-13 the account sat LONG 3x 6J + 1x MNQ, naked,
for hours — the 5-min scan's orphan check was blind (it queried the sim
account by default, and NT's sqlite Positions table doesn't persist live
Lucid positions). This closes the awareness gap to ~1 minute.

Exit 0 always (best-effort watchdog — a read hiccup must not mark the unit
failed and stack restart loops).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.logging_setup import setup_logging  # noqa: E402

log = setup_logging("position_reconciler")


def _kick_trader() -> None:
    """Start the trader specialist now (no-op if already running)."""
    try:
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "judas-trader.service"],
        ).returncode == 0
        if active:
            log.info("reconciler.trader_already_running")
            return
        subprocess.run(
            ["systemctl", "--user", "start", "--no-block", "judas-trader.service"],
            check=True,
        )
        log.warning("reconciler.trader_kicked — orphan task queued, trader started now")
    except Exception:  # noqa: BLE001
        log.exception("reconciler.trader_kick_failed (hourly timer will pick the task up)")


COOLDOWN_MIN = 60  # don't re-queue/kick for the SAME unmanaged book within this window


def _unmanaged_signature(db_path: str, truth: dict) -> str | None:
    """Sorted 'SYM:SIDE:qty' signature of unmanaged NT contracts, '' if none,
    None on read failure (treat as unknown — do nothing)."""
    import sqlite3
    if not truth.get("ok"):
        return None
    if truth.get("flat"):
        return ""
    managed: dict[tuple[str, str], int] = {}
    with sqlite3.connect(db_path) as conn:
        for sym, direction, qty in conn.execute(
            "SELECT symbol, direction, qty FROM trades WHERE status='open'"
        ).fetchall():
            k = (str(sym).upper(), str(direction).lower())
            managed[k] = managed.get(k, 0) + int(qty or 1)
    parts = []
    for p in truth.get("open_positions", []):
        sym = str(p.get("instrument", "")).upper()
        direction = "long" if p.get("side") == "LONG" else "short"
        excess = int(p.get("qty", 0) or 0) - managed.get((sym, direction), 0)
        if excess > 0:
            parts.append(f"{sym}:{p.get('side')}:{excess}")
    return ",".join(sorted(parts))


def _recently_tasked(db_path: str, signature: str) -> bool:
    """True if a scan_orphan_detector task for this same unmanaged book was
    requested within COOLDOWN_MIN (any status — the crew may have deliberately
    chosen HOLD; re-kicking every minute would just burn LLM quota)."""
    import json
    import sqlite3
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=COOLDOWN_MIN)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM agent_tasks WHERE requester='scan_orphan_detector' "
            "AND requested_at_utc >= ? ORDER BY id DESC LIMIT 5", (cutoff,),
        ).fetchall()
    for (payload,) in rows:
        try:
            ups = json.loads(payload or "{}").get("unmanaged_positions", [])
            sig = ",".join(sorted(
                f"{u['symbol']}:{u['side']}:{u['unmanaged_qty']}" for u in ups))
            if sig == signature:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _cache_truth_snapshot(truth: dict) -> None:
    """Dump the latest NT-truth positions to data/nt_truth_positions.json so the
    dashboard can show LIVE account truth without its own WinRM round-trip
    (fresh to ~1 minute). Best-effort."""
    import json
    from datetime import datetime, timezone
    try:
        if truth.get("ok"):
            truth = dict(truth)
            truth["cached_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            out = REPO / "data" / "nt_truth_positions.json"
            tmp = out.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(truth, indent=1))
            tmp.replace(out)
    except Exception:  # noqa: BLE001
        log.exception("reconciler.truth_cache_failed")


def main() -> int:
    try:
        from src.portfolio_runtime import _orphan_body
        from src.research.agent_tools import get_nt_positions
        db_path = str(REPO / "judas_crew.db")
        truth = get_nt_positions()
        _cache_truth_snapshot(truth)
        sig = _unmanaged_signature(db_path, truth)
        if sig is None:
            log.warning("reconciler.truth_unavailable — skipping this tick")
            return 0
        if not sig:
            log.info("reconciler.clean unmanaged=0")
            return 0
        if _recently_tasked(db_path, sig):
            log.info("reconciler.cooldown same book already tasked <%dmin: %s",
                     COOLDOWN_MIN, sig)
            return 0
        n = _orphan_body(db_path)
        if n > 0:
            log.critical("reconciler.unmanaged_contracts count=%d sig=%s — task queued",
                         n, sig)
            _kick_trader()
    except Exception:  # noqa: BLE001
        log.exception("reconciler.run_failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
