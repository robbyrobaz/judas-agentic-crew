#!/usr/bin/env python3
"""Refill sweep harness -- 2026-10-25 cycle.

Calls EXACTLY the same function the sanctioned `run_custom_backtest` tool calls
(src/research/custom_strategy_runtime.run_custom_backtest), so every metric it
prints is byte-identical to what the tool returns: dollar-denominated P&L,
cost_model=v1_realistic_micros, n_trades, PF, E[R], max_dd, plus the
stale-fill diagnostics (stale_fill_n / backtest_fill_valid).

Usage:
    .venv/bin/python research/refill_sweep_20261025.py combos.json out.json

combos.json = [[{"csid": 203} | {"file": "path.py"}, "SYMBOL", "15m",
                {params...}, 120], ...]
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.research import custom_strategy_runtime as csr  # noqa: E402

DB = ROOT / "judas_crew.db"

KEEP = (
    "n_trades", "n_signals", "n_wins", "n_losses", "total_pnl", "pf",
    "profit_factor", "expectancy_r", "max_drawdown", "cost_model", "symbol",
    "qty", "dollars_per_point", "n_bars", "bars_span_days", "first_bar",
    "last_bar", "stale_fill_n", "stale_fill_pct", "stale_measure_fail_n",
    "backtest_fill_valid", "error",
)


def code_for(spec: dict) -> str | None:
    if "file" in spec:
        p = ROOT / spec["file"]
        return p.read_text() if p.exists() else None
    csid = int(spec["csid"])
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            "SELECT code FROM custom_strategies WHERE id = ?", (csid,)
        ).fetchone()
    return row[0] if row else None


def main() -> int:
    combos = json.loads(Path(sys.argv[1]).read_text())
    out_path = Path(sys.argv[2])
    results = []
    for spec, symbol, tf, params, days in combos:
        label = spec.get("name") or f"csid{spec.get('csid')}"
        code = code_for(spec)
        if not code:
            results.append({"label": label, "symbol": symbol, "tf": tf,
                            "error": "no code"})
            continue
        t0 = time.time()
        m = csr.run_custom_backtest(
            code=code, symbol=symbol.upper(), days=int(days),
            db_path=str(DB), timeframe=tf, params=dict(params or {}), qty=1,
        )
        row = {k: m.get(k) for k in KEEP}
        row.update({
            "label": label, "symbol": symbol.upper(), "tf": tf,
            "params": params or {}, "elapsed_s": round(time.time() - t0, 1),
            "csid": spec.get("csid"),
        })
        results.append(row)
        print(json.dumps({k: row.get(k) for k in (
            "label", "symbol", "tf", "n_trades", "pf", "expectancy_r",
            "total_pnl", "max_drawdown", "bars_span_days", "stale_fill_n",
            "backtest_fill_valid", "error", "elapsed_s")}), flush=True)
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nwrote {out_path} ({len(results)} runs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
