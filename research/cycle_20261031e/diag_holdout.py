"""Diagnose the HOLDOUT 'high PF, negative E[R]' artifact.

The IS-selected configs scored PF 2.8-4.9 on the untouched 2025-11..2026-06
holdout, yet E[R] was NEGATIVE and several MONTHS printed PF=999 (zero losing
trades in a month).  A real edge cannot do that.  This script dumps the
per-trade ledger so we can see WHERE the money actually came from.

Hypothesis under test: the ARCHIVE in the older region has large time GAPS
(weekends / stale segments / holidays).  A decision taken on the last bar
before a gap fills at the next bar's open, which can be many dollars away
from the stop -> a huge `risk` denominator -> a few winners with a fat
denominator inflate the DOLLAR profit factor while the R-expectancy stays
negative.  That is a data artifact, not an edge.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research/cycle_20261031a"))

from explore_prod import load_bars, sigs_sweep_disp  # noqa: E402
from coord_search import BASE  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.tools.cost_model import apply_costs_to_trade  # noqa: E402


def ledger(bars: pd.DataFrame, sigs: dict, symbol: str) -> pd.DataFrame:
    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    ts = bars["ts"]
    spec = _CONTRACT_SPECS[symbol]
    dpp = float(spec["tick_value"]) / float(spec["tick"])
    rows = []
    pending = None
    open_trade = None
    for i in range(2, len(bars)):
        if pending is not None:
            d, stop, target = pending
            open_trade = {"direction": d, "entry": float(O[i]), "stop": float(stop),
                          "target": float(target), "dec_i": pending[3]}
            pending = None
        if open_trade is not None:
            d = open_trade["direction"]
            e = open_trade["entry"]
            s = open_trade["stop"]
            t = open_trade["target"]
            res = None
            if d == "long":
                if O[i] <= s:
                    res = (O[i] - e, "stop", float(O[i]))
                elif L[i] <= s:
                    res = (s - e, "stop", s)
                elif H[i] >= t:
                    res = (t - e, "target", t)
            else:
                if O[i] >= s:
                    res = (e - O[i], "stop", float(O[i]))
                elif H[i] >= s:
                    res = (e - s, "stop", s)
                elif L[i] <= t:
                    res = (e - t, "target", t)
            if res is not None:
                pts, reason, px = res
                gross = pts * dpp
                pnl = apply_costs_to_trade(symbol=symbol, gross_pnl=gross,
                                           exit_reason=reason, qty=1)
                risk = abs(e - s) or 1e-9
                dec_close = float(bars["close"].iloc[open_trade["dec_i"]])
                rows.append({
                    "ts": ts.iloc[i], "dir": d, "dec_close": dec_close,
                    "fill": e, "stop": s, "target": t, "exit_px": px,
                    "risk_pts": risk, "pnl": pnl,
                    "R": pnl / (risk * dpp),
                    "gap_dec_to_fill_pts": e - dec_close,
                    "reason": reason,
                })
                open_trade = None
            continue
        if i in sigs:
            d, stop, target = sigs[i]
            pending = (d, stop, target, i)
    return pd.DataFrame(rows)


def main():
    symbol, tf = "MGC", "15m"
    raw = load_bars(symbol, tf, None)
    lo = pd.Timestamp("2026-06-11", tz="UTC")
    ho = raw.loc[raw["ts"] < lo].reset_index(drop=True)

    # --- bar-gap structure of the holdout region ---
    gaps = ho["ts"].diff().dt.total_seconds().div(60).dropna()
    print(f"HOLDOUT {symbol} {tf}: {len(ho)} bars  {ho['ts'].iloc[0]} -> {ho['ts'].iloc[-1]}")
    print(f"  bar-gap minutes: median={gaps.median():.1f} p90={gaps.quantile(.9):.1f} "
          f"p99={gaps.quantile(.99):.1f} max={gaps.max():.1f}")
    print(f"  gaps > 60min: {(gaps > 60).sum()}   > 240min: {(gaps > 240).sum()}")

    for sess, dk, rr, body in (((4, 10), 0.8, 1.5, 0.65), ((6, 11), 0.8, 1.5, 0.65)):
        cfg = dict(BASE)
        cfg.update(sess_lo=sess[0], sess_hi=sess[1], disp_k=dk, rr=rr,
                   body_ratio_min=body)
        sigs = sigs_sweep_disp(ho, dict(cfg, _tf=tf))
        led = ledger(ho, sigs, symbol)
        if led.empty:
            print(f"\n-- sess={sess} disp={dk} rr={rr} body={body}: no trades")
            continue
        wins = led[led["pnl"] > 0]
        loss = led[led["pnl"] <= 0]
        gw = wins["pnl"].sum()
        gl = -loss["pnl"].sum()
        print(f"\n-- sess={sess} disp={dk} rr={rr} body={body}: n={len(led)} "
              f"PF={(gw/gl if gl else 999):.3f} E[R]={led['R'].mean():+.3f} "
              f"pnl=${led['pnl'].sum():.0f}")
        print("   risk_pts (fill->stop): "
              f"min={led['risk_pts'].min():.3f} med={led['risk_pts'].median():.3f} "
              f"max={led['risk_pts'].max():.3f}")
        print("   |dec_close->fill| gap: "
              f"med={led['gap_dec_to_fill_pts'].abs().median():.3f} "
              f"max={led['gap_dec_to_fill_pts'].abs().max():.3f}")
        if not loss.empty:
            print("   LOSERS: risk_pts "
                  f"{sorted(round(x, 3) for x in loss['risk_pts'].tolist())}")
        if not wins.empty:
            print("   WINNERS: risk_pts "
                  f"{sorted(round(x, 3) for x in wins['risk_pts'].tolist())}")
            print("   WINNER pnl: "
                  f"{sorted(round(x, 1) for x in wins['pnl'].tolist())}")
        print(led[["ts", "dir", "dec_close", "fill", "stop", "target", "exit_px",
                   "risk_pts", "pnl", "R", "reason"]].to_string(index=False))


if __name__ == "__main__":
    main()
