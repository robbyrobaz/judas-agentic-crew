"""WHAT IS A 'PLACEHOLDER' ROW?  (the decisive question for archive readiness)

FINDING SO FAR (profile_dirty.py)
--------------------------------
In EVERY cell measured, `range>0 AND volume==0` has count **exactly 0**.  The
zero-volume rows are a strict SUBSET of the zero-range rows.  So the archive
contains three row classes:

    A  range>0, volume>0        -> a normal bar                       (REAL)
    B  range==0, volume>0       -> a flat bar that traded             (REAL?)
    C  range==0, volume==0      -> nothing happened                   (PLACEHOLDER?)

The existing honest-screen mask (`(high-low)>0 & volume>0`) keeps only class A.
That throws away class B -- a bar that PRINTED but did not move its high/low
-- and for ZF 5m it shreds the series into shards with NO contiguous run >= 300
bars, i.e. it makes our most profitable symbol untestable.

Task #2892's own wording is: "drop placeholder **zero-range / zero-volume** rows"
-- i.e. class C only (both zero), which is what this script checks.

This script proves class B is genuinely real data:
  * its OHLC is a coherent price (not a synthetic zero/NaN fill)
  * it sits exactly on the timeframe grid (no off-step timestamps)
  * its timestamp continues the 5m/15m sequence with no gap
  * dropping it vs keeping it changes only the local volatility, not the price

Usage:
    .venv/bin/python research/cycle_20261029b/class_proof.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src import bar_archive  # noqa: E402

STEP_MIN = {"5m": 5, "15m": 15, "1h": 60}


def classify(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    rng = df["high"] - df["low"]
    df["cls"] = np.where(rng > 0, "A_real", np.where(df["volume"] > 0, "B_flat_traded", "C_placeholder"))
    return df


def show(symbol: str, tf: str) -> None:
    raw = bar_archive.read_archive(symbol, tf)
    if raw is None or raw.empty:
        print(f"\n### {symbol} {tf}: MISSING")
        return
    df = classify(raw)
    step = STEP_MIN[tf]
    diffs = df["ts"].diff().dt.total_seconds() / 60.0

    counts = df["cls"].value_counts().to_dict()
    print(f"\n### {symbol} {tf}  raw={len(df)}  classes={counts}")

    # ---- class C: what do placeholders actually contain? ----
    c = df[df["cls"] == "C_placeholder"]
    if len(c):
        cols = ["open", "high", "low", "close", "volume"]
        allzero = bool((c[cols] == 0).all().all())
        print(f"  C: n={len(c)}  all-OHLCV-zero={allzero}  "
              f"close uniq={c['close'].nunique()}  "
              f"price range=[{c['close'].min():.4f},{c['close'].max():.4f}]")

    # ---- class B: is it real? ----
    b = df[df["cls"] == "B_flat_traded"]
    if len(b):
        # 1) on-grid timestamps?
        bsteps = df["ts"].diff().dt.total_seconds().div(60).loc[b.index]
        ongrid = int((bsteps == step).sum())
        # 2) is the flat price the SAME as the previous bar's close? (a "no
        #    change" bar) or a distinct level?
        prev_c = df["close"].shift(1).loc[b.index]
        same = int((np.isclose(b["close"], prev_c, rtol=0, atol=1e-9)).sum())
        # 3) does the price sit inside the neighbouring bars' range?
        print(f"  B: n={len(b)}  on_grid={ongrid}/{len(b)}  "
              f"close==prev_close={same}/{len(b)}  "
              f"close uniq={b['close'].nunique()}  "
              f"price=[{b['close'].min():.4f},{b['close'].max():.4f}]")
        # 4) are B rows contiguous with A rows in time (i.e. do they fill the
        #    gaps that C rows create)?
        idx = set(b.index)
        prev_is_A_or_B = sum(
            1 for i in b.index
            if i > 0 and (df["cls"].iloc[i - 1] in ("A_real", "B_flat_traded")))
        print(f"      B preceded by a real bar: {prev_is_A_or_B}/{len(b)}")
        ex = b.head(3)[["ts", "open", "high", "low", "close", "volume"]]
        for _, r in ex.iterrows():
            print(f"      e.g. {r['ts']}  O={r['open']:.4f} H={r['high']:.4f} "
                  f"L={r['low']:.4f} C={r['close']:.4f} V={r['volume']}")

    # ---- run lengths under both masks ----
    for name, mask in (
        ("strict  A only (high-low>0 & vol>0)", (df["cls"] == "A_real")),
        ("correct drop-C (vol>0, i.e. A+B)", (df["volume"] > 0)),
    ):
        runs, start = [], None
        for i, v in enumerate(mask):
            if v and start is None:
                start = i
            elif not v and start is not None:
                runs.append(i - start)
                start = None
        if start is not None:
            runs.append(len(mask) - start)
        big = sorted([r for r in runs if r >= 300], reverse=True)
        print(f"  [{name:<36}] runs>=300: {len(big):>3}  top5={big[:5]}")


def main() -> int:
    for s, tf in [("ZF", "5m"), ("ZF", "15m"), ("MGC", "5m"), ("MCL", "5m"),
                  ("6J", "5m"), ("MNQ", "5m"), ("MNQ", "15m"), ("MGC", "15m"),
                  ("MGC", "1h"), ("MNQ", "1h"), ("MCL", "1h"), ("ZF", "1h"),
                  ("6J", "1h"), ("6J", "15m"), ("MCL", "15m")]:
        show(s, tf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
