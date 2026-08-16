"""NT-truth trade-ledger reconciliation (Rob's directive, 2026-08-16).

The crew's `trades` table only records brackets the scan placed and tracked.
Fills the crew CAUSED but never tracked — stale JC_ stop orders triggering
hours later, orphan positions closed by the trader via CLOSEPOSITION — were
invisible to the ledger, so every PF/expectancy computed from `trades`
silently excluded them (16 untracked 6J fills on 2026-08-12..14 alone).

This job replays NT's executions truth (the fills-derived sync copy the NQ
pipeline maintains — read-only, single-writer rule respected), segments each
crew-legal symbol's fills into flat-to-flat episodes, computes each episode's
realized P&L from cash flow x point_value, subtracts whatever the crew's
trades already booked in that window, and books any residual as an explicit
`nt_ledger_gap` trade row. Idempotent: an episode's gap row is keyed by
(symbol, exit_reason='nt_ledger_gap', closed_at=episode end).

Manual fills on OTHER instruments (e.g. Rob's NQ) are ignored — only the
crew's legal symbol set is reconciled. If Rob manually trades a crew symbol
on this account, that residual will be booked too; on a shared account fills
are attributable per-symbol only.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.logging_setup import setup_logging  # noqa: E402

log = setup_logging("nt_ledger_gap_sync")

CREW_DB = str(REPO / "judas_crew.db")
SINCE = "2026-08-16T00:00:00"  # LFE..104 cutover (account read from config)
MIN_GAP_DOLLARS = 2.0
MATCH_SLOP_S = 600


def _truth_db() -> str:
    import os
    return os.environ.get(
        "JUDAS_NT_TRUTH_DB",
        "/home/rob/.openclaw/workspace/NQ-Trading-PIPELINE/data/nq_pipeline.db",
    )


def _crew_symbols_and_account() -> tuple[set[str], str]:
    from src.config import load_config
    from src.research.lucid_guard import RULES
    cfg = load_config()
    syms = {str(s).upper() for s in dict(cfg.ninjatrader.instrument_map)}
    return syms - set(RULES["banned_symbols"]), cfg.ninjatrader.account


def _episodes(rows: list[sqlite3.Row]) -> list[dict]:
    """Segment time-ordered fills into flat-to-flat episodes.

    Position is reconstructed from position_after deltas (prev starts at 0 —
    correct as long as SINCE predates the account's first fill)."""
    eps: list[dict] = []
    cur: dict | None = None
    prev_pos = 0
    for r in rows:
        pos_after = int(r["position_after"] or 0)
        delta = pos_after - prev_pos
        prev_pos = pos_after
        if delta == 0:
            continue
        if cur is None:
            cur = {"start": r["time_utc"], "cash": 0.0, "max_qty": 0,
                   "first_delta": delta, "fills": 0,
                   "first_px": float(r["price"] or 0),
                   "pv": float(r["point_value"] or 0)}
        cur["cash"] += -delta * float(r["price"] or 0)
        cur["fills"] += 1
        cur["max_qty"] = max(cur["max_qty"], abs(pos_after), abs(delta))
        cur["last_px"] = float(r["price"] or 0)
        if pos_after == 0:
            cur["end"] = r["time_utc"]
            cur["pnl"] = round(cur["cash"] * cur["pv"], 2)
            eps.append(cur)
            cur = None
    return eps  # a trailing open episode (cur) is deliberately dropped — reconciled once flat


def _iso_z(ts: str) -> str:
    return ts[:19].replace(" ", "T") + "Z" if not ts.endswith("Z") else ts


def main() -> int:
    symbols, account = _crew_symbols_and_account()
    truth = sqlite3.connect(f"file:{_truth_db()}?mode=ro", uri=True, timeout=10)
    truth.row_factory = sqlite3.Row
    crew = sqlite3.connect(CREW_DB, timeout=30)
    crew.row_factory = sqlite3.Row

    booked = 0
    consumed: set[int] = set()
    for sym in sorted(symbols):
        rows = truth.execute(
            "SELECT time_utc, position_after, quantity, price, point_value "
            "FROM nt_executions_truth WHERE account=? "
            "AND COALESCE(instrument_name, instrument) LIKE ? AND time_utc>=? "
            "ORDER BY time_utc, nt_execution_id",
            (account, f"{sym}%", SINCE),
        ).fetchall()
        if not rows:
            continue
        for ep in _episodes(rows):
            start_dt = datetime.fromisoformat(ep["start"][:19])
            end_dt = datetime.fromisoformat(ep["end"][:19])
            lo = (start_dt - timedelta(seconds=MATCH_SLOP_S)).strftime("%Y-%m-%dT%H:%M:%SZ")
            hi = (end_dt + timedelta(seconds=MATCH_SLOP_S)).strftime("%Y-%m-%dT%H:%M:%SZ")
            matched = [r for r in crew.execute(
                "SELECT id, pnl_dollars FROM trades WHERE symbol=? AND status='closed' "
                "AND closed_at>=? AND closed_at<=? AND exit_reason!='nt_ledger_gap'",
                (sym, lo, hi)).fetchall() if r["id"] not in consumed]
            consumed.update(r["id"] for r in matched)
            residual = round(ep["pnl"] - sum(float(r["pnl_dollars"] or 0) for r in matched), 2)
            if abs(residual) < MIN_GAP_DOLLARS:
                continue
            end_iso = _iso_z(ep["end"])
            exists = crew.execute(
                "SELECT 1 FROM trades WHERE symbol=? AND exit_reason='nt_ledger_gap' AND closed_at=?",
                (sym, end_iso)).fetchone()
            if exists:
                continue
            crew.execute(
                "INSERT INTO trades (symbol, direction, qty, entry_fill, exit_fill, "
                "pnl_dollars, status, opened_at, closed_at, strategy_family, exit_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sym, "long" if ep["first_delta"] > 0 else "short", ep["max_qty"],
                 ep["first_px"], ep["last_px"], residual, "closed",
                 _iso_z(ep["start"]), end_iso, "nt_ledger_gap", "nt_ledger_gap"))
            booked += 1
            log.warning("ledger_gap.booked %s %s..%s residual=$%.2f (episode=$%.2f, matched=%d)",
                        sym, ep["start"][:16], ep["end"][11:16], residual, ep["pnl"], len(matched))
    crew.commit()
    log.info("ledger_gap.done booked=%d", booked)
    print(json.dumps({"booked": booked}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
