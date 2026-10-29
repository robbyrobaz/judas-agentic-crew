"""Smoke test: does csid 441 code load and fire on MGC 5m archive bars?

Bounded: last N bars of data/bars_archive/MGC_5m.parquet, house runtime
(run_custom_backtest_on_bars) which applies cost_model v1_realistic_micros.
Purpose is LOADABILITY + directional sanity, not stamp reproduction.
"""
import json
import sqlite3
import sys

import pandas as pd

sys.path.insert(0, ".")

from src.research.custom_strategy_runtime import run_custom_backtest_on_bars

DB = "judas_crew.db"
CSID = 441

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
row = conn.execute(
    "SELECT code, name, symbol, active FROM custom_strategies WHERE id = ?", (CSID,)
).fetchone()
conn.close()
if row is None:
    print("FAIL: no custom_strategies row", CSID)
    raise SystemExit(1)
code = row["code"]
print(f"csid={CSID} name={row['name']} symbol={row['symbol']} active={row['active']}")

cand = sqlite3.connect(DB)
cand.row_factory = sqlite3.Row
c = cand.execute("SELECT params_json FROM strategy_candidates WHERE id=7383").fetchone()
params = json.loads(c["params_json"])
cand.close()

bars = pd.read_parquet("data/bars_archive/MGC_5m.parquet")
print("archive bars:", len(bars), bars["ts"].iloc[0], "->", bars["ts"].iloc[-1])

# hygiene audit on the archive itself
rng = (bars["high"] - bars["low"])
placeholder = (rng <= 0) | (bars["volume"] <= 0)
print(f"placeholder bars (rng<=0 or vol<=0): {int(placeholder.sum())} "
      f"({placeholder.mean()*100:.1f}%)")

N = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
sub = bars.iloc[-N:].reset_index(drop=True)
res = run_custom_backtest_on_bars(
    code=code, bars=sub, params=params, symbol="MGC", qty=1, timeout_s=300
)
keys = ("error", "n_signals", "n_wins", "n_losses", "pf", "expectancy_r",
        "total_pnl", "max_drawdown", "stale_fill_n", "stale_measure_fail_n",
        "cost_model", "backtest_fill_valid")
print("window:", N, "bars", sub["ts"].iloc[0], "->", sub["ts"].iloc[-1])
for k in keys:
    if k in res:
        print(f"  {k} = {res[k]}")
if res.get("error"):
    print("SMOKE RESULT: ERROR ->", res["error"])
elif res.get("n_signals", 0) > 0:
    print("SMOKE RESULT: OK - code loads and fires")
else:
    print("SMOKE RESULT: NO SIGNALS - code loads but never fired")
