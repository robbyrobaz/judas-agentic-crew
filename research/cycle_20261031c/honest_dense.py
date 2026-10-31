"""HONEST DENSE-WINDOW HARNESS (cycle 20261031c).

Why this file exists
--------------------
1. The deep archive's OLD region is mostly IBKR filler (no price data):
   MNQ 15m keeps ~1-27 real bars/day before 2026-05 and 72-78/day only from
   2026-09 (finding: research/cycle_20261031c/filler_density.csv).  A 15m
   strategy evaluated on that series sees a SPARSE SAMPLE of the tape: its
   "sweep within the last 6 bars" spans hours.  The prior cycle's harness
   (research/cycle_20261031a/explore_prod.py) loaded the FULL archive, so its
   numbers were computed on a bar sequence the live scanner never sees.
2. Dropping filler turns "no data" into a hole, so the production fill
   ("open of the next row") can be 45+ minutes late -- 4.7% of MGC 15m
   decisions and 12.7% of MGC 5m decisions (measured 20261031c).

This harness therefore:
  * restricts every test to the DENSE era of the series (auto-detected),
  * resolves production fills ONLY when the fill row is the next time bucket
    (gap <= 1.5 x bar interval); later fills are counted and DROPPED,
  * splits chronologically (train 60% / test 40%) and reports both,
  * never promotes: it only produces evidence.

Usage:
  .venv/bin/python research/cycle_20261031c/honest_dense.py            # grid all
  .venv/bin/python research/cycle_20261031c/honest_dense.py MGC 15m    # one
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261031a"))

from src import bar_archive  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.tools.cost_model import apply_costs_to_trade  # noqa: E402
from explore_prod import sigs_sweep_disp  # noqa: E402

INTERVAL_MIN = {"5m": 5, "15m": 15, "1h": 60}
LEGAL = ["MGC", "MNQ", "MCL", "ZF", "6J"]


# ------------------------------------------------------------------ data
def load_dense(symbol: str, tf: str, verbose: bool = False):
    """Archive -> filler dropped -> DENSE ERA only.

    Dense era = the longest suffix of the series whose trailing 20-day median
    daily bar count is >= 70% of the series' 90th-percentile daily count.
    """
    df = bar_archive.read_archive(symbol, tf)
    if df is None or len(df) == 0:
        return None, {}
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").drop_duplicates(subset=["ts"], keep="last")
    df = df.reset_index(drop=True)
    rng = df["high"] - df["low"]
    filler = (df["volume"] <= 0) & (rng <= 0)
    df = df.loc[~filler].reset_index(drop=True)
    if len(df) < 500:
        return None, {}

    per_day = df.groupby(df["ts"].dt.date).size()
    full = float(per_day.quantile(0.90))
    roll = per_day.rolling(20, min_periods=5).median()
    ok = roll >= 0.70 * full
    if not ok.any():
        return None, {}
    last_bad = roll.index[~ok]
    start = (last_bad.max() + pd.Timedelta(days=1)) if len(last_bad) else per_day.index.min()
    start = pd.Timestamp(start)
    dense = df.loc[df["ts"].dt.date >= start.date()].reset_index(drop=True)

    ts = dense["ts"]
    d = ts.diff().dt.total_seconds().div(60).dropna()
    iv = INTERVAL_MIN[tf]
    meta = {
        "sym": symbol, "tf": tf, "n_bars": len(dense),
        "start": str(ts.iloc[0]), "end": str(ts.iloc[-1]),
        "days": int(dense["ts"].dt.date.nunique()),
        "bars_per_day": round(len(dense) / max(dense["ts"].dt.date.nunique(), 1), 1),
        "dt_median": round(float(d.median()), 1),
        "frac_gap_gt_1p5iv": round(float((d > 1.5 * iv).mean()), 4),
        "dense_start": str(start.date()),
        "full_bars_per_day_p90": round(full, 1),
    }
    if verbose:
        print(meta)
    return dense, meta


# --------------------------------------------------------------- simulator
def simulate(bars: pd.DataFrame, sigs: dict, symbol: str, qty: int = 1,
             tf: str = "15m", max_gap_iv: float = 1.5) -> dict:
    """PRODUCTION fill at the open of the next row, GAP-GUARDED.

    A fill whose row is more than `max_gap_iv` bar intervals after the
    decision is NOT an achievable live fill (the live scanner sees the dense
    tape; the archive does not).  Such trades are counted and dropped.
    """
    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    TS = bars["ts"]
    spec = _CONTRACT_SPECS[symbol]
    dpp = float(spec["tick_value"]) / float(spec["tick"])
    iv = INTERVAL_MIN[tf]
    max_gap_s = max_gap_iv * iv * 60.0
    n = len(bars)
    trades = []
    gap_dropped = 0
    open_trade = None
    pending = None
    for i in range(2, n):
        if pending is not None:
            d, stop, target, dec = pending
            gap = float((TS.iloc[i] - TS.iloc[dec]).total_seconds())
            if gap > max_gap_s:
                gap_dropped += 1
                pending = None
            else:
                open_trade = {"direction": d, "entry": float(O[i]), "stop": float(stop),
                              "target": float(target), "dec": dec, "fill": i}
                pending = None
        if open_trade is not None:
            d = open_trade["direction"]
            e, s, t = open_trade["entry"], open_trade["stop"], open_trade["target"]
            res = None
            if d == "long":
                if O[i] <= s:
                    res = (O[i] - e, "stop")
                elif L[i] <= s:
                    res = (s - e, "stop")
                elif H[i] >= t:
                    res = (t - e, "target")
            else:
                if O[i] >= s:
                    res = (e - O[i], "stop")
                elif H[i] >= s:
                    res = (e - s, "stop")
                elif L[i] <= t:
                    res = (e - t, "target")
            if res is not None:
                pts, reason = res
                gross = pts * dpp * qty
                pnl = apply_costs_to_trade(symbol=symbol, gross_pnl=gross,
                                           exit_reason=reason, qty=qty)
                risk = abs(e - s) or 1e-9
                trades.append({"pnl": float(pnl), "r": float(pnl / (risk * dpp * qty)),
                               "date": str(TS.iloc[open_trade["dec"]].date()),
                               "hour": int(TS.iloc[open_trade["dec"]].hour)})
                open_trade = None
            continue
        if i in sigs:
            pending = (*sigs[i], i)
    pnls = [t["pnl"] for t in trades]
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    pf = float(gw / gl) if gl > 0 else (0.0 if gw == 0 else 999.0)
    return {"n": len(pnls), "pf": round(pf, 4), "pnl": round(float(sum(pnls)), 2),
            "er": round(float(np.mean([t["r"] for t in trades])), 4) if trades else 0.0,
            "wr": round(100.0 * sum(1 for p in pnls if p > 0) / len(pnls), 1) if pnls else 0.0,
            "gap_dropped": gap_dropped, "trades": trades}


def split_sim(bars, sigs, symbol, tf, frac=0.6):
    n = len(bars)
    cut = int(n * frac)
    tr = simulate(bars.iloc[:cut].reset_index(drop=True),
                  {i: v for i, v in sigs.items() if i < cut}, symbol, tf=tf)
    te = simulate(bars.iloc[cut:].reset_index(drop=True),
                  {i - cut: v for i, v in sigs.items() if i >= cut}, symbol, tf=tf)
    return tr, te


# -------------------------------------------------------------------- grid
BASE = {"lookback": 12, "min_sweep_atr": 0.30, "disp_k": 1.0, "rr": 1.5,
        "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
        "atr_period": 14, "sweep_ref": "rolling"}


def grid_for(symbol: str, tf: str, cells: list[dict]) -> list[dict]:
    bars, meta = load_dense(symbol, tf)
    if bars is None:
        print(f"{symbol} {tf}: no dense data")
        return []
    print(f"\n### {symbol} {tf} dense: {meta['n_bars']} bars {meta['start'][:10]} -> "
          f"{meta['end'][:10]} ({meta['bars_per_day']}/day, "
          f"dt_med={meta['dt_median']}m, hole={meta['frac_gap_gt_1p5iv']:.3f})")
    rows = []
    for cell in cells:
        p = dict(BASE, _tf=tf, **cell)
        sigs = sigs_sweep_disp(bars, p)
        tr, te = split_sim(bars, sigs, symbol, tf)
        row = {"sym": symbol, "tf": tf, **{f"p_{k}": v for k, v in cell.items()},
               "tr_n": tr["n"], "tr_pf": tr["pf"], "tr_er": tr["er"],
               "te_n": te["n"], "te_pf": te["pf"], "te_er": te["er"],
               "te_pnl": te["pnl"], "gap_drop": tr["gap_dropped"] + te["gap_dropped"],
               "te_wr": te["wr"], "n_sigs": len(sigs)}
        rows.append(row)
        print(f"  {str(cell):<66} tr n={tr['n']:>3} pf={tr['pf']:>6} | "
              f"te n={te['n']:>3} pf={te['pf']:>6} er={te['er']:>7} pnl=${te['pnl']:>9.2f} "
              f"gapdrop={row['gap_drop']}")
    return rows


CELLS = [
    {"lookback": 12, "min_sweep_atr": 0.30, "rr": 1.5, "body_ratio_min": 0.55},
    {"lookback": 20, "min_sweep_atr": 0.30, "rr": 1.5, "body_ratio_min": 0.55},
    {"lookback": 30, "min_sweep_atr": 0.45, "rr": 1.5, "body_ratio_min": 0.55},
    {"lookback": 12, "min_sweep_atr": 0.30, "rr": 1.0, "body_ratio_min": 0.55},
    {"lookback": 12, "min_sweep_atr": 0.30, "rr": 2.0, "body_ratio_min": 0.55},
    {"lookback": 20, "min_sweep_atr": 0.45, "rr": 1.5, "body_ratio_min": 0.55},
    {"lookback": 20, "min_sweep_atr": 0.30, "rr": 1.5, "body_ratio_min": 0.65},
    {"lookback": 12, "min_sweep_atr": 0.45, "rr": 1.5, "body_ratio_min": 0.65},
    {"lookback": 12, "min_sweep_atr": 0.30, "rr": 1.5, "body_ratio_min": 0.55,
     "sweep_ref": "pdh_pdl"},
    {"lookback": 12, "min_sweep_atr": 0.30, "rr": 1.5, "body_ratio_min": 0.55,
     "sweep_ref": "asian"},
]


def main() -> None:
    args = sys.argv[1:]
    syms = LEGAL
    tfs = ["15m", "5m"]
    if args:
        syms = [args[0].upper()]
        if len(args) > 1:
            tfs = [args[1]]
    rows = []
    for sym in syms:
        for tf in tfs:
            rows.extend(grid_for(sym, tf, CELLS))
    if rows:
        out = pd.DataFrame(rows)
        out.to_csv("research/cycle_20261031c/grid_dense.csv", index=False)
        print("\n===== RANKED BY TEST PF (n>=10) =====")
        sub = out[out.te_n >= 10].sort_values("te_pf", ascending=False)
        print(sub.head(25).to_string(index=False))


if __name__ == "__main__":
    main()
