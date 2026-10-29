"""Deep-archive integrity probe — contract rolls + synthetic bars.

Two questions the archive-readiness table does NOT answer:

Q1. ROLL STITCHING. A deep archive (92-330 days) cannot come from a single
    futures contract -- MGC/ZF/MCL roll every ~3 months. If several contracts
    are stitched into one series, the price JUMPS at each roll boundary.  A
    stitched jump is indistinguishable from a real displacement bar to any
    sweep/displacement/FVG detector, so every backtest over the deep archive
    is silently fed FAKE DISPLACEMENT EVENTS at the roll seams.

Q2. SYNTHETIC BARS. IBKR's whatToShow="TRADES" feed emits a bucket with
    volume==0 and OHLC all equal to the last trade when no trade occurred.
    Those bars are forward-filled, not traded.  ATR computed over them is
    deflated; a carried-forward high can be "swept" by a later real bar.

Output: per cell, the roll-jump census and the synthetic-bar census.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src import bar_archive  # noqa: E402

SYMS = ["MGC", "MNQ", "MCL", "ZF", "6J"]
TFS = ["5m", "15m", "1h"]

# Contract specs (symbol -> tick size / tick value) for dollar conversion.
TICK = {"MGC": 0.10, "MNQ": 0.25, "MCL": 0.01, "ZF": 1 / 32, "6J": 0.0000005}
TICKVAL = {"MGC": 10.0, "MNQ": 2.0, "MCL": 10.0, "ZF": 1000.0, "6J": 125000.0}


def atr(df: pd.DataFrame, p: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(p, min_periods=p).mean()


def main() -> None:
    out_rows = []
    for sym in SYMS:
        for tf in TFS:
            df = bar_archive.read_archive(sym, tf)
            if df.empty:
                continue
            df = df.sort_values("ts").reset_index(drop=True)
            c = df["close"].to_numpy(float)
            ts = df["ts"]

            # ---- Q1: roll-jump census on the REAL (volume>0) bar sequence ----
            # Only look at transitions where BOTH bars traded (volume>0),
            # because a synthetic bar's close is by construction the previous
            # close and would otherwise mask a real roll jump.
            vol = df["volume"].to_numpy(float)
            real = vol > 0
            prev_c = np.concatenate([[np.nan], c[:-1]])
            both_real = real & np.concatenate([[False], real[:-1]])
            jump = np.abs(c - prev_c)
            scale = max(float(np.nanmedian(np.abs(np.diff(c)))), 1e-12)
            big = both_real & (jump > 8.0 * scale) & (jump > 0)

            # also: absolute dollar jump size for context
            dpp = TICKVAL[sym] / TICK[sym]
            big_dollars = [round(float(jump[i]) * dpp, 2) for i in np.flatnonzero(big)]

            # ---- Q2: synthetic census ----
            synth = (~real) & (df["high"].to_numpy(float) <= df["low"].to_numpy(float))
            n_synth = int(synth.sum())

            # ---- ATR distortion: ATR over raw series vs ATR over real-only series ----
            a_raw = atr(df)
            a_real = atr(df[real].reset_index(drop=True))
            atr_raw_med = float(a_raw.dropna().median()) if a_raw.notna().any() else float("nan")
            atr_real_med = float(a_real.dropna().median()) if a_real.notna().any() else float("nan")

            # ---- price monotonicity: does the series drift out of a sane band? ----
            rec = {
                "cell": f"{sym}_{tf}",
                "n": int(len(df)),
                "first": str(ts.iloc[0]), "last": str(ts.iloc[-1]),
                "n_real": int(real.sum()),
                "n_synth": n_synth,
                "synth_pct": round(100.0 * n_synth / len(df), 1),
                "n_roll_jumps": int(big.sum()),
                "roll_jump_dollars": big_dollars[:12],
                "max_jump_dollars": round(float(np.nanmax(jump) * dpp), 2) if np.isfinite(jump).any() else None,
                "atr_raw_median": round(atr_raw_med, 6),
                "atr_realonly_median": round(atr_real_med, 6),
                "atr_inflation_pct": (round(100.0 * (atr_raw_med / atr_real_med - 1), 1)
                                      if atr_real_med and np.isfinite(atr_real_med) and atr_real_med > 0 else None),
                "close_min": float(c.min()), "close_max": float(c.max()),
            }
            out_rows.append(rec)
            print(f"{rec['cell']:<9} n={rec['n']:<6} real={rec['n_real']:<6} synth={rec['n_synth']:<6}"
                  f"({rec['synth_pct']}%) rollJumps={rec['n_roll_jumps']:<4} "
                  f"maxJump=${rec['max_jump_dollars']} atrInfl={rec['atr_inflation_pct']}% "
                  f"close[{rec['close_min']}..{rec['close_max']}]", flush=True)

            if rec["n_roll_jumps"]:
                idx = np.flatnonzero(big)[:8]
                print("      roll-jump timestamps: " + ", ".join(
                    f"{ts.iloc[i]} (${round(float(jump[i])*dpp,2)})" for i in idx))

    Path("outputs/archive_integrity_probe_20261029.json").write_text(
        json.dumps(out_rows, indent=2, default=str))


if __name__ == "__main__":
    main()
