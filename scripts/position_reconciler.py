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


def main() -> int:
    try:
        from src.portfolio_runtime import _orphan_body
        db_path = str(REPO / "judas_crew.db")
        n = _orphan_body(db_path)
        if n > 0:
            log.critical("reconciler.unmanaged_contracts count=%d — task queued", n)
            _kick_trader()
        else:
            log.info("reconciler.clean unmanaged=0")
    except Exception:  # noqa: BLE001
        log.exception("reconciler.run_failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
