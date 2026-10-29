"""MEASURE: does replaying IBKR placeholder bars change the promotion-gate PF?

The custom backtest engine (`src/research/custom_strategy_runtime.py`) replays
whatever bars it is handed.  `_fetch_synthetic_or_live_bars` hands it the deep
archive UNION the live cache -- and the archive contains large stretches of
IBKR *placeholder* rows (high==low AND volume==0) that IBKR emits to fill time
ranges in which the requested contract did not trade (e.g. before its listing,
or across a contract roll).

The honest research screen (finding 9153) runs per CLEAN SEGMENT with those
rows dropped.  The promotion-gate tool does NOT.  So the gate's PF and the
honest screen's PF can disagree on the SAME code.

This script runs the SAME strategy code through the SAME engine on:
  ARM A -- bars exactly as the gate tool builds them (placeholder rows kept)
  ARM B -- identical, with placeholder rows dropped
and reports the delta.

Usage:
  .venv/bin/python research/task2892_placeholder_impact.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import bar_archive, bar_cache  # noqa: E402
from src.config import load_config  # noqa: E402
from src.research import custom_strategy_runtime as csr  # noqa: E402

DB = ROOT / "judas_crew.db"


def load_code(csid: int) -> tuple[str, dict]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT code, backtest_metrics_json FROM custom_strategies WHERE id=?",
            (csid,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"csid {csid} not found")
    try:
        params = json.loads(row["backtest_metrics_json"] or "{}")
    except Exception:
        params = {}
    return row["code"], params


def gate_bars(symbol: str, tf: str, days: int, use_live: bool = True):
    """Exactly what `_fetch_synthetic_or_live_bars` builds."""
    live = None
    if use_live:
        cfg = load_config()
        ib = cfg.ibkr
        try:
            live = bar_cache.get_bars(symbol, host=ib.host, port=ib.port,
                                      client_id=int(ib.data_client_id) + 9,
                                      timeframe=tf)
        except Exception as exc:  # noqa: BLE001
            print(f"  [live cache unavailable: {type(exc).__name__}: {exc}]")
    arch = bar_archive.read_archive(symbol, tf)
    if live is None or len(live) == 0:
        merged = arch.reset_index(drop=True)
    else:
        merged = pd.concat([arch, live], ignore_index=True)
        merged = merged.drop_duplicates(subset=["ts"], keep="last")
        merged = merged.sort_values("ts").reset_index(drop=True)
    per_day = csr._BARS_PER_DAY.get(tf, 24)
    if days and len(merged) > per_day * days:
        merged = merged.tail(per_day * days).reset_index(drop=True)
    return merged


def is_placeholder(df: pd.DataFrame) -> pd.Series:
    return ((df["high"] - df["low"]) <= 0) & (df["volume"] <= 0)


def run_arm(code, bars, symbol, params, qty=1, timeout_s=60):
    return csr.run_custom_backtest_on_bars(
        code=code, bars=bars, params=params, symbol=symbol, qty=qty,
        timeout_s=timeout_s)


CASES = [
    # (csid, symbol, tf, days)
    (398, "MGC", "1h", 180),     # buffet_zoo RSI 1h regime filtered
    (398, "MNQ", "1h", 180),
    (398, "MCL", "1h", 180),
    (398, "ZF", "1h", 180),
    (398, "6J", "1h", 180),
    (441, "MGC", "5m", 90),      # sweep cont market entry hygiene (candidate 7383)
    (270, "ZF", "5m", 90),       # silver bullet 5m nogate ZF
    (348, "MGC", "5m", 90),      # silver bullet 5m nogate roll24
    (357, "MGC", "5m", 90),      # ict inside bar disp breakout 5m
    (354, "MCL", "15m", 120),    # silver bullet 15m 50pct
]


def main() -> None:
    out = []
    for csid, sym, tf, days in CASES:
        try:
            code, params = load_code(csid)
        except SystemExit as exc:
            print(exc)
            continue
        bars = gate_bars(sym, tf, days)
        if bars is None or len(bars) < 50:
            print(f"{sym} {tf} csid={csid}: NO BARS")
            continue
        ph = is_placeholder(bars)
        n_ph = int(ph.sum())
        clean = bars.loc[~ph].reset_index(drop=True)

        a = run_arm(code, bars, sym, params)
        b = run_arm(code, clean, sym, params)

        rec = {
            "csid": csid, "symbol": sym, "tf": tf, "days": days,
            "bars_gate": int(len(bars)), "placeholder_rows": n_ph,
            "placeholder_pct": round(100.0 * n_ph / len(bars), 1),
            "bars_clean": int(len(clean)),
            "A_raw": {k: a.get(k) for k in (
                "n_trades", "pf", "total_pnl", "expectancy_r", "max_drawdown",
                "stale_fill_n", "stale_measure_fail_n", "backtest_fill_valid",
                "bars_span_days", "first_bar", "last_bar", "error")},
            "B_clean": {k: b.get(k) for k in (
                "n_trades", "pf", "total_pnl", "expectancy_r", "max_drawdown",
                "stale_fill_n", "stale_measure_fail_n", "backtest_fill_valid",
                "bars_span_days", "first_bar", "last_bar", "error")},
        }
        out.append(rec)
        print(f"\n=== csid={csid} {sym} {tf} days={days} "
              f"gate_bars={len(bars)} placeholders={n_ph} ({rec['placeholder_pct']}%) ===")
        for tag, m in (("A_raw  ", a), ("B_clean", b)):
            print(f"  {tag} n={m.get('n_trades'):>4} pf={m.get('pf')} "
                  f"${m.get('total_pnl'):,.2f} E[R]={m.get('expectancy_r'):.4f} "
                  f"stale={m.get('stale_fill_n')} valid={m.get('backtest_fill_valid')} "
                  f"span={m.get('bars_span_days')}d err={m.get('error')}")
        dpf = (b.get("pf") or 0) - (a.get("pf") or 0)
        print(f"  DELTA pf: {dpf:+.3f}   delta n: {(b.get('n_trades') or 0) - (a.get('n_trades') or 0)}"
              f"   delta $: {(b.get('total_pnl') or 0) - (a.get('total_pnl') or 0):,.2f}")

    Path("outputs/task2892_placeholder_impact.json").write_text(json.dumps(out, indent=2))
    print("\nwrote outputs/task2892_placeholder_impact.json")


if __name__ == "__main__":
    main()
