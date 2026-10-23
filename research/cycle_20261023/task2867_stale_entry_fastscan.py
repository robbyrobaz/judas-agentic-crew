"""TASK_2867_FAST_STALE_SCAN -- the (A) deliverable, made to actually finish.

WHY THE PREVIOUS SCAN TIMED OUT
--------------------------------
`research/stale_fill_scan_20261022.py` replays `evaluate()` bar-by-bar over the
whole archive for every active strategy. That is O(n_bars) per strategy --
minutes each, 175 strategies, never finishes.

THE FIX
-------
The staleness of a signal is a property of ONE representative evaluation, not
of the whole history. Call `evaluate(bars)` once on the most recent real bars
and compare the returned `ts` with the last bar's `ts`:

    age_bars = (last_bar_ts - signal_ts) / bar_size

age 0  -> decision-time clean.
age >0 -> the returned entry is taken from a bar that had ALREADY CLOSED when
          the signal was emitted. That is the retro-fill idiom, measured in O(1).

If the strategy does not fire on the last slice, walk BACK in bar-sized slices
until it does (max 60 tries), so we measure a real signal rather than
"no signal". Bars come from the live cache_1h parquet files, which carry
5m/15m/1h for all legal symbols.

READ-ONLY.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research.custom_strategy_runtime import _compile_and_load  # noqa: E402

LEGAL = {"MGC", "MNQ", "MCL", "ZF", "6J"}
CACHE = Path("cache_1h")
BAR_MIN = {"5m": 5.0, "15m": 15.0, "1h": 60.0}
CACHE_FILE = {"5m": "{s}_5m.parquet", "15m": "{s}_15m.parquet", "1h": "{s}_1h.parquet"}
CACHE_ALT = {"5m": ["{s}_5m.parquet", "{s}_15m.parquet"], "15m": ["{s}_15m.parquet", "{s}_5m.parquet"],
             "1h": ["{s}_1h.parquet", "{s}_15m.parquet"]}
DROP = {"slot_key", "architecture", "strategy_name", "symbol", "strategy_family",
        "execution_engine", "custom_strategy_id", "cost_model", "session_start_utc",
        "session_end_utc", "timeframe", "enabled", "qty", "side_filter"}

_bars: dict[tuple[str, str], pd.DataFrame] = {}


def bars_for(sym: str, tf: str) -> pd.DataFrame:
    key = (sym, tf)
    if key in _bars:
        return _bars[key]
    df = pd.DataFrame()
    for pat in CACHE_ALT.get(tf, [CACHE_FILE[tf]]):
        p = CACHE / pat.format(s=sym)
        if p.exists():
            df = pd.read_parquet(p)
            break
    if not df.empty:
        df = df.sort_values("ts").reset_index(drop=True)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    _bars[key] = df
    return df


def probe(code: str, sym: str, tf: str, params: dict, wide: bool = False):
    """One representative evaluation, walked back until the strategy fires."""
    fn, _ = _compile_and_load(code)
    if fn is None:
        return {"err": "compile_failed"}
    df = bars_for(sym, tf)
    if df.empty:
        return {"err": f"no_cache_{tf}"}
    # Walk back over STRIDED slices so the search reaches ~150 days instead of
    # the ~7 hours a 1-bar step covers. Stride 11 is coprime with the common
    # bar/day divisors, so successive probes land on unrelated sessions.
    n = len(df)
    ends = list(range(n - 1, 320, -1))[:80]
    if wide:
        ends = list(range(n - 1, 320, -11))
    for end in ends:
        sub = df.iloc[:end]
        try:
            sig = fn(sub.copy(), dict(params))
        except Exception as exc:  # noqa: BLE001
            return {"err": f"eval_exc:{type(exc).__name__}"}
        if isinstance(sig, dict) and "entry" in sig and "ts" in sig:
            last_ts = pd.to_datetime(sub["ts"].iloc[-1], utc=True)
            try:
                sig_ts = pd.to_datetime(sig["ts"], utc=True)
            except Exception:  # noqa: BLE001
                sig_ts = pd.NaT
            if pd.isna(sig_ts):
                return {"err": "unparsable_ts", "back": back}
            age_min = (last_ts - sig_ts).total_seconds() / 60.0
            return {
                "age_min": round(float(age_min), 1),
                "age_bars": round(float(age_min) / BAR_MIN[tf], 2),
                "lookback_bars": n - end,
                "entry": float(sig["entry"]) if sig.get("entry") is not None else None,
                "dir": sig.get("direction"),
            }
    return {"err": "no_signal_found"}


WIDE_SET: set | None = None


def main() -> None:
    global WIDE_SET
    conn = sqlite3.connect("file:judas_crew.db?mode=ro", uri=True)
    acts = conn.execute(
        "SELECT id, symbol, strategy_family, version, params_json "
        "FROM active_strategies WHERE state='active' ORDER BY id"
    ).fetchall()
    codes = {r[0]: r[1] for r in
             conn.execute("SELECT id, code FROM custom_strategies").fetchall()}
    conn.close()

    WIDE_SET = {int(x) for x in sys.argv[1:]} if len(sys.argv) > 1 else set()
    if WIDE_SET:
        print(f"[wide re-probe requested for {len(WIDE_SET)} ids]")
    print(f"active strategies: {len(acts)}")
    print(f"symbols with a live bar cache: "
          f"{sorted({a[1] for a in acts if not bars_for(a[1], '5m').empty})}")
    print()
    print("=" * 96)
    print("STALE-ENTRY SCAN  -- age of the entry price at the moment it is returned")
    print("=" * 96)
    print(f"{'id':>5} {'sym':>4} {'tf':>4} {'csid':>5} {'family':<34} "
          f"{'age_min':>8} {'age_bars':>9} {'verdict'}")
    print("-" * 96)

    out = []
    t0 = time.time()
    for aid, sym, fam, ver, pj in acts:
        p = json.loads(pj or "{}")
        tf = str(p.get("timeframe", "1h")).lower().replace(" ", "")
        csid = p.get("custom_strategy_id")
        params = {k: v for k, v in p.items() if k not in DROP}
        code = codes.get(int(csid)) if csid else None
        if not code:
            out.append({"id": aid, "sym": sym, "tf": tf, "csid": csid,
                        "family": fam, "verdict": "NO_CODE"})
            print(f"{aid:>5} {sym:>4} {tf:>4} {str(csid):>5} {fam[:34]:<34} "
                  f"{'-':>8} {'-':>9} NO_CODE")
            continue
        r = probe(code, sym, tf, params, wide=(WIDE_SET is not None and aid in WIDE_SET))
        if "err" in r:
            out.append({"id": aid, "sym": sym, "tf": tf, "csid": csid,
                        "family": fam, "verdict": r["err"]})
            print(f"{aid:>5} {sym:>4} {tf:>4} {str(csid):>5} {fam[:34]:<34} "
                  f"{'-':>8} {'-':>9} {r['err']}")
            continue
        ab = r["age_bars"]
        verdict = "CLEAN" if ab <= 0.01 else ("STALE" if ab >= 1.0 else "MILD")
        out.append({"id": aid, "sym": sym, "tf": tf, "csid": int(csid),
                    "family": fam, "age_min": r["age_min"], "age_bars": ab,
                    "lookback_bars": r.get("lookback_bars"),
                    "verdict": verdict})
        print(f"{aid:>5} {sym:>4} {tf:>4} {int(csid):>5} {fam[:34]:<34} "
              f"{r['age_min']:>8.1f} {ab:>9.2f} {verdict}")

    print("-" * 96)
    print(f"elapsed {time.time()-t0:.1f}s")
    df = pd.DataFrame(out)
    print()
    print("=" * 96)
    print("SUMMARY")
    print("=" * 96)
    print(df.verdict.value_counts().to_string())
    print()
    clean = df[df.verdict == "CLEAN"]
    stale = df[df.verdict == "STALE"]
    print(f"CLEAN (age 0 bars)     : {len(clean):>4} strategies, "
          f"{sorted(set(clean.csid.astype(int)))[:20]}")
    print(f"STALE (age >= 1 bar)   : {len(stale):>4} strategies, "
          f"{sorted(set(stale.csid.astype(int)))[:20]}")
    if len(stale):
        print()
        print("STALE detail, worst first:")
        s = stale.sort_values("age_bars", ascending=False)
        print(f"{'id':>5} {'sym':>4} {'tf':>4} {'csid':>5} {'age_bars':>9} "
              f"{'age_min':>8}  family")
        for _, r in s.iterrows():
            print(f"{r.id:>5} {r.sym:>4} {r.tf:>4} {int(r.csid):>5} "
                  f"{r.age_bars:>9.1f} {r.age_min:>8.0f}  {r.family[:38]}")
    print()
    print("age_bars >= 1 means: at the instant `evaluate()` returned, the entry")
    print("price came from a bar that had ALREADY CLOSED. `run_custom_backtest`")
    print("fills at that price. The live path does not (MARKET order) -- but the")
    print("signal itself is structurally stale, so its BT envelope is not a")
    print("prediction of what the same rule earns live.")
    df.to_csv("outputs/task2867_stale_entry_fastscan_20261023.csv", index=False)
    print("\nwrote outputs/task2867_stale_entry_fastscan_20261023.csv")


if __name__ == "__main__":
    main()