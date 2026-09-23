#!/usr/bin/env python3
"""Roster watchdog — the alarm that was missing on 2026-09-22.

The active roster drained from 25 (Sep 5) to 4 (Sep 20): reviewer/operator
kept retiring ~3 strategies every other day while the researcher, capped on
2026-09-02, produced zero candidates after Sep 16. No process watched the
book size, so nothing complained. This runs daily (judas-roster-watchdog.timer)
and, whenever the roster is below the floor, a legal symbol is uncovered, or
no candidate has been proposed in 3 days, it:
  1. records a finding (author roster_watchdog) with the health snapshot, and
  2. enqueues ONE high-urgency researcher task per uncovered/thin symbol
     (deduped by the queue on target_id) telling it to refill from the library.
It never retires, promotes or places anything.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src import strategy_registry  # noqa: E402
from src.research.agent_tools import make_enqueue_task  # noqa: E402
from src.db.models import get_conn  # noqa: E402

DB_PATH = os.environ.get("JUDAS_DB_PATH", str(REPO_ROOT / "judas_crew.db"))
THIN_PER_SYMBOL = int(os.environ.get("JUDAS_ROSTER_THIN_PER_SYMBOL", "2"))


def _record_finding(title: str, body: str, refs: dict) -> None:
    with get_conn(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO findings (created_at_utc, author, title, body, refs_json, status) "
            "VALUES (?, 'roster_watchdog', ?, ?, ?, 'active')",
            (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), title, body,
             json.dumps(refs, default=str)),
        )
        conn.commit()


def main() -> int:
    h = strategy_registry.roster_health(DB_PATH)
    alarm = h["below_floor"] or bool(h["uncovered_symbols"]) or h["inflow_stalled"]
    print(json.dumps({"roster_health": h, "alarm": alarm}))
    if not alarm:
        return 0
    try:
        from src.research.lucid_guard import tradeable_symbols
        legal = sorted(tradeable_symbols())
    except Exception:  # noqa: BLE001
        legal = sorted(h["per_symbol"])
    thin = [s for s in legal if h["per_symbol"].get(s, 0) < THIN_PER_SYMBOL]
    enq = make_enqueue_task(db_path=DB_PATH, requester="roster_watchdog")
    queued = []
    for sym in thin:
        r = enq(
            team="researcher",
            action="refill_roster",
            payload={"target_id": f"refill:{sym}", "symbol": sym,
                     "n_active_symbol": h["per_symbol"].get(sym, 0)},
            rationale=(f"ROSTER WATCHDOG: {sym} has {h['per_symbol'].get(sym, 0)} active "
                       f"(thin < {THIN_PER_SYMBOL}); book n_active={h['n_active']} "
                       f"floor={h['roster_floor']} candidates_3d={h['candidates_3d']}. "
                       f"Refill from the LIBRARY block in your briefing: run_custom_backtest "
                       f"(gives the cost_model=v1 stamp) on the listed custom_strategy_ids for "
                       f"{sym}, then propose_candidate for every net PF > 1.0 / n >= 20 result. "
                       f"Complete this task with the candidate ids you proposed."),
            urgency="high",
        )
        queued.append({"symbol": sym, **r})
    _record_finding(
        title=(f"ROSTER WATCHDOG: n_active={h['n_active']} floor={h['roster_floor']} "
               f"uncovered={','.join(h['uncovered_symbols']) or 'none'} "
               f"candidates_3d={h['candidates_3d']}"),
        body=("The book is below the roster floor, has uncovered legal symbols, or has had "
              "no candidate inflow for 3 days. retire_strategy() now refuses soft retirements "
              "below the floor (strategy_registry._check_roster_floor_for_retire). Researcher "
              "refill tasks queued: " + json.dumps(queued, default=str) + "\n\nHealth: "
              + json.dumps(h, default=str)),
        refs={"health": h, "queued": queued},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
