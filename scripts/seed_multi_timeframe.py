#!/usr/bin/env python3
"""Seed faster-timeframe clones of existing active strategies.

The detectors are timeframe-agnostic; a strategy's ``timeframe`` param just
selects which bars bar_cache feeds it. This clones each existing active
strategy onto the requested timeframes (default 5m + 15m) so the crew starts
producing more frequent signals. These run on the SimJudasCrew SIM account, so
this is forward-testing, not real-money risk — a small bootstrap that gives the
M3 research crew a faster validation loop to expand/tune from.

Usage:
  python scripts/seed_multi_timeframe.py --timeframes 5m,15m \
      --families buffet_zoo,judas_native --symbols MGC,MNQ,MCL --dry-run
  python scripts/seed_multi_timeframe.py --timeframes 5m,15m --limit 8
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.models import get_conn  # noqa: E402
from src.strategy_registry import _ensure_db, _utc_now  # noqa: E402


def _existing_keys(conn) -> set[tuple[str, str, str]]:
    """(symbol, strategy_name, timeframe) already active — for idempotency."""
    keys = set()
    for row in conn.execute(
        "SELECT symbol, params_json FROM active_strategies WHERE state='active'"
    ):
        try:
            p = json.loads(row["params_json"])
        except Exception:
            continue
        keys.add((row["symbol"].upper(), str(p.get("strategy_name", "")), str(p.get("timeframe", "1h"))))
    return keys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeframes", default="5m,15m")
    ap.add_argument("--families", default="buffet_zoo,judas_native",
                    help="source execution_engines/families to clone")
    ap.add_argument("--symbols", default="", help="restrict to these symbols (comma-sep); blank=all")
    ap.add_argument("--limit", type=int, default=0, help="cap number of NEW seeds (0=no cap)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    fams = {f.strip() for f in args.families.split(",") if f.strip()}
    syms = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}

    seeded = 0
    with get_conn(_ensure_db()) as conn:
        existing = _existing_keys(conn)
        sources = conn.execute(
            "SELECT symbol, strategy_family, params_json, metrics_json FROM active_strategies "
            "WHERE state='active'"
        ).fetchall()
        # Only clone genuine 1h sources (don't clone an already-5m row).
        for src in sources:
            try:
                params = json.loads(src["params_json"])
            except Exception:
                continue
            fam = src["strategy_family"]
            sym = src["symbol"].upper()
            src_tf = str(params.get("timeframe", "1h")).lower()
            if fams and fam not in fams and params.get("execution_engine") not in fams:
                continue
            if syms and sym not in syms:
                continue
            if src_tf not in ("1h", "1 hour", "1hour"):
                continue  # only clone from the 1h originals
            base_name = str(params.get("strategy_name", f"{fam}_{sym}".lower()))
            for tf in tfs:
                new_name = f"{base_name}_{tf}"
                if (sym, new_name, tf) in existing:
                    continue
                if args.limit and seeded >= args.limit:
                    break
                new_params = dict(params)
                new_params["timeframe"] = tf
                new_params["strategy_name"] = new_name
                metrics = {"seeded_from": base_name, "seed_timeframe": tf, "bootstrap": True}
                if args.dry_run:
                    print(f"[dry-run] seed {sym} {fam} {new_name} tf={tf}")
                else:
                    conn.execute(
                        "INSERT INTO active_strategies "
                        "(symbol, strategy_family, version, params_json, metrics_json, state, activated_at_utc, notes) "
                        "VALUES (?, ?, 1, ?, ?, 'active', ?, ?)",
                        (sym, fam, json.dumps(new_params), json.dumps(metrics), _utc_now(),
                         f"multi-tf bootstrap: {tf} clone of {base_name} (paper/SimJudasCrew)"),
                    )
                    print(f"seeded {sym} {fam} {new_name} tf={tf}")
                seeded += 1
                existing.add((sym, new_name, tf))
        if not args.dry_run:
            conn.commit()
    print(f"\n{'[dry-run] would seed' if args.dry_run else 'seeded'} {seeded} strategies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
