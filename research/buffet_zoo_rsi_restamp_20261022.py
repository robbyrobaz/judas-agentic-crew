"""BUFFET_ZOO_RSI_RESTAMP_2026-10-22 -- task #2864.

Re-measure the EXACT #4308 (MNQ) / #3969 (MGC) `buffet_zoo` RSI configuration on
the REAL deep archive (`src/bar_archive.py`) instead of the cache window.

WHY THIS IS A DIFFERENT EVIDENCE CLASS FROM THE CUSTOM-ENGINE ROWS
------------------------------------------------------------------
Finding 9121 (RETRO_FILL_DEFECT) shows the custom engine can be handed an
`entry` priced off an already-closed bar, and that every PF>=2 custom envelope in
this book is a fill artifact. `buffet_zoo` is NOT that engine: the live native
evaluator in `src/portfolio_runtime.py::_evaluate_rsi` fills at

    entry = float(bars["close"].iloc[-1])

i.e. the close of the LAST bar in the slice it was handed -- a decision-time
fill. The `_rsi` / `_atr` helpers below are copied VERBATIM from that function
(including the `rolling(14).mean()` ATR, which differs from the Wilder `ewm` ATR
in `research/rsi_zoo_strategy.py` -- using that file would NOT have been a
restamp). So this script isolates the only variable across the A/B: the WINDOW.

READ-ONLY: opens the DB with `sqlite3.connect(..., mode=ro)`. No writes.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src import bar_archive  # noqa: E402
from src.research.custom_strategy_runtime import run_custom_backtest_on_bars  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION  # noqa: E402

DB = "judas_crew.db"
OUT = "outputs/buffet_zoo_rsi_restamp_20261022.json"

# Cache-equivalent window: `bar_cache._TF_SPECS` asks IBKR for 1h="60 D", so
# every 1h promote was made on at most 60 days. Reproduced honestly below.
CACHE_EQ_BARS = {"5m": 2730, "15m": 20 * 96, "1h": 60 * 24}

# The two configurations, transcribed from `active_strategies.params_json`.
# THEY ARE NOT IDENTICAL: MNQ ran target_r=1.5, MGC ran target_r=1.0.
CFGS = {
    "4308": {"strategy_type": "rsi", "period": 10, "lo_thr": 30, "hi_thr": 70,
             "target_r": 1.5, "stop_atr_mult": 1.0, "timeframe": "1H",
             "execution_engine": "buffet_zoo"},
    "3969": {"strategy_type": "rsi", "period": 10, "lo_thr": 30, "hi_thr": 70,
             "target_r": 1.0, "stop_atr_mult": 1.0, "timeframe": "1H",
             "execution_engine": "buffet_zoo"},
}


# --------------------------------------------------------------------------
# Faithful re-implementation of src/portfolio_runtime.py::_evaluate_rsi
# --------------------------------------------------------------------------
def _rsi(c: pd.Series, p: int = 14) -> pd.Series:
    d = c.diff()
    u = d.clip(lower=0).ewm(alpha=1 / p, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / p, adjust=False).mean()
    return 100 - 100 / (1 + u / dn.replace(0, pd.NA))


def _atr(bars: pd.DataFrame, p: int = 14) -> pd.Series:
    h, l, c = bars["high"], bars["low"], bars["close"]
    tr = pd.concat([(h - l), (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(p).mean()


def evaluate(bars: pd.DataFrame, params: dict) -> dict | None:
    """Byte-faithful port of the live `_evaluate_rsi`. Entry is the close of the
    LAST bar -- never a retroactively-closed bar (9121's rule)."""
    period = int(params.get("period", 14))
    lo = float(params.get("lo_thr", 30))
    hi = float(params.get("hi_thr", 70))
    target_r = float(params.get("target_r", 2.0))
    sa = float(params.get("stop_atr_mult", 1.5))
    if len(bars) < period + 30:
        return None
    rsi = _rsi(bars["close"], period)
    a = _atr(bars).iloc[-1]
    if pd.isna(a) or a <= 0:
        return None
    last, prev = rsi.iloc[-1], rsi.iloc[-2]
    if pd.isna(last) or pd.isna(prev):
        return None
    if last > lo and prev <= lo:
        direction = "long"
    elif last < hi and prev >= hi:
        direction = "short"
    else:
        return None
    entry = float(bars["close"].iloc[-1])
    stop_dist = sa * float(a)
    stop = entry - stop_dist if direction == "long" else entry + stop_dist
    target = entry + target_r * stop_dist if direction == "long" else entry - target_r * stop_dist
    return {"direction": direction, "entry": entry, "stop": stop, "target": target}


CODE = evaluate.__doc__ and ""  # placeholder, real code injected below
CODE = (
    "import numpy as np\n"
    "import pandas as pd\n\n"
    "def _rsi(c, p=14):\n"
    "    d = c.diff()\n"
    "    u = d.clip(lower=0).ewm(alpha=1 / p, adjust=False).mean()\n"
    "    dn = (-d.clip(upper=0)).ewm(alpha=1 / p, adjust=False).mean()\n"
    "    return 100 - 100 / (1 + u / dn.replace(0, pd.NA))\n\n"
    "def _atr(bars, p=14):\n"
    "    h, l, c = bars['high'], bars['low'], bars['close']\n"
    "    tr = pd.concat([(h - l), (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)\n"
    "    return tr.rolling(p).mean()\n\n"
    "def evaluate(bars, params):\n"
    "    period = int(params.get('period', 14))\n"
    "    lo = float(params.get('lo_thr', 30))\n"
    "    hi = float(params.get('hi_thr', 70))\n"
    "    target_r = float(params.get('target_r', 2.0))\n"
    "    sa = float(params.get('stop_atr_mult', 1.5))\n"
    "    if len(bars) < period + 30:\n"
    "        return None\n"
    "    rsi = _rsi(bars['close'], period)\n"
    "    a = _atr(bars).iloc[-1]\n"
    "    if pd.isna(a) or a <= 0:\n"
    "        return None\n"
    "    last, prev = rsi.iloc[-1], rsi.iloc[-2]\n"
    "    if pd.isna(last) or pd.isna(prev):\n"
    "        return None\n"
    "    if last > lo and prev <= lo:\n"
    "        direction = 'long'\n"
    "    elif last < hi and prev >= hi:\n"
    "        direction = 'short'\n"
    "    else:\n"
    "        return None\n"
    "    entry = float(bars['close'].iloc[-1])\n"
    "    stop_dist = sa * float(a)\n"
    "    stop = entry - stop_dist if direction == 'long' else entry + stop_dist\n"
    "    target = entry + target_r * stop_dist if direction == 'long' else entry - target_r * stop_dist\n"
    "    return {'direction': direction, 'entry': entry, 'stop': stop, 'target': target}\n"
)


def measure(bars, params, symbol, qty=1, timeout_s=900):
    t0 = time.time()
    res = run_custom_backtest_on_bars(code=CODE, bars=bars, params=params,
                                      symbol=symbol, qty=qty, timeout_s=timeout_s)
    res["wall_s"] = round(time.time() - t0, 1)
    return res


def slim(res: dict) -> dict:
    n = int(res.get("n_trades", 0) or 0)
    w = int(res.get("n_wins", 0) or 0)
    l = int(res.get("n_losses", 0) or 0)
    pf = float(res.get("pf", 0.0) or 0.0)
    return {
        "n": n, "W": w, "L": l,
        "pf": round(pf, 3),
        "wr_pct": round(100.0 * w / n, 1) if n else 0.0,
        "e_r": round(float(res.get("expectancy_r", 0.0) or 0.0), 3),
        "total_usd": round(float(res.get("total_pnl", 0.0) or 0.0), 2),
        "usd_per_trade": round(float(res.get("total_pnl", 0.0) or 0.0) / n, 2) if n else 0.0,
        "max_dd_usd": round(float(res.get("max_drawdown", 0.0) or 0.0), 2),
        "span_days": res.get("bars_span_days"),
        "n_bars": res.get("n_bars"),
        "first_bar": str(res.get("first_bar")),
        "last_bar": str(res.get("last_bar")),
        "cost_model": res.get("cost_model", COST_MODEL_VERSION),
        "error": res.get("error"),
        "wall_s": res.get("wall_s"),
        # joint BT-LURE (finding 6511): PF>3 AND WR>70% together
        "bt_lure": bool(pf > 3.0 and n and 100.0 * w / n > 70.0),
    }


def main():
    out = {"cost_model_version": COST_MODEL_VERSION, "rows": []}

    # (label, symbol, tf, cfg_key, is_the_real_config)
    targets = [
        ("4308", "MNQ", "1h", "4308", True),
        ("3969", "MGC", "1h", "3969", True),
        # supplementary: the SAME configs on the fast TFs that DO have an archive.
        # Explicitly NOT a restamp of the 1H configuration.
        ("4308-sup", "MNQ", "5m", "4308", False),
        ("3969-sup", "MGC", "5m", "3969", False),
        ("3969-sup15", "MGC", "15m", "3969", False),
    ]

    for label, sym, tf, cfg_key, is_real in targets:
        params = {k: v for k, v in CFGS[cfg_key].items()
                  if k not in ("timeframe", "execution_engine", "symbol")}
        rec = {"label": label, "symbol": sym, "timeframe": tf,
               "params": params, "is_the_registered_1h_config": is_real}

        bars_all = bar_archive.read_archive(sym, tf)
        if bars_all is None or bars_all.empty:
            rec["archive_status"] = "MISSING"
            rec["not_restampable"] = True
            rec["verdict"] = "NOT-RESTAMPABLE (no archive; IBKR unreachable from research sandbox)"
            out["rows"].append(rec)
            print(f"[{label}] {sym} {tf}: ARCHIVE MISSING -- NOT-RESTAMPABLE", flush=True)
            continue

        ts = pd.to_datetime(bars_all["ts"])
        span = (ts.iloc[-1] - ts.iloc[0]).days
        rec["archive_bars"] = int(len(bars_all))
        rec["archive_span_days"] = int(span)
        rec["archive_first"] = str(ts.iloc[0])
        rec["archive_last"] = str(ts.iloc[-1])
        if span < 60:
            rec["not_restampable"] = True
            rec["verdict"] = f"NOT-RESTAMPABLE (THIN {span}d < 60d)"

        cap = CACHE_EQ_BARS.get(tf, 2730)
        tail = bars_all.tail(cap).reset_index(drop=True)
        rec["cache_eq_bars_used"] = int(len(tail))
        rec["CACHE_EQ"] = slim(measure(tail, params, sym))
        print(f"[{label}] {sym} {tf} CACHE-EQ -> {rec['CACHE_EQ']}", flush=True)

        rec["ARCHIVE"] = slim(measure(bars_all.reset_index(drop=True), params, sym))
        print(f"[{label}] {sym} {tf} ARCHIVE  -> {rec['ARCHIVE']}", flush=True)

        cpf, apf = rec["CACHE_EQ"]["pf"], rec["ARCHIVE"]["pf"]
        rec["span_inflation_pf"] = round(cpf / apf, 3) if apf else None
        cu, au = rec["CACHE_EQ"]["usd_per_trade"], rec["ARCHIVE"]["usd_per_trade"]
        rec["span_inflation_usd_per_trade"] = round(cu / au, 3) if au else None

        out["rows"].append(rec)
        with open(OUT, "w") as fh:
            json.dump(out, fh, indent=2, default=str)
        print(f"  span_inflation_pf={rec['span_inflation_pf']}", flush=True)

    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(json.dumps(out["rows"], indent=2, default=str))


if __name__ == "__main__":
    main()