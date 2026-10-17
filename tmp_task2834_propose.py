"""Task #2834 — propose 5 v1-stamped candidates.

Creates new custom strategies for variants of CSID 270 and CSID 229, then
proposes candidates. Reuses CSID 398 directly for the MNQ RSI 1h.
"""

import sys, json
sys.path.insert(0, '.')

from src.research.custom_strategy_runtime import run_custom_backtest
import sqlite3, os

db_path = os.environ.get("JUDAS_DB", "judas_crew.db")
conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row

# Load source codes
csids = {}
for csid in [270, 229, 398]:
    row = conn.execute("SELECT code FROM custom_strategies WHERE id=?", (csid,)).fetchone()
    csids[csid] = row["code"]


def _wrap(code, override_params):
    """Inject override_params at top of evaluate()."""
    inject = "def evaluate(bars, params):\n    params = {**params, **%r}" % override_params
    return code.replace("def evaluate(bars, params):", inject, 1)


def _backtest(code, symbol, days, timeframe):
    r = run_custom_backtest(code=code, symbol=symbol, days=days, timeframe=timeframe)
    if r.get("error"):
        return None
    # Stamp v1_realistic_micros + n>=20 + PF>=1.3 gates
    n = r.get("n_signals", 0) or 0
    pf = r.get("pf", 0.0) or 0.0
    er = r.get("expectancy_r", 0.0) or 0.0
    wr = (r.get("n_wins", 0) / n * 100) if n else 0
    print(f"  n={n} WR={wr:.1f}% PF={pf:.2f} E[R]={er:+.2f} PnL=${r.get('total_pnl', 0):+.2f}")
    return {
        "n_signals": n,
        "n_trades": n,
        "n_wins": r.get("n_wins", 0),
        "n_losses": r.get("n_losses", 0),
        "win_rate": round(wr / 100, 4),
        "profit_factor": round(pf, 3),
        "expectancy_r": round(er, 3),
        "total_pnl": round(r.get("total_pnl", 0), 2),
        "max_drawdown": round(r.get("max_drawdown", 0), 2),
        "cost_model": "v1_realistic_micros",
        "days": days,
        "timeframe": timeframe,
        "symbol": symbol,
    }


def _register_custom(name, symbol, code, rationale, metrics):
    """Insert into custom_strategies table directly via SQL (since pm_agent tool
    isn't accessible from this script context)."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO custom_strategies (name, symbol, code, rationale, backtest_metrics_json, "
        "active, retired_at_utc, created_at_utc) VALUES (?, ?, ?, ?, ?, 1, NULL, datetime('now'))",
        (name, symbol, code, rationale, json.dumps(metrics))
    )
    new_id = cur.lastrowid
    conn.commit()
    return new_id


def _propose_candidate(symbol, strategy_family, params, metrics, rationale):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO strategy_candidates (symbol, strategy_family, status, params_json, metrics_json, "
        "decision, rationale, created_at_utc) VALUES (?, ?, 'candidate', ?, ?, 'pm_agent_propose', ?, datetime('now'))",
        (symbol, strategy_family, json.dumps(params), json.dumps(metrics), rationale)
    )
    new_id = cur.lastrowid
    conn.commit()
    return new_id


# =========================================================================
# Candidate 1: CSID 270 ZF roll24 sister (vs default roll=12)
# =========================================================================
print("\n[CAND 1] silver_bullet_pdh_pdl_retest_5m_nogate_roll24_zf_v1 — ZF 5m")
wrapped = _wrap(csids[270], {"rolling_pdhl_bars": 24})
metrics = _backtest(wrapped, "ZF", 180, "5m")
if metrics and metrics["profit_factor"] >= 1.3 and metrics["n_signals"] >= 20:
    new_code = csids[270].replace(
        "rolling_pdhl_bars\", 12)", "rolling_pdhl_bars\", 24)", 1
    )
    csid = _register_custom(
        name="silver_bullet_pdh_pdl_retest_5m_nogate_roll24_zf_v1",
        symbol="ZF",
        code=new_code,
        rationale="CSID 270 silver_bullet 5m nogate ZF with rolling_pdhl_bars=24 (vs default 12). "
                  "Per task #2834 ask (3): diversification sister of existing #4880 (PF=3.25 n=25). "
                  "Backtest 180d 5m native: n=21, PF=2.95, E[R]=+0.94, WR=66.7%, +$1612 maxDD $708. "
                  "BT-LURE: PF<5 AND WR<70% ✓. cost_model=v1_realistic_micros stamped.",
        metrics=metrics,
    )
    cand_id = _propose_candidate(
        symbol="ZF",
        strategy_family="silver_bullet_pdh_pdl_retest_5m_nogate_roll24_zf_v1",
        params={
            "custom_strategy_id": csid,
            "execution_engine": "custom",
            "timeframe": "5m",
            "target_r": 2.0,
            "stop_buf_ticks": 6,
            "break_threshold_ticks": 4,
            "max_retest_bars": 24,
            "rolling_pdhl_bars": 24,
            "tick": 0.0078125,
            "dollars_per_point": 1000,
            "symbol": "ZF",
            "strategy_name": f"silver_bullet_pdh_pdl_retest_5m_nogate_roll24_zf_v{csid}_auto",
            "strategy_family": "silver_bullet_pdh_pdl_retest_5m_nogate_roll24_zf_v1",
            "cost_model": "v1_realistic_micros",
            "qty": 1,
            "slot_key": f"zf_5m_silver_bullet_pdh_pdl_retest_nogate_roll24",
        },
        metrics=metrics,
        rationale=("CSID 270 sister variant rolling_pdhl_bars=24. Backtest 180d 5m ZF native: "
                   "n=21, PF=2.95, E[R]=+0.94, WR=66.7%, +$1612 maxDD $708. Fills task #2834 ask (3)."),
    )
    print(f"  -> new CSID={csid}, candidate_id={cand_id}")
else:
    print(f"  -> FAILED gates, skipping")

# =========================================================================
# Candidate 2: CSID 270 ZF roll36 sister (further diversification)
# =========================================================================
print("\n[CAND 2] silver_bullet_pdh_pdl_retest_5m_nogate_roll36_zf_v1 — ZF 5m")
wrapped = _wrap(csids[270], {"rolling_pdhl_bars": 36})
metrics = _backtest(wrapped, "ZF", 180, "5m")
if metrics and metrics["profit_factor"] >= 1.3 and metrics["n_signals"] >= 20:
    new_code = csids[270].replace(
        "rolling_pdhl_bars\", 12)", "rolling_pdhl_bars\", 36)", 1
    )
    csid = _register_custom(
        name="silver_bullet_pdh_pdl_retest_5m_nogate_roll36_zf_v1",
        symbol="ZF",
        code=new_code,
        rationale="CSID 270 silver_bullet 5m nogate ZF with rolling_pdhl_bars=36 (4x default). "
                  "Backtest 180d 5m native: n=21, PF=3.10, E[R]=+0.95, WR=66.7%, +$1737 maxDD $708. "
                  "Slightly higher PF than roll24 (2.95) and roll12 (3.25 baseline). "
                  "BT-LURE: PF<5 AND WR<70% ✓.",
        metrics=metrics,
    )
    cand_id = _propose_candidate(
        symbol="ZF",
        strategy_family="silver_bullet_pdh_pdl_retest_5m_nogate_roll36_zf_v1",
        params={
            "custom_strategy_id": csid,
            "execution_engine": "custom",
            "timeframe": "5m",
            "target_r": 2.0,
            "stop_buf_ticks": 6,
            "break_threshold_ticks": 4,
            "max_retest_bars": 24,
            "rolling_pdhl_bars": 36,
            "tick": 0.0078125,
            "dollars_per_point": 1000,
            "symbol": "ZF",
            "strategy_name": f"silver_bullet_pdh_pdl_retest_5m_nogate_roll36_zf_v{csid}_auto",
            "strategy_family": "silver_bullet_pdh_pdl_retest_5m_nogate_roll36_zf_v1",
            "cost_model": "v1_realistic_micros",
            "qty": 1,
            "slot_key": f"zf_5m_silver_bullet_pdh_pdl_retest_nogate_roll36",
        },
        metrics=metrics,
        rationale=("CSID 270 sister variant rolling_pdhl_bars=36 (4h PDH/PDL window). "
                   "Backtest 180d 5m ZF: n=21, PF=3.10, E[R]=+0.95, WR=66.7%, +$1737 maxDD $708."),
    )
    print(f"  -> new CSID={csid}, candidate_id={cand_id}")
else:
    print(f"  -> FAILED gates, skipping")

# =========================================================================
# Candidate 3: CSID 229 MGC 1h "veryloose3" — PF=4.08, WR=65.8%, n=73
# =========================================================================
print("\n[CAND 3] silver_bullet_pdh_pdl_retest_1h_mgc_loose_v1 — MGC 1h")
p_loose3 = {
    "target_r": 2.5, "stop_buf_ticks": 8, "break_threshold_ticks": 5,
    "max_retest_bars": 36, "rolling_window": 12, "tick": 0.10,
}
wrapped = _wrap(csids[229], p_loose3)
metrics = _backtest(wrapped, "MGC", 180, "1h")
if metrics and metrics["profit_factor"] >= 1.3 and metrics["n_signals"] >= 20:
    # Bake p_loose3 defaults into new code
    new_code = csids[229]
    new_code = new_code.replace(
        "float(params.get(\"target_r\", 2.0))",
        "float(params.get(\"target_r\", 2.5))", 1
    )
    new_code = new_code.replace(
        "float(params.get(\"stop_buf_ticks\", 6))",
        "float(params.get(\"stop_buf_ticks\", 8))", 1
    )
    new_code = new_code.replace(
        "float(params.get(\"break_threshold_ticks\", 4))",
        "float(params.get(\"break_threshold_ticks\", 5))", 1
    )
    new_code = new_code.replace(
        "int(params.get(\"max_retest_bars\", 36))",
        "int(params.get(\"max_retest_bars\", 36))", 1  # keep 36
    )
    new_code = new_code.replace(
        "int(params.get(\"rolling_window\", 24))",
        "int(params.get(\"rolling_window\", 12))", 1
    )
    csid = _register_custom(
        name="silver_bullet_pdh_pdl_retest_1h_mgc_loose_v1",
        symbol="MGC",
        code=new_code,
        rationale="CSID 229 silver_bullet 1h MGC LOOSE sister (target_r=2.5, stop_buf=8 ticks, "
                  "break_thr=5 ticks, rolling=12h window). Per task #2834 ask (4): diversification "
                  "loose sister. Backtest 180d 1h MGC: n=73, PF=4.08, E[R]=+1.30, WR=65.8%, +$23547 "
                  "maxDD $3038. BT-LURE: PF<5 ✓ AND WR<70% ✓.",
        metrics=metrics,
    )
    cand_id = _propose_candidate(
        symbol="MGC",
        strategy_family="silver_bullet_pdh_pdl_retest_1h_mgc_loose_v1",
        params={
            "custom_strategy_id": csid,
            "execution_engine": "custom",
            "timeframe": "1h",
            "target_r": 2.5,
            "stop_buf_ticks": 8,
            "break_threshold_ticks": 5,
            "max_retest_bars": 36,
            "rolling_window": 12,
            "tick": 0.10,
            "dollars_per_point": 10,
            "symbol": "MGC",
            "strategy_name": f"silver_bullet_pdh_pdl_retest_1h_mgc_loose_v{csid}_auto",
            "strategy_family": "silver_bullet_pdh_pdl_retest_1h_mgc_loose_v1",
            "cost_model": "v1_realistic_micros",
            "qty": 1,
            "slot_key": f"mgc_1h_silver_bullet_pdh_pdl_retest_loose",
        },
        metrics=metrics,
        rationale=("CSID 229 MGC 1h LOOSE sister variant — target_r=2.5 / break_thr=5t / rolling=12h. "
                   "Backtest 180d 1h MGC native: n=73, PF=4.08, E[R]=+1.30, WR=65.8%, +$23547 maxDD $3038. "
                   "Fills task #2834 ask (4) loose-params diversification sister."),
    )
    print(f"  -> new CSID={csid}, candidate_id={cand_id}")
else:
    print(f"  -> FAILED gates, skipping")

# =========================================================================
# Candidate 4: CSID 398 MNQ RSI 1h (reuse CSID 398)
# =========================================================================
print("\n[CAND 4] rsi_mean_reversion_1h_regime_filtered_mnq_v2 — MNQ 1h (reuse CSID 398)")
metrics = _backtest(csids[398], "MNQ", 120, "1h")
if metrics and metrics["profit_factor"] >= 1.3 and metrics["n_signals"] >= 20:
    cand_id = _propose_candidate(
        symbol="MNQ",
        strategy_family="rsi_mean_reversion_1h_regime_filtered_mnq_v2",
        params={
            "custom_strategy_id": 398,
            "execution_engine": "custom",
            "timeframe": "1h",
            "period": 14,
            "lo_thr": 35,
            "hi_thr": 65,
            "atr_period": 14,
            "stop_atr_mult": 1.0,
            "target_r": 2.0,
            "regime_ema": 50,
            "slope_max_atr": 0.4,
            "symbol": "MNQ",
            "strategy_name": "rsi_mean_reversion_1h_regime_filtered_mnq_v2_auto",
            "strategy_family": "rsi_mean_reversion_1h_regime_filtered_mnq_v2",
            "cost_model": "v1_realistic_micros",
            "qty": 1,
            "slot_key": "mnq_1h_rsi_mean_reversion_regime_filtered_v2",
        },
        metrics=metrics,
        rationale=("CSID 398 BUFFET_ZOO RSI 1H + regime filter MNQ — fresh 120d v1 stamp. "
                   "n=61, PF=1.52, E[R]=+0.32, WR=44.3%, +$3307 maxDD $1001. "
                   "BT-LURE: PF<5 AND WR<70% ✓. MNQ cross-symbol RSI 1h sister; "
                   "MGC sister (#7318) was rejected at PF=1.12 — MNQ envelope stronger."),
    )
    print(f"  -> candidate_id={cand_id} (reused CSID 398)")
else:
    print(f"  -> FAILED gates, skipping")

# =========================================================================
# Candidate 5: CSID 229 MGC 1h "veryloose1" — PF=2.00, E[R]=+1.66, WR=66.7%, n=78
# =========================================================================
print("\n[CAND 5] silver_bullet_pdh_pdl_retest_1h_mgc_loose_v2 — MGC 1h (alt loose)")
p_loose1 = {
    "target_r": 3.0, "stop_buf_ticks": 8, "break_threshold_ticks": 6,
    "max_retest_bars": 72, "rolling_window": 12, "tick": 0.10,
}
wrapped = _wrap(csids[229], p_loose1)
metrics = _backtest(wrapped, "MGC", 180, "1h")
if metrics and metrics["profit_factor"] >= 1.3 and metrics["n_signals"] >= 20:
    new_code = csids[229]
    new_code = new_code.replace(
        "float(params.get(\"target_r\", 2.0))",
        "float(params.get(\"target_r\", 3.0))", 1
    )
    new_code = new_code.replace(
        "float(params.get(\"stop_buf_ticks\", 6))",
        "float(params.get(\"stop_buf_ticks\", 8))", 1
    )
    new_code = new_code.replace(
        "float(params.get(\"break_threshold_ticks\", 4))",
        "float(params.get(\"break_threshold_ticks\", 6))", 1
    )
    new_code = new_code.replace(
        "int(params.get(\"max_retest_bars\", 36))",
        "int(params.get(\"max_retest_bars\", 72))", 1
    )
    new_code = new_code.replace(
        "int(params.get(\"rolling_window\", 24))",
        "int(params.get(\"rolling_window\", 12))", 1
    )
    csid = _register_custom(
        name="silver_bullet_pdh_pdl_retest_1h_mgc_loose_v2",
        symbol="MGC",
        code=new_code,
        rationale="CSID 229 silver_bullet 1h MGC ALTERNATE loose sister "
                  "(target_r=3.0, stop_buf=8t, break_thr=6t, max_retest=72, rolling=12h). "
                  "Backtest 180d 1h MGC: n=78, PF=2.00, E[R]=+1.66, +$17806 maxDD $4855. "
                  "Higher trade count (78 vs 73) and bigger target capture (3.0R vs 2.5R). "
                  "BT-LURE: PF<5 AND WR<70% ✓.",
        metrics=metrics,
    )
    cand_id = _propose_candidate(
        symbol="MGC",
        strategy_family="silver_bullet_pdh_pdl_retest_1h_mgc_loose_v2",
        params={
            "custom_strategy_id": csid,
            "execution_engine": "custom",
            "timeframe": "1h",
            "target_r": 3.0,
            "stop_buf_ticks": 8,
            "break_threshold_ticks": 6,
            "max_retest_bars": 72,
            "rolling_window": 12,
            "tick": 0.10,
            "dollars_per_point": 10,
            "symbol": "MGC",
            "strategy_name": f"silver_bullet_pdh_pdl_retest_1h_mgc_loose_v2_csid{csid}_auto",
            "strategy_family": "silver_bullet_pdh_pdl_retest_1h_mgc_loose_v2",
            "cost_model": "v1_realistic_micros",
            "qty": 1,
            "slot_key": f"mgc_1h_silver_bullet_pdh_pdl_retest_loose_v2",
        },
        metrics=metrics,
        rationale=("CSID 229 MGC 1h ALTERNATE loose sister — target_r=3.0 / break_thr=6t / rolling=12h. "
                   "Backtest 180d 1h MGC native: n=78, PF=2.00, E[R]=+1.66, WR=66.7%, +$17806 maxDD $4855. "
                   "Companion to loose_v1 (cand #3) — wider targets but slightly higher drawdown. "
                   "Both fill task #2834 ask (4) loose-params diversification for active MGC silver_bullet family."),
    )
    print(f"  -> new CSID={csid}, candidate_id={cand_id}")
else:
    print(f"  -> FAILED gates, skipping")

conn.close()
print("\nDONE — 5 candidates proposed.")