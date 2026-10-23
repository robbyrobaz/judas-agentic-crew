"""Task #2860 — SHADOW_LEDGER_RANKING v2 (read-only, resumes the abandoned #2850 work).

Adds a fourth fill mode, STRICT = the first bar that OPENS at/after the signal timestamp.
That is the only mode in which no part of the fill can have happened before the signal
existed, so it is the headline number.  HONEST (open of bar i+1) is optimistic by up to
one bar because bar i+1 may already be in progress when the signal prints.

  STRICT : market fill at the OPEN of the first bar whose open >= signal ts.
  HONEST : market fill at the OPEN of the bar immediately after the last fully-closed 5m bar.
  RETRO  : fill at the signal's own `entry`, evaluated from the last closed bar
           (this is the convention run_custom_backtest uses -> findings 9121 / 9126).
  LIMIT  : the strategy's own `entry` as a resting LIMIT; fills only if a LATER bar trades
           through it; no fill = not executable and dropped.

Adverse-first tie-breaks everywhere:
  * same bar touches stop AND target  -> LOSS
  * bar OPENS beyond the stop           -> exit at the OPEN (stop-market gap)
  * bar OPENS beyond the target         -> exit at the OPEN (scratch, r<0 possible)
  * horizon 96 x 5m = 8h, then mark to the close.
"""
import sqlite3
from datetime import timezone, timedelta

import numpy as np
import pandas as pd

DB = "judas_crew.db"
WIN_A, WIN_B = "2026-10-12", "2026-10-22"   # [start, end)
SYMS = ["MGC", "MNQ", "MCL", "6J", "ZF"]
HORIZON = 96
POINT = {"MGC": 10.0, "MNQ": 2.0, "MCL": 1000.0, "6J": 12500000.0, "ZF": 1000.0}
LEGAL = set(SYMS)


def load_bars():
    out = {}
    for s in SYMS:
        df = pd.read_parquet(f"cache_1h/{s}_5m.parquet").copy()
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        out[s] = df.sort_values("ts").reset_index(drop=True)
    return out


def load_signals():
    con = sqlite3.connect(DB)
    df = pd.read_sql(
        """SELECT id, ts_utc, symbol, direction, entry, stop, target,
                  strategy_id, strategy_family, strategy_version
           FROM signals WHERE ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc""",
        con, params=(WIN_A, WIN_B))
    con.close()
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    return df


def sess_et(ts):
    h = ts.tz_convert(timezone(timedelta(hours=-4))).hour
    return "Asia" if h < 4 else "London" if h < 12 else "NY AM" if h < 16 else "NY PM"


def replay(b, start, stop, tgt, d):
    """Walk bars from `start`; return (result, exit_px, bars_held). Adverse-first."""
    n = len(b)
    hi, lo, op, cl = b["high"].values, b["low"].values, b["open"].values, b["close"].values
    for j in range(start, min(start + HORIZON, n)):
        if (lo[j] <= stop) if d == 1 else (hi[j] >= stop):     # stop first => LOSS
            gap = (op[j] < stop) if d == 1 else (op[j] > stop)
            return "LOSS", (op[j] if gap else stop), j - start + 1
        if (op[j] >= tgt) if d == 1 else (op[j] <= tgt):       # gapped through target
            return "OPEN", op[j], j - start + 1
        if (hi[j] >= tgt) if d == 1 else (lo[j] <= tgt):
            return "WIN", tgt, j - start + 1
    j = min(start + HORIZON, n) - 1
    return "OPEN", cl[j], j - start + 1


def reconcile(df, bars, mode):
    rows = []
    nofill = {s: 0 for s in SYMS}
    for s in SYMS:
        b, sub = bars[s], df[df.symbol == s]
        if not len(sub):
            continue
        ts = b["ts"].values.astype("datetime64[ns]")
        st = sub["ts"].values.astype("datetime64[ns]")
        # index of the last bar that was FULLY closed at signal time
        last_closed = np.searchsorted(ts, st - np.timedelta64(5, "m"), side="right") - 1
        # index of the first bar that OPENS at/after the signal prints
        first_open = np.searchsorted(ts, st, side="left")
        hi, lo, op = b["high"].values, b["low"].values, b["open"].values
        for k in range(len(sub)):
            r = sub.iloc[k]
            i = int(last_closed[k])
            if i < 0 or i >= len(b) - 1:
                continue
            d = 1 if r.direction == "long" else -1
            stop, tgt, ent = r.stop, r.target, r.entry
            if not all(np.isfinite([stop, tgt, ent])):
                continue
            bh = 0
            if mode == "STRICT":
                fi = int(first_open[k])
                if fi >= len(b):
                    continue
                fill = op[fi]
                res, px, bh = replay(b, fi, stop, tgt, d)
            elif mode == "HONEST":
                fi = i + 1
                fill = op[fi]
                res, px, bh = replay(b, fi, stop, tgt, d)
            elif mode == "RETRO":
                fill = ent
                res, px, bh = replay(b, i, stop, tgt, d)
            else:                                              # LIMIT
                a, z = i + 1, min(i + 1 + HORIZON, len(b))
                if a >= len(b):
                    continue
                seg_lo, seg_hi = lo[a:z], hi[a:z]
                hit = np.where((seg_lo <= ent) if d == 1 else (seg_hi >= ent))[0]
                if len(hit) == 0:
                    nofill[s] += 1
                    continue
                j = int(a + hit[0])
                fill = ent
                if (lo[j] <= stop) if d == 1 else (hi[j] >= stop):
                    gap = (op[j] < stop) if d == 1 else (op[j] > stop)
                    res, px, bh = "LOSS", (op[j] if gap else stop), 1
                elif (hi[j] >= tgt) if d == 1 else (lo[j] <= tgt):
                    res, px, bh = "WIN", tgt, 1
                else:
                    res, px, bh = replay(b, j + 1, stop, tgt, d)
                    bh += 1
            risk = abs(fill - stop)
            if risk <= 0:
                continue
            rmult = d * (px - fill) / risk
            if res == "LOSS":
                rmult = min(rmult, -1e-9)
            elif res == "WIN":
                rmult = max(rmult, 1e-9)
            rows.append(dict(signal_id=r.id, symbol=s, strategy_id=r.strategy_id,
                             strategy_family=r.strategy_family,
                             strategy_version=r.strategy_version, direction=r.direction,
                             ts_utc=r.ts_utc, fill=fill, exit_px=px, risk_pts=risk,
                             res=res, r=rmult, dollars=rmult * risk * POINT[s],
                             bars_held=bh, sess=sess_et(r.ts)))
    if mode == "LIMIT":
        for s in SYMS:
            if nofill[s]:
                print(f"  [limit-fill] {s}: {nofill[s]} signals NEVER filled at their own entry")
    return pd.DataFrame(rows)


def pf(d):
    w = d.loc[d.res == "WIN", "r"].sum()
    l = -d.loc[d.res == "LOSS", "r"].sum()
    return w / l if l > 0 else np.nan


def summarise(df):
    def f(x):
        w, l = x[x.res == "WIN"], x[x.res == "LOSS"]
        return pd.Series({"n": len(x), "W": len(w), "L": len(l),
                          "OPEN": int((x.res == "OPEN").sum()),
                          "winR": w.r.sum(), "lossR": -l.r.sum(),
                          "netR": x.r.sum(), "shadow_$": x.dollars.sum(),
                          "avg_R": x.r.mean(), "days": x.ts_utc.str[:10].nunique()})
    o = df.groupby(["strategy_id", "symbol", "strategy_family"]).apply(
        f, include_groups=False).reset_index()
    o["shadow_PF"] = o.winR / o.lossR.replace(0, np.nan)
    o["per_contract_day"] = o["shadow_$"] / o.days
    return o


if __name__ == "__main__":
    bars, sig = load_bars(), load_signals()
    print(f"signals in window: {len(sig)}   distinct strategy_id: {sig.strategy_id.nunique()}")
    bad_sym = set(sig.symbol) - LEGAL
    if bad_sym:
        print(f"  !! banned symbols present and EXCLUDED downstream: {bad_sym}")
        sig = sig[sig.symbol.isin(LEGAL)]

    # --- fillability of the signal's own `entry` against the already-closed bar
    bad = dict(total=0, inside=0, adverse=0, favourable=0)
    for s in SYMS:
        b, sub = bars[s], sig[sig.symbol == s]
        if not len(sub):
            continue
        ts = b["ts"].values.astype("datetime64[ns]")
        idx = np.searchsorted(ts, sub["ts"].values.astype("datetime64[ns]")
                             - np.timedelta64(5, "m"), side="right") - 1
        ok = idx >= 0
        idx = np.clip(idx, 0, len(b) - 1)
        hi, lo = b["high"].values[idx], b["low"].values[idx]
        e = sub["entry"].values[ok]
        d = np.where(sub["direction"].values[ok] == "long", 1, -1)
        inside = (e <= hi) & (e >= lo)
        fav = np.where(d == 1, e < lo, e > hi)
        bad["total"] += len(e)
        bad["inside"] += int(inside.sum())
        bad["favourable"] += int(fav.sum())
        bad["adverse"] += int((~inside & ~fav).sum())
    t = max(bad["total"], 1)
    print(f"\n=== FILLABILITY of signal `entry` vs the ALREADY-CLOSED bar (n={t}) ===")
    print(f"  inside the closed bar (impossible fill)         : {bad['inside']:>5} ({bad['inside']/t:.1%})")
    print(f"  beyond bar, FAVOURABLE side (must return to you): {bad['favourable']:>5} ({bad['favourable']/t:.1%})")
    print(f"  beyond bar, ADVERSE side (market-fillable now)  : {bad['adverse']:>5} ({bad['adverse']/t:.1%})")

    outs = {}
    for m in ("STRICT", "HONEST", "RETRO", "LIMIT"):
        d = reconcile(sig, bars, m)
        outs[m] = d
        d.to_parquet(f"research/task2860b_shadow_{m.lower()}.parquet")
        w, l = (d.res == "WIN").sum(), (d.res == "LOSS").sum()
        print(f"\n--- {m} --- n={len(d)}  W={w} ({w/max(len(d),1):.1%})  L={l}  "
              f"OPEN={(d.res=='OPEN').sum()}  PF={pf(d):.3f}  netR={d.r.sum():.1f}  "
              f"net$={d.dollars.sum():,.0f}")
        summarise(d).to_csv(f"research/task2860b_rank_{m.lower()}.csv", index=False)

    S = outs["STRICT"]
    s = S.groupby("sess").apply(lambda x: pd.Series({
        "n": len(x), "W": (x.res == "WIN").sum(), "L": (x.res == "LOSS").sum(),
        "PF": pf(x), "netR": x.r.sum(), "net$": x.dollars.sum()}), include_groups=False)
    s["share_of_$"] = s["net$"] / s["net$"].sum()
    print("\n=== (C) SESSION WEIGHTING (STRICT) ===")
    print(s.to_string())
    s.to_csv("research/task2860b_sessions.csv")

    st = summarise(S)
    st[st.n >= 20].sort_values("shadow_$", ascending=False).to_csv(
        "research/task2860b_rank_strict_n20.csv", index=False)
    print("\n=== (D) TOP 20 STRICT shadow_$ (n>=20) ===")
    print(st[st.n >= 20].sort_values("shadow_$", ascending=False).head(20)
          [["strategy_id", "symbol", "strategy_family", "n", "W", "L", "shadow_PF",
            "avg_R", "shadow_$", "per_contract_day"]].to_string(index=False))
    print(f"\ninsufficient sample (n<20): {int((st.n < 20).sum())} strategies / "
          f"{int(st.loc[st.n < 20, 'n'].sum())} signals")

    print("\nsymbol-level STRICT totals:")
    print(S.groupby("symbol").apply(lambda x: pd.Series({
        "n": len(x), "W": (x.res == "WIN").sum(), "L": (x.res == "LOSS").sum(),
        "PF": pf(x), "netR": x.r.sum(), "net$": x.dollars.sum()}),
        include_groups=False).to_string())