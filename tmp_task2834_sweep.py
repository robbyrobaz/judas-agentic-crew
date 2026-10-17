"""Task #2834 — Pre-reset pipeline refill sweep.

Tests:
  1. CSID 270 ZF roll24 sister (vs default roll12)  [ZF 5m]
  2. CSID 397 6J London Silver Bullet 3-5AM EST    [6J 5m] — needs proper tick override
  3. CSID 229 MGC 1h silver_bullet loose sister    [MGC 1h]
  4. CSID 398 BUFFET_ZOO RSI 1H MGC re-test        [MGC 1h]
  5. CSID 312 turtle_soup 15m — port to ZF 15m     [ZF 15m]

Cost-model: v1_realistic_micros (default).
Days: 120 (fresh stamp per task) except 1h strategies use 180 for stats.
"""

import sys
import json

sys.path.insert(0, '.')

from src.research.custom_strategy_runtime import run_custom_backtest

# Load source codes
with open('.research_cache/270.py') as f:
    code_270 = f.read()

with open('.research_cache/229.py') as f:
    code_229 = f.read()

# Load from DB-cached files (via query) — fetch 397 and 398 by name
from src.research.custom_strategy_runtime import load_custom_strategy
from src.db.models import init_db
import sqlite3, os

db_path = os.environ.get("JUDAS_DB", "judas_crew.db")
init_db(db_path)
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
for csid in [397, 398, 312]:
    row = conn.execute("SELECT code FROM custom_strategies WHERE id=?", (csid,)).fetchone()
    globals()[f"code_{csid}"] = row["code"]
conn.close()


def _wrap(code, override_params):
    """Inject override_params at the top of evaluate()."""
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
    line = (f"{sym:>5} {label:<48} n={n:>4}  WR={wr:>5.1f}%  PF={pf:>5.2f}  "
            f"E[R]={er:>+5.2f}  PnL=${pnl:>+9.2f}  DD=${dd:>7.2f}")
    if err:
        line += f"  ERR={err}"
    print(line)
    return r


print("=" * 130)
print("TASK #2834 SWEEP — pipeline refill")
print("=" * 130)

results = {}

# ============================================================
# 1. CSID 270 ZF roll24 (vs default 12) — silver bullet 5m
# ============================================================
print("\n[1] CSID 270 silver_bullet_5m_nogate_v1 — rolling_pdhl_bars=24 vs 12 (ZF)")
for roll in [12, 24, 18, 36, 48]:
    wrapped = _wrap(code_270, {"rolling_pdhl_bars": roll})
    r = run_custom_backtest(code=wrapped, symbol="ZF", days=120, timeframe="5m")
    label = f"ZF 5m roll={roll}"
    results[f"csid270_zf_roll{roll}"] = _summ(r, "ZF", label)

# ============================================================
# 2. CSID 397 6J London Silver Bullet (3-5 AM EST = 7-10 UTC summer EDT)
# Note: 3-5 AM EST = 8-10 UTC (winter EST) or 7-9 UTC (summer EDT).
# We pick 7-9 UTC to cover the summer window; winter window 8-10 will catch too.
# ============================================================
print("\n[2] CSID 397 london_silver_bullet_fvg_3to5am_5m_6j_v1 — 6J 5m")
# 6J micro tick = 0.000001 ($0.50 per 0.000001), DPP not used in this code
# Try multiple window variants
for win_lo, win_hi in [(7, 9), (8, 10), (7, 10), (3, 5)]:  # 3-5 is window raw hour
    for tick in [0.000001, 0.0001]:
        wrapped = _wrap(code_397, {
            "window_start_utc": float(win_lo),
            "window_end_utc": float(win_hi),
            "tick": tick,
            "fvg_min_ticks": 5,
        })
        r = run_custom_backtest(code=wrapped, symbol="6J", days=120, timeframe="5m")
        label = f"6J 5m win={win_lo}-{win_hi}UTC tick={tick}"
        results[f"csid397_6j_w{win_lo}-{win_hi}_t{tick}"] = _summ(r, "6J", label)

# ============================================================
# 3. CSID 229 MGC 1h silver_bullet loose sister
# Original params: target_r=2.0, stop_buf_ticks=6, break_thr=4 ticks, max_retest=36, rolling=24
# Looser = bigger max_retest, smaller break_thr (more signals), maybe lower rolling
# ============================================================
print("\n[3] CSID 229 silver_bullet_pdh_pdl_retest_1h_mgc_v1 — LOOSE variants (MGC 1h)")
loose_variants = [
    ("base",    {"target_r": 2.0, "stop_buf_ticks": 6, "break_threshold_ticks": 4, "max_retest_bars": 36, "rolling_window": 24}),
    ("loose1",  {"target_r": 2.0, "stop_buf_ticks": 6, "break_threshold_ticks": 3, "max_retest_bars": 48, "rolling_window": 24}),
    ("loose2",  {"target_r": 1.5, "stop_buf_ticks": 4, "break_threshold_ticks": 3, "max_retest_bars": 60, "rolling_window": 24}),
    ("loose3",  {"target_r": 2.5, "stop_buf_ticks": 8, "break_threshold_ticks": 5, "max_retest_bars": 24, "rolling_window": 12}),
    ("loose4",  {"target_r": 1.5, "stop_buf_ticks": 6, "break_threshold_ticks": 4, "max_retest_bars": 48, "rolling_window": 36}),
    ("loose5",  {"target_r": 2.0, "stop_buf_ticks": 6, "break_threshold_ticks": 2, "max_retest_bars": 48, "rolling_window": 24}),
]
for name, p in loose_variants:
    p["tick"] = 0.10
    wrapped = _wrap(code_229, p)
    r = run_custom_backtest(code=wrapped, symbol="MGC", days=180, timeframe="1h")
    label = f"MGC 1h {name}: {p}"
    results[f"csid229_mgc_{name}"] = _summ(r, "MGC", label)

# ============================================================
# 4. CSID 398 BUFFET_ZOO RSI 1H MGC re-test
# ============================================================
print("\n[4] CSID 398 rsi_mean_reversion_1h_regime_filtered_v1 — MGC 1h")
for days in [120, 180]:
    r = run_custom_backtest(code=code_398, symbol="MGC", days=days, timeframe="1h")
    label = f"MGC 1h RSI days={days}"
    results[f"csid398_mgc_d{days}"] = _summ(r, "MGC", label)
# Also test on MNQ for comparison
for days in [120]:
    r = run_custom_backtest(code=code_398, symbol="MNQ", days=days, timeframe="1h")
    label = f"MNQ 1h RSI days={days}"
    results[f"csid398_mnq_d{days}"] = _summ(r, "MNQ", label)

# ============================================================
# 5. CSID 312 turtle_soup 15m MGC → port to ZF 15m
# CSID 312 uses session windows 6-10 UTC (London) or 11-15 UTC (NY) and prior session H/L
# ZF needs proper session — let me try
# ============================================================
print("\n[5] CSID 312 turtle_soup_15m_mgc_v1 — port to ZF 15m")
for tick in [0.0078125, 0.015625]:
    wrapped = _wrap(code_312, {
        "tick": tick,
        "atr_period": 14,
        "displacement_max_atr": 0.30,
        "stop_buf_atr": 0.25,
        "rr": 2.0,
    })
    r = run_custom_backtest(code=wrapped, symbol="ZF", days=180, timeframe="15m")
    label = f"ZF 15m tick={tick}"
    results[f"csid312_zf_t{tick}"] = _summ(r, "ZF", label)

# Cross-symbol MGC/MCL baselines for CSID 312
for sym, tick in [("MGC", 0.10), ("MCL", 0.01), ("MNQ", 0.25), ("6J", 0.000001)]:
    wrapped = _wrap(code_312, {"tick": tick})
    r = run_custom_backtest(code=wrapped, symbol=sym, days=180, timeframe="15m")
    label = f"{sym} 15m turtle_soup"
    results[f"csid312_{sym}"] = _summ(r, sym, label)

# Save summary
print("\n" + "=" * 130)
print("DONE — saving JSON")
print("=" * 130)
with open("tmp_task2834_results.json", "w") as f:
    json.dump({k: {kk: vv for kk, vv in v.items() if not callable(vv)} for k, v in results.items()}, f, indent=2, default=str)
print(f"Saved {len(results)} results to tmp_task2834_results.json")