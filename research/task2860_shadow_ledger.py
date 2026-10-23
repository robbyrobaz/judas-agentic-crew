"""Task #2860 — SHADOW_LEDGER_RANKING v3 (read-only, corrected).

Three fill models on the SAME signals / SAME stops / SAME targets, so the delta
isolates the fill assumption and nothing else:

  HONEST  : market fill at the OPEN of the first bar that opens after the signal's
            last fully-closed 5m bar.
  RETRO   : fill at the signal's own `entry` price, evaluated from the last closed bar.
            This is the convention `run_custom_backtest` uses -> findings 9121/9126.
  LIMIT   : the strategy's own `entry` treated as a resting LIMIT order.  Fills only if
            a bar AFTER the signal bar trades through it.  No fill = not executable.

Adverse-first tie-breaks everywhere:
  * same-bar stop AND target            -> LOSS
  * bar OPENS beyond the stop            -> exit at the OPEN (stop-market gap)
  * bar OPENS beyond the target          -> exit at the OPEN (scratch, r ~ 0)
  * horizon 96 bars (8h) then mark to the close.
"""
import sqlite3
from datetime import timezone, timedelta
import pandas as pd
import numpy as np

DB = "judas_crew.db"
WIN_A, WIN_B = "2026-10-12", "2026-10-22"
SYMS = ["MGC", "MNQ", "MCL", "6J", "ZF"]
HORIZON = 96
POINT = {"MGC": 10.0, "MNQ": 2.0, "MCL": 1000.0, "6J": 12500000.0, "ZF": 1000.0}


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


def replay(b, start, fill, stop, tgt, d):
    n = len(b)
    hi, lo, op = b["high"].values, b["low"].values, b["open"].values
    for j in range(start, min(start + HORIZON, n)):
        stop_hit = (lo[j] <= stop) if d == 1 else (hi[j] >= stop)
        if stop_hit:
            gap = (op[j] < stop) if d == 1 else (op[j] > stop)
            return "LOSS", (op[j] if gap else stop), j - start + 1
        if (op[j] >= tgt) if d == 1 else (op[j] <= tgt):
            return "OPEN", op[j], j - start + 1
        if (hi[j] >= tgt) if d == 1 else (lo[j] <= tgt):
            return "WIN", tgt, j - start + 1
    j = min(start + HORIZON, n) - 1
    return "OPEN", b["close"].values[j], j - start + 1


def reconcile(df, bars, mode):
    rows, nofill = [], {}
    for s in SYMS:
        b, sub = bars[s], df[df.symbol == s]
        ts = b["ts"].values.astype("datetime64[ns]")
        st = sub["ts"].values.astype("datetime64[ns]")
        idx = np.searchsorted(ts, st - np.timedelta64(5, "m"), side="right") - 1
        hi, lo, op = b["high"].values, b["low"].values, b["open"].values
        nofill[s] = 0
        for k, (_, r) in enumerate(sub.iterrows()):
            i = idx[k]
            if i < 0:
                continue
            d = 1 if r.direction == "long" else -1
            stop, tgt, ent = r.stop, r.target, r.entry
            if not all(np.isfinite([stop, tgt, ent])):
                continue
            if mode == "HONEST":
                fi = i + 1
                if fi >= len(b):
                    continue
                fill = op[fi]
                res, px, bh = replay(b, fi, fill, stop, tgt, d)
            elif mode == "RETRO":
                fill = ent
                res, px, bh = replay(b, i, fill, stop, tgt, d)
            else:                                     # LIMIT
                a, z = i + 1, min(i + 1 + HORIZON, len(b))
                if a >= len(b):
                    continue
                hit = np.where(((lo[a:z] <= ent) if d == 1 else (hi[a:z] >= ent)))[0]
                if len(hit) == 0:
                    nofill[s] += 1
                    continue
                j = a + hit[0]
                fill = ent
                if (lo[j] <= stop) if d == 1 else (hi[j] >= stop):
                    gap = (op[j] < stop) if d == 1 else (op[j] > stop)
                    res, px, bh = "LOSS", (op[j] if gap else stop), 1
                elif (hi[j] >= tgt) if d == 1 else (lo[j] <= tgt):
                    res, px, bh = "WIN", tgt, 1
                else:
                    res, px, bh = replay(b, j + 1, fill, stop, tgt, d)
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
            print(f"  [limit-fill] {s}: {nofill[s]} signals NEVER filled at their own entry")
    return pd.DataFrame(rows)


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
    print(f"signals {len(sig)}  dup(strategy_id,ts)={int(sig.duplicated(['strategy_id','ts_utc']).sum())}")

    bad = dict(total=0, inside=0, adverse=0, favourable=0)
    for s in SYMS:
        b, sub = bars[s], sig[sig.symbol == s]
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
    t = bad["total"]
    print(f"\n=== FILLABILITY of signal `entry` vs the ALREADY-CLOSED bar (n={t}) ===")
    print(f"  inside the closed bar  (impossible fill)          : {bad['inside']:>5} ({bad['inside']/t:.1%})")
    print(f"  beyond bar, FAVOURABLE side (must return to you)  : {bad['favourable']:>5} ({bad['favourable']/t:.1%})")
    print(f"  beyond bar, ADVERSE side   (market-fillable now)  : {bad['adverse']:>5} ({bad['adverse']/t:.1%})")

    outs = {}
    for m in ("HONEST", "RETRO", "LIMIT"):
        d = reconcile(sig, bars, m)
        outs[m] = d
        d.to_parquet(f"research/task2860_shadow_{m.lower()}.parquet")
        w, l = (d.res == "WIN").sum(), (d.res == "LOSS").sum()
        gw, gl = d.loc[d.res == "WIN", "r"].sum(), -d.loc[d.res == "LOSS", "r"].sum()
        print(f"\n--- {m} --- n={len(d)}  W={w} ({w/len(d):.1%})  L={l}  "
              f"OPEN={(d.res=='OPEN').sum()}  PF={gw/gl:.3f}  netR={d.r.sum():.1f}  "
              f"net$={d.dollars.sum():,.0f}")
        summarise(d).to_csv(f"research/task2860_rank_{m.lower()}.csv", index=False)

    H = outs["HONEST"]
    s = H.groupby("sess").apply(lambda x: pd.Series({
        "n": len(x), "W": (x.res == "WIN").sum(), "L": (x.res == "LOSS").sum(),
        "PF": x.loc[x.res == "WIN", "r"].sum() / -x.loc[x.res == "LOSS", "r"].sum(),
        "netR": x.r.sum(), "net$": x.dollars.sum()}), include_groups=False)
    s["share"] = s["net$"] / s["net$"].sum()
    print("\n=== (C) SESSION WEIGHTING (HONEST) ===")
    print(s.to_string())

    hon = summarise(H)
    hon[hon.n >= 20].sort_values("shadow_$", ascending=False).to_csv(
        "research/task2860_rank_honest_n20.csv", index=False)
    print("\n=== (D) TOP 20 HONEST shadow_$ (n>=20) ===")
    print(hon[hon.n >= 20].sort_values("shadow_$", ascending=False).head(20)
          [["strategy_id", "symbol", "strategy_family", "n", "W", "L", "shadow_PF",
            "avg_R", "shadow_$", "per_contract_day"]].to_string(index=False))
    print(f"\ninsufficient sample (n<20): {int((hon.n<20).sum())} strategies / "
          f"{int(hon.loc[hon.n<20,'n'].sum())} signals")
    print("\nsymbol-level HONEST totals:")
    print(H.groupby("symbol").apply(lambda x: pd.Series({
        "n": len(x), "W": (x.res=='WIN').sum(), "L": (x.res=='LOSS').sum(),
        "PF": x.loc[x.res=='WIN','r'].sum()/-x.loc[x.res=='LOSS','r'].sum(),
        "net$": x.dollars.sum()}), include_groups=False).to_string())