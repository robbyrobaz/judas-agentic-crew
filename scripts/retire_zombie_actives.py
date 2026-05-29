#!/usr/bin/env python3
"""Retire active judas_native strategies that have no runtime keys.

These are the alias-bag zombies (disp/displacement/lookback/atr_mult only,
no detector_lookback_bars/confirmation_bars/pivot_length/target_r/
stop_buffer_ticks/min_sweep_ticks). The runtime would have fallen back
to default-detector params and every version would have fired the same
trade — see May 22 2026 MNQ -$2,397 duplicate-fire incident.

Idempotent: safe to run repeatedly, only touches active rows that match.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import strategy_registry as sr
from src.db.models import get_conn

_RUNTIME_KEYS = {
    "detector_lookback_bars", "confirmation_bars", "pivot_length",
    "target_r", "stop_buffer_ticks", "min_sweep_ticks",
}


def main() -> int:
    db_path = os.environ.get(
        "JUDAS_DB_PATH",
        str(Path(__file__).resolve().parents[1] / "judas_crew.db"),
    )
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, strategy_family, version, params_json
            FROM active_strategies
            WHERE state = 'active'
            """
        ).fetchall()

    targets: list[tuple[int, str, str, int, dict]] = []
    for row in rows:
        try:
            params = json.loads(row["params_json"] or "{}")
        except (TypeError, ValueError):
            params = {}
        engine = str(params.get("execution_engine", "judas_native")).lower()
        if engine != "judas_native":
            continue
        if not (_RUNTIME_KEYS & set(params.keys())):
            targets.append((
                int(row["id"]), str(row["symbol"]),
                str(row["strategy_family"]), int(row["version"]), params,
            ))

    print(f"found {len(targets)} judas_native zombies to retire")
    retired = 0
    for sid, sym, fam, ver, params in targets:
        try:
            sr.retire_strategy(
                strategy_id=sid,
                reason="zombie cleanup: judas_native params have no runtime keys "
                       "(alias-bag would fall back to default detector and cause "
                       "duplicate fires across versions)",
                metrics_snapshot={
                    "param_keys_seen": sorted(params.keys()),
                    "execution_engine": params.get("execution_engine", "judas_native"),
                },
            )
            retired += 1
            print(f"  retired id={sid} {sym} {fam} v{ver}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED id={sid}: {type(exc).__name__}: {exc}")

    print(f"retired {retired}/{len(targets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
