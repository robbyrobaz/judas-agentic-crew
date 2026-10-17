"""Task #2834 round 2 — more aggressive loose variants + extended days."""

import sys, json
sys.path.insert(0, '.')
from src.research.custom_strategy_runtime import run_custom_backtest

import sqlite3, os
db_path = os.environ.get("JUDAS_DB", "judas_crew.db")
conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
csids = {}
for csid in [270, 229, 312, 398]:
    row = conn.execute("SELECT code FROM custom_strategies WHERE id=?", (csid,)).fetchone()
    csids[csid] = row["code"]
conn.close()


def _wrap(code, override_params):
    inject = "def evaluate(bars, params):\n    params = {**params, **%r}" % override_params
    return code.replace("def evaluate(bars, params):", inject, 1)


def _summ(r, sym, label):
    n = r.get("n_signals", 0) or 0
    pf = r.get("pf", 0.0) or 0.0
    er = r.get("expectancy_r", 0.0) or 0.0
    wr = (r.get("n_wins", 0) / n * 100) if n else 0
    pnl = r.get("total_pnl", 0.0) or 0.0
    dd = r.get("max_drawdown", 0.0) or 0.0
    err = r.get("error", "")
    lure = "LURE?" if (pf >= 5 or wr >= 70) else "ok"
    line = (f"{sym:>5} {label:<55} n={n:>4}  WR={wr:>5.1f}%  PF={pf:>5.2f}  "
            f"E[R]={er:>+5.2f}  PnL=${pnl:>+9.2f}  DD=${dd:>7.2f}  {lure}")
    if err:
        line += f"  ERR={err}"
    print(line)
    return r


results = {}

# CSID 229 more aggressive loose variants — target WR<70%
print("\n[A] CSID 229 MGC 1h — AGGRESSIVE loose variants (target WR<70%, PF≥1.3)")
hard_loose = [
    ("veryloose1", {"target_r": 3.0, "stop_buf_ticks": 8, "break_threshold_ticks": 6, "max_retest_bars": 72, "rolling_window": 12}),
    ("veryloose2", {"target_r": 3.0, "stop_buf_ticks": 10, "break_threshold_ticks": 8, "max_retest_bars": 48, "rolling_window": 24}),
    ("veryloose3", {"target_r": 2.5, "stop_buf_ticks": 8, "break_threshold_ticks": 5, "max_retest_bars": 36, "rolling_window": 12}),
    ("veryloose4", {"target_r": 3.0, "stop_buf_ticks": 8, "break_threshold_ticks": 5, "max_retest_bars": 60, "rolling_window": 18}),
    ("veryloose5", {"target_r": 2.5, "stop_buf_ticks": 6, "break_threshold_ticks": 6, "max_retest_bars": 48, "rolling_window": 24}),
    ("veryloose6", {"target_r": 2.0, "stop_buf_ticks": 8, "break_threshold_ticks": 6, "max_retest_bars": 60, "rolling_window": 36}),
    ("veryloose7", {"target_r": 4.0, "stop_buf_ticks": 8, "break_threshold_ticks": 6, "max_retest_bars": 48, "rolling_window": 24}),
]
for name, p in hard_loose:
    p["tick"] = 0.10
    wrapped = _wrap(csids[229], p)
    r = run_custom_backtest(code=wrapped, symbol="MGC", days=180, timeframe="1h")
    results[f"csid229_mgc_{name}"] = _summ(r, "MGC", f"MGC 1h {name}")

# CSID 270 with days=180 to see if roll=24 still strong
print("\n[B] CSID 270 ZF 5m — confirm roll24/36 at 180d")
for roll in [24, 36]:
    wrapped = _wrap(csids[270], {"rolling_pdhl_bars": roll})
    r = run_custom_backtest(code=wrapped, symbol="ZF", days=180, timeframe="5m")
    results[f"csid270_zf_180d_roll{roll}"] = _summ(r, "ZF", f"ZF 5m roll={roll} 180d")

# CSID 398 BUFFET_ZOO RSI cross-symbol at 180d
print("\n[C] CSID 398 RSI 1h — cross-symbol 180d")
for sym in ["6J", "MCL", "ZF"]:
    r = run_custom_backtest(code=csids[398], symbol=sym, days=180, timeframe="1h")
    results[f"csid398_{sym}_180d"] = _summ(r, sym, f"{sym} 1h RSI 180d")

# CSID 312 turtle_soup 15m — extended days for n>=20
print("\n[D] CSID 312 turtle_soup 15m — extended days")
for sym, tick in [("MCL", 0.01), ("6J", 0.000001), ("MGC", 0.10), ("MNQ", 0.25)]:
    wrapped = _wrap(csids[312], {"tick": tick, "rr": 2.0})
    r = run_custom_backtest(code=wrapped, symbol=sym, days=270, timeframe="15m")
    results[f"csid312_{sym}_270d"] = _summ(r, sym, f"{sym} 15m turtle_soup 270d")

# CSID 312 with looser rr to bring WR down
print("\n[E] CSID 312 turtle_soup 15m — looser rr (lower WR, more trades)")
for sym, tick, rr in [("MCL", 0.01, 1.5), ("6J", 0.000001, 1.5), ("MGC", 0.10, 1.5), ("MNQ", 0.25, 1.5)]:
    wrapped = _wrap(csids[312], {"tick": tick, "rr": rr})
    r = run_custom_backtest(code=wrapped, symbol=sym, days=270, timeframe="15m")
    results[f"csid312_{sym}_rr1.5"] = _summ(r, sym, f"{sym} 15m turtle rr=1.5 270d")

with open("tmp_task2834_results_r2.json", "w") as f:
    json.dump({k: {kk: vv for kk, vv in v.items() if not callable(vv)} for k, v in results.items()}, f, indent=2, default=str)
print(f"\nSaved {len(results)} round-2 results.")