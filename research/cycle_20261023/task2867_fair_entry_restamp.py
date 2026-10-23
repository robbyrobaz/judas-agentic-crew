"""TASK_2867_FAIR_ENTRY_RESTAMP -- what csid 256 (#4599) and 270 (#4678) are
worth when the entry is re-anchored to the decision bar instead of the stale
bar the strategy names.

THE RULE (from finding 07f3a4c2, applied here)
-----------------------------------------------
`evaluate(bars)` may return an `entry` taken from a bar that has already
closed. The backtest fills at that price. The LIVE path does not: it places a
MARKET order and gets the market price.

So the honest test of the same rule is:

    signal emitted on bar i
      RETRO  entry = sig["entry"]                      (what the BT does)
      FAIR   entry = open of bar i+1                   (what we would get live)

    stop/target keep their INTENDED DISTANCES in both cases (that is what
    place_bracket does -- it re-anchors by distance to the real fill), so the
    only thing that changes is where the trade starts.

Simulation: bars after the decision bar, intrabar order = stop-first when both
levels are touched in the same bar. Costs: v1_realistic_micros (commission +
half-spread each way), same as the promotion stamp.

READ-ONLY.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research.custom_strategy_runtime import _compile_and_load  # noqa: E402

CACHE = Path("cache_1h")
SPEC = {"MGC": (0.10, 1.0), "MNQ": (0.25, 0.5), "MCL": (0.01, 1.0),
        "ZF": (0.0078125, 7.8125), "6J": (0.0000005, 6.25)}
FILE = {"5m": "{s}_5m.parquet", "15m": "{s}_15m.parquet", "1h": "{s}_1h.parquet"}
ALT = {"5m": ["{s}_5m.parquet"], "15m": ["{s}_15m.parquet"],
       "1h": ["{s}_1h.parquet", "{s}_15m.parquet"]}
# v1_realistic_micros: $2.00 round-turn commission + 1 tick of slippage per side.
COMMISSION_RT = 2.00

TARGETS = [
    # active_id, symbol, csid, timeframe
    (4599, "MNQ", 256, "15m"),
    (4678, "MNQ", 270, "5m"),
    # controls: the two CLEAN actives
    (4975, "MNQ", 424, "15m"),
    (4977, "ZF", 424, "15m"),
]


def load(sym: str, tf: str) -> pd.DataFrame:
    for pat in ALT.get(tf, [FILE[tf]]):
        p = CACHE / pat.format(s=sym)
        if p.exists():
            d = pd.read_parquet(p)
            d["ts"] = pd.to_datetime(d["ts"], utc=True)
            return d.sort_values("ts").reset_index(drop=True)
    return pd.DataFrame()


def params_for(conn, aid: int) -> dict:
    r = conn.execute("SELECT params_json FROM active_strategies WHERE id=?", (aid,)).fetchone()
    p = json.loads((r[0] if r else "{}") or "{}")
    for k in ("slot_key", "architecture", "strategy_name", "symbol",
              "strategy_family", "execution_engine", "custom_strategy_id",
              "cost_model", "session_start_utc", "session_end_utc"):
        p.pop(k, None)
    return p


def run(code, bars, params, symbol, fair: bool, step: int, max_trades: int):
    """Bar-by-bar replay. RETRO fills at sig['entry']; FAIR fills at next open."""
    fn, _ = _compile_and_load(code)
    tick, tval = SPEC[symbol]
    dpp = tval / tick
    o, h, l = (bars["open"].values.astype(float), bars["high"].values.astype(float),
               bars["low"].values.astype(float))
    n = len(bars)
    trades = []
    i = 320
    while i < n - 2 and len(trades) < max_trades:
        try:
            sig = fn(bars.iloc[: i + 1].copy(), dict(params))
        except Exception:  # noqa: BLE001
            sig = None
        if not (isinstance(sig, dict) and "entry" in sig and sig.get("direction")):
            i += step
            continue
        d = str(sig["direction"]).lower()
        if fair:
            k = i + 1
            entry = float(o[k])
        else:
            k = i + 1
            entry = float(sig["entry"])
        sig_stop = float(sig["stop"]); sig_tgt = float(sig["target"])
        s_dist = abs(float(sig["entry"]) - sig_stop)
        t_dist = abs(sig_tgt - float(sig["entry"]))
        if s_dist <= 0:
            i += step
            continue
        stop = entry - s_dist if d == "long" else entry + s_dist
        targ = entry + t_dist if d == "long" else entry - t_dist
        cost = COMMISSION_RT + 2 * tick * dpp          # commission + 1 tick/side
        r = 1.0 / d_dist(d) if False else s_dist
        exit_px, reason = None, None
        for j in range(k + 1, min(n, k + 400)):
            if d == "long":
                hit_s, hit_t = l[j] <= stop, h[j] >= targ
            else:
                hit_s, hit_t = h[j] >= stop, l[j] <= targ
            if hit_s and hit_t:
                reason = "stop"                        # pessimistic: stop first
                exit_px = stop
                break
            if hit_s:
                reason, exit_px = "stop", stop
                break
            if hit_t:
                reason, exit_px = "target", targ
                break
        if exit_px is None:
            i += step
            continue
        pnl = ((exit_px - entry) if d == "long" else (entry - exit_px)) * dpp - cost
        trades.append({"pnl": pnl, "R": pnl / (s_dist * dpp), "reason": reason,
                       "dir": d, "entry": entry, "dec_i": i, "fill_k": k,
                       "slip_ticks": (entry - float(sig["entry"])) / tick * (1 if d == "long" else -1)})
        i = (j + 1) if exit_px is not None else i + step
    return trades


def d_dist(d):  # pragma: no cover - placeholder removed at runtime
    return 1.0


def summarise(name, tr, span_days):
    if not tr:
        print(f"{name:<28} n=0   (no trades)")
        return
    pnl = np.array([t["pnl"] for t in tr])
    w = int((pnl > 0).sum()); l = len(pnl) - w
    pf = pnl[pnl > 0].sum() / abs(pnl[pnl <= 0].sum()) if (pnl <= 0).any() else float("inf")
    eq = np.cumsum(pnl)
    dd = float((eq - np.maximum.accumulate(np.r_[0.0, eq])).min())
    print(f"{name:<28} n={len(pnl):>4} {w:>3}W/{l:<3}L WR={100.0*w/len(pnl):>5.1f}% "
          f"PF={pf:>6.3f} E[R]={pnl.sum()/max(len(pnl),1):>6.2f} ${pnl.sum():>+10.2f} "
          f"maxDD=${dd:>9.2f} span={span_days:>4.1f}d "
          f"med_slip_t={np.median([abs(t['slip_ticks']) for t in tr]):>6.1f}")


def main() -> None:
    conn = sqlite3.connect("file:judas_crew.db?mode=ro", uri=True)
    codes = {r[0]: r[1] for r in conn.execute("SELECT id, code FROM custom_strategies")}
    print("=" * 110)
    print("FAIR-ENTRY RESTAMP -- csid 256 (#4599) and 270 (#4678) + CLEAN control csid 424")
    print("cost_model=v1_realistic_micros ($2.00 RT commission + 1 tick/side)")
    print("=" * 110)
    for aid, sym, csid, tf in TARGETS:
        code = codes.get(csid)
        bars = load(sym, tf)
        if not code or bars.empty:
            print(f"#{aid} csid={csid} {sym} {tf}: SKIP (code or bars missing)")
            continue
        params = params_for(conn, aid)
        span = (bars["ts"].iloc[-1] - bars["ts"].iloc[0]).total_seconds() / 86400.0
        print(f"\n--- #{aid}  {sym} {tf}  csid {csid}  "
              f"bars {bars.ts.min().date()} -> {bars.ts.max().date()} ({span:.0f}d)")
        step = 3 if tf == "5m" else 1
        for fair, label in ((False, "RETRO (BT as run)"), (True, "FAIR  (next-bar open)")):
            tr = run(code, bars, params, sym, fair, step, 4000)
            summarise(f"  {label}", tr, span)
    conn.close()


if __name__ == "__main__":
    main()