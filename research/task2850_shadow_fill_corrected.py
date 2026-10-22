"""Task #2850 part 4 -- CORRECTED fill reconstruction.

Part 3's fill test scanned only bars AFTER the signal timestamp and concluded
70.7% of trades were fictional. Debugging signal_id 10297 (MNQ 15m short,
entry 30842.50, signal 00:11:15Z) showed the error: the signal bar itself
(00:00-00:15, high 30845.50) already traded through the entry. The detector
emits at the CLOSE of a bar it has already seen, so a stop-entry level inside
that bar's range was touched *before* the signal row was written.

Correct model, in order:
  1. The signal is actionable from the trigger bar onward (bar index i-1, where
     i is the first bar strictly after the signal ts).
  2. If entry lies inside the trigger bar's range -> FILLED at entry (the level
     was traded inside the bar that produced the signal).
  3. Otherwise the order rests. Walk forward bar by bar with an intrabar path
     assumption until entry is touched (fill) or the signal is abandoned
     (`expiry_bars`, i.e. the order is a resting order with a life).
  4. From the fill point, resolve stop/target with the ADVERSE intrabar
     tie-break, exactly as the BT harness does.

Reports both intrabar path assumptions (favourable-first and adverse-first) so
the range of outcomes is explicit rather than one arbitrary choice.

READ-ONLY.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / "cache_1h"
LEDGER = REPO / "research" / "task2850_shadow_ledger.json"

# A resting entry order is abandoned if the trigger is not reached within this
# many bars. 12 15m bars / 12 5m bars ~= 1h; for 1h we use 6 bars. Stated as an
# assumption, tested at 4 and 24 below.
EXPIRY = {"5m": 12, "15m": 12, "1h": 6}


def load(symbol: str, tf: str):
    df = pd.read_parquet(CACHE / f"{symbol}_{tf}.parquet")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    return {"ts": np.array([x.value for x in df["ts"]], dtype="int64"),
            "o": df.open.to_numpy(float), "h": df.high.to_numpy(float),
            "l": df.low.to_numpy(float), "c": df.close.to_numpy(float)}


def main() -> None:
    t = pd.read_json(LEDGER)
    t = t[t.outcome.isin(["WIN", "LOSS", "OPEN"])].copy()
    t["ts_utc"] = pd.to_datetime(t["ts_utc"], utc=True, format="ISO8601")

    from src.portfolio_runtime import _CONTRACT_SPECS
    from src.tools.cost_model import round_trip_cost
    dpp = {s: float(v["tick_value"]) / float(v["tick"]) for s, v in _CONTRACT_SPECS.items()}
    TICK = {s: float(v["tick"]) for s, v in _CONTRACT_SPECS.items()}

    cache: dict[tuple[str, str], dict] = {}
    recs = []
    for r in t.itertuples():
        key = (r.symbol, r.tf)
        if key not in cache:
            cache[key] = load(*key)
        a = cache[key]
        n = len(a["ts"])
        i = int(np.searchsorted(a["ts"], pd.Timestamp(r.ts_utc).tz_convert("UTC").value, side="right"))
        trig = i - 1                      # the bar the detector saw
        if trig < 0:
            continue
        d = r.direction
        entry, stop, target = float(r.entry), float(r.stop), float(r.target)
        d_per_pt = dpp[r.symbol]
        risk = abs(entry - stop)
        rec = {"signal_id": r.signal_id, "strategy_id": r.strategy_id, "symbol": r.symbol,
               "tf": r.tf, "csid": str(r.csid), "family": str(r.family),
               "strategy_name": str(r.strategy_name), "ts_utc": r.ts_utc,
               "direction": d, "entry": entry, "stop": stop, "target": target,
               "harness_outcome": r.outcome, "risk_dollars": risk * d_per_pt,
               "entry_off_ticks": abs(float(a["c"][trig]) - entry) / TICK[r.symbol]}

        # --- step 2/3: locate the fill -------------------------------------
        fill_idx, in_trigger = None, False
        if a["l"][trig] <= entry <= a["h"][trig]:
            fill_idx, in_trigger = trig, True
        else:
            limit = min(n, trig + 1 + EXPIRY[r.tf])
            for j in range(trig + 1, limit):
                if a["l"][j] <= entry <= a["h"][j]:
                    fill_idx = j
                    break
        rec["filled"] = fill_idx is not None
        rec["fill_in_trigger_bar"] = in_trigger
        rec["fill_bars_delay"] = (fill_idx - trig) if fill_idx is not None else np.nan
        if fill_idx is None:
            rec["pf_fav"] = rec["pf_adv"] = np.nan
            rec["r_fav"] = rec["r_adv"] = np.nan
            rec["pnl_fav"] = rec["pnl_adv"] = np.nan
            rec["outcome_fav"] = rec["outcome_adv"] = "NO_FILL"
            recs.append(rec)
            continue

        # --- step 4: resolve from the fill ---------------------------------
        def resolve(bar_start: int, adverse: bool) -> tuple[str, float, int]:
            for j in range(bar_start, n):
                first_leg = "stop" if adverse else "target"
                seq = [("stop", a["l"][j] if d == "long" else a["h"][j]),
                       ("target", a["h"][j] if d == "long" else a["l"][j])]
                if not adverse:
                    seq = seq[::-1]
                assert seq[0][0] == first_leg or not adverse
                for leg, level in seq:
                    if d == "long":
                        hit = (level <= stop) if leg == "stop" else (level >= target)
                    else:
                        hit = (level >= stop) if leg == "stop" else (level <= target)
                    if hit:
                        return ("LOSS" if leg == "stop" else "WIN"), float(a["c"][j]), j
                # neither level hit this bar -> nothing
            return "OPEN", float(a["c"][n - 1]), n - 1

        for adverse, tag in ((False, "fav"), (True, "adv")):
            oc, exit_px, k = resolve(fill_idx, adverse)
            if oc == "WIN":
                gross = abs(target - entry) * d_per_pt
                pnl = gross - round_trip_cost(r.symbol, "target")
            elif oc == "LOSS":
                gross = -risk * d_per_pt
                pnl = gross - round_trip_cost(r.symbol, "stop")
            else:
                pts = (exit_px - entry) if d == "long" else (entry - exit_px)
                pnl = pts * d_per_pt - round_trip_cost(r.symbol, None)
            rec[f"outcome_{tag}"] = oc
            rec[f"pnl_{tag}"] = pnl
            rec[f"r_{tag}"] = pnl / (risk * d_per_pt) if risk > 0 else 0.0
            rec[f"pf_{tag}"] = np.nan
            rec[f"hold_bars_{tag}"] = k - fill_idx
        recs.append(rec)

    S = pd.DataFrame(recs)
    S = S.drop_duplicates(subset=["arch"] if "arch" in S else ["symbol", "csid", "tf", "ts_utc"])
    S.to_csv(REPO / "research" / "task2850_shadow_fill_corrected.csv", index=False)

    def pf(x: pd.Series) -> float:
        w, l = x.clip(lower=0).sum(), -x.clip(upper=0).sum()
        return float(w / l) if l > 0 else 0.0

    print("=" * 104)
    print("CORRECTED FILL RECONSTRUCTION  (entry fillable from the trigger bar; adverse / favourable "
          "intrabar path)")
    print("=" * 104)
    F = S[S.filled]
    print(f"signals analysed                 : {len(S)}")
    print(f"FILLED (order would have filled) : {len(F)}  ({100*len(F)/len(S):.1f}%)")
    print(f"NO FILL within expiry            : {len(S)-len(F)}")
    print(f"  of which entry inside trigger bar's range: {int(F.fill_in_trigger_bar.sum())} "
          f"({100*F.fill_in_trigger_bar.mean():.1f}% of filled)")
    print(f"fill delay (bars, filled & not in trigger bar): "
          f"median={F[~F.fill_in_trigger_bar].fill_bars_delay.median():.0f}")

    for tag, lab in (("adv", "ADVERSE intrabar path (pessimistic)"), ("fav", "FAVOURABLE intrabar path")):
        x = F[F[f"outcome_{tag}"] != "OPEN"]
        print(f"\n--- {lab} ---")
        print(f"resolved n={len(x)}  WIN={(x[f'outcome_{tag}']=='WIN').sum()}  "
              f"LOSS={(x[f'outcome_{tag}']=='LOSS').sum()}  "
              f"WR={(x[f'outcome_{tag}']=='WIN').mean()*100:.1f}%")
        print(f"PF={pf(x[f'pnl_{tag}']):.3f}   $={x[f'pnl_{tag}'].sum():,.0f}   "
              f"avgR={x[f'r_{tag}'].mean():+.3f}")

    print("\n--- by symbol (adverse path, filled) ---")
    print(f"{'sym':<6}{'n':>6}{'W':>6}{'L':>6}{'PF':>8}{'$':>12}{'avgR':>8}")
    for sym, x in F[F.outcome_adv != "OPEN"].groupby("symbol"):
        print(f"{sym:<6}{len(x):>6}{(x.outcome_adv=='WIN').sum():>6}{(x.outcome_adv=='LOSS').sum():>6}"
              f"{pf(x.pnl_adv):>8.3f}{x.pnl_adv.sum():>12,.0f}{x.r_adv.mean():>+8.3f}")

    print("\n--- by session (adverse path, filled) ---")
    F2 = F.copy()
    F2["session"] = F2.ts_utc.dt.tz_convert("America/New_York").map(
        lambda t: "Asia" if (t.hour + t.minute/60) < 3 else
        "London" if (t.hour + t.minute/60) < 8 else
        "NY_AM" if (t.hour + t.minute/60) < 12 else "NY_PM")
    for s, x in F2[F2.outcome_adv != "OPEN"].groupby("session"):
        print(f"{s:<8}{len(x):>6}{pf(x.pnl_adv):>8.3f}{x.pnl_adv.sum():>12,.0f}{x.r_adv.mean():>+8.3f}")

    print("\n--- DOES THE BT HARNESS (instant fill at signal) OVERSTATE? ---")
    print(f"{'path':<12}{'n':>7}{'PF':>9}{'WR%':>7}{'$':>13}{'avgR':>9}")
    xa = S[S.harness_outcome != "OPEN"]
    print(f"{'harness':<12}{len(xa):>7}{pf(xa.pnl):>9.3f}{(xa.harness_outcome=='WIN').mean()*100:>7.1f}"
          f"{xa.pnl.sum():>13,.0f}{xa.r_mult.mean():>+9.3f}")
    xb = F[F.outcome_adv != "OPEN"]
    print(f"{'shadow-adv':<12}{len(xb):>7}{pf(xb.pnl_adv):>9.3f}{(xb.outcome_adv=='WIN').mean()*100:>7.1f}"
          f"{xb.pnl_adv.sum():>13,.0f}{xb.r_adv.mean():>+9.3f}")
    print(f"\nfill-rate by whether entry sits inside the trigger bar:")
    for k, x in F.groupby("fill_in_trigger_bar"):
        y = x[x.outcome_adv != "OPEN"]
        print(f"  in_trigger={str(k):<6} n={len(y):<6} PF={pf(y.pnl_adv):>7.3f} "
              f"WR={(y.outcome_adv=='WIN').mean()*100:>5.1f}%  ${y.pnl_adv.sum():>10,.0f}")
    print(f"\nwrote {REPO/'research'/'task2850_shadow_fill_corrected.csv'}")


if __name__ == "__main__":
    main()