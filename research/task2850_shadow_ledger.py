"""Task #2850 -- SHADOW LEDGER: counterfactual P&L of the signals discarded by
the CUSHION_EXHAUSTED halt (2026-10-12 -> 2026-10-21).

READ-ONLY. Touches nothing but `signals`, `active_strategies` and the on-disk
bar cache. Produces SYNTHETIC / bar-touch-reconciled evidence -- this is a
counterfactual ranking aid, NOT live P&L and NOT broker-confirmed.

Fidelity choices are deliberately identical to the production backtest harness
(`src/research/custom_strategy_runtime.run_custom_backtest_on_bars`) so the
shadow numbers are comparable with the BT envelopes in metrics_json:

  * entry filled at the signal's own `entry` price (the price the detector
    published -- not a mid, not a bid/ask);
  * resolution starts at the FIRST bar strictly AFTER the signal timestamp
    (the harness opens on bar i and resolves from bar i+1);
  * one open trade per strategy row at a time -- a new signal while a trade is
    open is DROPPED (harness: `if open_trade is not None: continue`);
  * mid-bar ambiguity resolved ADVERSARILY: stop wins ties
    (`target_won = hit_target and not hit_stop`);
  * costs charged with `src.tools.cost_model.round_trip_cost` = the same
    v1_realistic_micros model the BT envelopes are stamped with;
  * no artificial max-hold in the primary ledger (the harness has none).

Run:  .venv/bin/python research/task2850_shadow_ledger.py
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "judas_crew.db"
CACHE = REPO / "cache_1h"

WIN = (f"{'2026-10-12T00:00:00Z'}", f"{'2026-10-21T00:00:00Z'}")
LEGAL = {"MGC", "MNQ", "MCL", "6J", "ZF"}

TF_ALIASES = {
    "5m": "5m", "5min": "5m", "5mins": "5m", "5 min": "5m", "5 mins": "5m",
    "15m": "15m", "15min": "15m", "15mins": "15m", "15 min": "15m", "15 mins": "15m",
    "1h": "1h", "1hr": "1h", "1hour": "1h", "1 hour": "1h", "60m": "1h",
    "1H": "1h", "15M": "15m", "5M": "5m",
}

# ET session buckets (October 2026 = EDT = UTC-4). Stated explicitly so the
# attribution is reproducible.
SESSION_EDGES_ET = [(3.0, "Asia"), (8.0, "London"), (12.0, "NY_AM"), (16.75, "NY_PM")]


def norm_tf(tf) -> str:
    if tf is None:
        return "1h"
    return TF_ALIASES.get(str(tf).strip(), "1h")


def session_et(ts_utc: pd.Timestamp) -> str:
    et = ts_utc.tz_convert("America/New_York")
    et_h = et.hour + et.minute / 60.0
    lab = SESSION_EDGES_ET[0][1]
    for edge, name in SESSION_EDGES_ET:
        if et_h >= edge:
            lab = name
    return lab


def load_bars(symbol: str, tf: str) -> pd.DataFrame | None:
    p = CACHE / f"{symbol}_{tf}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("ts").reset_index(drop=True)


def pick(m: dict, *names, default=None):
    for n in names:
        if isinstance(m, dict) and m.get(n) is not None:
            return m[n]
    return default


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    sig = pd.read_sql(
        "SELECT id, ts_utc, symbol, strategy_id, strategy_family, direction, "
        "entry, stop, target, risk_decision FROM signals "
        "WHERE ts_utc>=? AND ts_utc<? ORDER BY strategy_id, ts_utc",
        con, params=WIN)
    act = pd.read_sql(
        "SELECT id, symbol, strategy_family, version, state, activated_at_utc, "
        "deactivated_at_utc, params_json, metrics_json, notes FROM active_strategies",
        con)
    con.close()
    print(f"signals in window: {len(sig)}  symbols: {sorted(sig.symbol.unique())}")

    pmap = {int(r.id): json.loads(r.params_json) for r in act.itertuples()}
    mmap = {int(r.id): json.loads(r.metrics_json or "{}") for r in act.itertuples()}
    amap = {int(r.id): r for r in act.itertuples()}

    from src.portfolio_runtime import _CONTRACT_SPECS
    from src.tools.cost_model import round_trip_cost, COST_MODEL_VERSION

    dpp = {s: float(v["tick_value"]) / float(v["tick"]) for s, v in _CONTRACT_SPECS.items()}
    COST_MODEL_VERSION_: str = COST_MODEL_VERSION

    bars_cache: dict[tuple[str, str], dict] = {}

    def get_arrays(symbol: str, tf: str):
        key = (symbol, tf)
        if key not in bars_cache:
            df = load_bars(symbol, tf)
            if df is None or df.empty:
                bars_cache[key] = None
            else:
                bars_cache[key] = {
                    # NB: pandas >=2 stores tz-aware datetimes at us/ms resolution,
                    # so .astype("int64") is NOT ns. Build ns explicitly from
                    # Timestamp.value so signal-side and bar-side epochs match.
                    "ts": np.array([t.value for t in df["ts"]], dtype="int64"),
                    "high": df["high"].to_numpy(float),
                    "low": df["low"].to_numpy(float),
                    "close": df["close"].to_numpy(float),
                }
        return bars_cache[key]

    # ---------------------------------------------------------------- resolve
    rows = []
    n_no_bars = n_after_cache = n_bad_geom = n_dropped_busy = 0
    busy_until: dict[int, int] = {}   # strategy_id -> bar index trade closes on
    for r in sig.itertuples():
        sid = int(r.strategy_id)
        p = pmap.get(sid, {})
        tf = norm_tf(p.get("timeframe"))
        arr = get_arrays(r.symbol, tf)
        if arr is None:
            n_no_bars += 1
            continue
        entry, stop, target = float(r.entry), float(r.stop), float(r.target)
        direction = str(r.direction).lower()
        # geometry sanity: stop on the losing side, target on the winning side
        if direction == "long":
            ok = stop < entry < target
        else:
            ok = stop > entry > target
        if not ok or entry == stop:
            n_bad_geom += 1
            continue
        ts_i = int(pd.Timestamp(r.ts_utc).tz_convert("UTC").value)
        i = int(np.searchsorted(arr["ts"], ts_i, side="right"))  # first bar AFTER signal
        n_bars_total = len(arr["ts"])
        rec = {
            "signal_id": r.id, "strategy_id": sid, "symbol": r.symbol, "tf": tf,
            "ts_utc": r.ts_utc, "direction": direction,
            "entry": entry, "stop": stop, "target": target,
            "csid": str(pick(p, "custom_strategy_id", default="")),
            "family": p.get("strategy_family") or r.strategy_family,
            "strategy_name": p.get("strategy_name", ""),
            "slot_key": p.get("slot_key", ""),
            "exec_engine": p.get("execution_engine", ""),
        }
        if i >= n_bars_total:
            rec.update(outcome="NO_BARS_AFTER", k=-1)
            rows.append(rec)
            n_after_cache += 1
            continue
        if i <= busy_until.get(sid, -1):
            n_dropped_busy += 1
            continue
        rec["bars_to_end"] = n_bars_total - i
        hi, lo, cl = arr["high"], arr["low"], arr["close"]
        outcome, k = "OPEN", None
        for j in range(i, n_bars_total):
            if direction == "long":
                hit_stop = lo[j] <= stop
                hit_target = hi[j] >= target
            else:
                hit_stop = hi[j] >= stop
                hit_target = lo[j] <= target
            if hit_stop or hit_target:
                outcome = "LOSS" if hit_stop else "WIN"   # adverse tie-break
                k = j
                break
        risk = abs(entry - stop)
        reward = abs(target - entry)
        d_per_pt = dpp[r.symbol]
        if outcome == "WIN":
            gross = reward * d_per_pt
            pnl = gross - round_trip_cost(r.symbol, "target")
        elif outcome == "LOSS":
            gross = -risk * d_per_pt
            pnl = gross - round_trip_cost(r.symbol, "stop")
        else:
            j = n_bars_total - 1
            exit_px = float(cl[j])
            pts = (exit_px - entry) if direction == "long" else (entry - exit_px)
            gross = pts * d_per_pt
            pnl = gross - round_trip_cost(r.symbol, None)
        rec.update(outcome=outcome, k=int(k) if k is not None else -1,
                   risk_pts=risk, gross=gross, pnl=pnl,
                   r_mult=(pnl / (risk * d_per_pt)) if risk > 0 else 0.0,
                   risk_dollars=risk * d_per_pt,
                   session=session_et(pd.Timestamp(r.ts_utc)))
        rows.append(rec)
        # a still-open trade blocks every later signal for this strategy row
        busy_until[sid] = int(k) if k is not None else n_bars_total - 1

    trades = pd.DataFrame(rows)
    out = REPO / "research" / "task2850_shadow_ledger.json"
    trades.to_json(out, orient="records", date_format="iso")
    print(f"resolved rows: {len(trades)}  no_bars={n_no_bars} bad_geom={n_bad_geom} "
          f"signal_after_cache={n_after_cache} dropped_busy={n_dropped_busy}")
    print(trades.outcome.value_counts().to_string())
    print(f"wrote {out}")


if __name__ == "__main__":
    main()