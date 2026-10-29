"""A/B: backtest metrics with vs without IBKR filler bars in the series.

The filler fix (src/research/custom_strategy_runtime._drop_filler_bars) changes
what the backtest replays.  This script quantifies the change on REAL strategy
code from the custom_strategies table, over the REAL merged bar series.

For each (strategy, symbol, tf):
    RAW  = archive (as stored)  + live cache, filler INCLUDED   (pre-fix)
    CLEAN= archive (filler dropped) + live cache                 (post-fix)

and reports PF / n / E[R] / median ATR for both, plus the ATR ratio
(clean/raw) -- the number that says how badly the old series understated the
bar the market actually produced.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src import bar_archive, bar_cache  # noqa: E402
from src.config import load_config  # noqa: E402
from src.research import custom_strategy_runtime as csr  # noqa: E402

DB = ROOT / "judas_crew.db"
OUT = ROOT / "outputs" / f"filler_ab_{os.environ.get('DAYS','30')}_{os.environ.get('TAG','x')}.json"

import os, json as _json
_PAIRS_ALL = [
    (414, "MGC", "5m"), (414, "MNQ", "5m"), (415, "MCL", "5m"),
    (408, "ZF", "5m"), (423, "6J", "5m"),
    (419, "MCL", "15m"), (418, "MNQ", "15m"),
]
_sel = os.environ.get("PAIRS")
PAIRS = ([tuple(int(x) if x.isdigit() else x for x in p.split(":")) for p in _sel.split(",")]
         if _sel else _PAIRS_ALL)
DAYS = int(os.environ.get("DAYS", "30"))


def load_code(csid: int) -> str:
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute("SELECT code FROM custom_strategies WHERE id=?", (csid,)).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row else ""


def atr_med(df: pd.DataFrame, p: int = 14) -> float:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    a = tr.rolling(p, min_periods=p).mean().dropna()
    return float(a.median()) if len(a) else float("nan")


def merged_series(symbol: str, tf: str, cfg, drop_filler: bool, days: int = DAYS):
    arch = bar_archive.read_archive(symbol, tf)
    try:
        live = bar_cache.get_bars(symbol, host=cfg.ibkr.host, port=cfg.ibkr.port,
                                  client_id=int(cfg.ibkr.data_client_id) + 9, timeframe=tf)
    except Exception:  # noqa: BLE001
        live = None
    if arch.empty:
        return live
    if drop_filler:
        arch, _ = csr._drop_filler_bars(arch)
    if live is None or len(live) == 0:
        m = arch.reset_index(drop=True)
    else:
        m = pd.concat([arch, live], ignore_index=True)
        m = m.drop_duplicates(subset=["ts"], keep="last").sort_values("ts").reset_index(drop=True)
    per_day = {"5m": 288, "15m": 96, "1h": 24}.get(tf, 24)
    if days and len(m) > per_day * days:
        m = m.tail(per_day * days).reset_index(drop=True)
    return m


def main() -> None:
    cfg = load_config()
    rows = []
    for csid, sym, tf in PAIRS:
        code = load_code(csid)
        if not code:
            print(f"csid {csid} not found")
            continue
        raw = merged_series(sym, tf, cfg, drop_filler=False)
        cln = merged_series(sym, tf, cfg, drop_filler=True)
        if raw is None or cln is None or len(raw) < 50 or len(cln) < 50:
            print(f"{sym} {tf}: insufficient bars")
            continue
        r = csr.run_custom_backtest_on_bars(code=code, bars=raw, params={}, symbol=sym, timeout_s=90)
        c = csr.run_custom_backtest_on_bars(code=code, bars=cln, params={}, symbol=sym, timeout_s=90)
        a_raw, a_cln = atr_med(raw), atr_med(cln)
        rec = {
            "csid": csid, "symbol": sym, "tf": tf,
            "raw_n_bars": int(len(raw)), "clean_n_bars": int(len(cln)),
            "raw_filler_pct": round(100.0 * float(((raw["volume"] <= 0) & (raw["high"] <= raw["low"])).sum()) / len(raw), 1),
            "raw_pf": round(float(r.get("pf", 0)), 3), "raw_n": int(r.get("n_trades", 0)),
            "raw_er": round(float(r.get("expectancy_r", 0)), 4),
            "raw_pnl": round(float(r.get("total_pnl", 0)), 2),
            "clean_pf": round(float(c.get("pf", 0)), 3), "clean_n": int(c.get("n_trades", 0)),
            "clean_er": round(float(c.get("expectancy_r", 0)), 4),
            "clean_pnl": round(float(c.get("total_pnl", 0)), 2),
            "atr_raw": round(a_raw, 6) if a_raw == a_raw else None,
            "atr_clean": round(a_cln, 6) if a_cln == a_cln else None,
            "atr_ratio_clean_over_raw": (round(a_cln / a_raw, 3) if a_raw and a_raw == a_raw and a_raw != 0 else None),
        }
        rows.append(rec)
        print(f"csid{csid} {sym} {tf:<4} bars {rec['raw_n_bars']}->{rec['clean_n_bars']} "
              f"(filler {rec['raw_filler_pct']}%)  "
              f"RAW pf={rec['raw_pf']:<7} n={rec['raw_n']:<4} E[R]={rec['raw_er']:<7} "
              f"| CLEAN pf={rec['clean_pf']:<7} n={rec['clean_n']:<4} E[R]={rec['clean_er']:<7} "
              f"| ATR x{rec['atr_ratio_clean_over_raw']}", flush=True)

    OUT.write_text(json.dumps(rows, indent=2, default=str))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
