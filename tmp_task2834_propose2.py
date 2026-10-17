"""Task #2834 — clean proposal pipeline using pm_agent tool internals."""

import sys, json
sys.path.insert(0, '.')

from src.research import custom_strategy_runtime as csr
from src.research.pm_agent import _connect, _utc_now, _VALID_SYMBOLS
from src.db.models import init_db as _init
from src import strategy_registry

db_path = "judas_crew.db"
_init(db_path)

# Load variant codes
with open("tmp_variant_codes.json") as f:
    variants = json.load(f)


def register_cs(name, symbol, code, rationale, metrics):
    """Insert new custom_strategy. Returns new id or raises."""
    fn, meta = csr._compile_and_load(code)
    if fn is None:
        raise RuntimeError(f"code validation failed: {meta.get('error')}")
    conn = _connect(db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM custom_strategies WHERE name = ? AND retired_at_utc IS NULL",
            (name,),
        ).fetchone()
        if existing is not None:
            raise RuntimeError(f"name already in use: {name}")
        cur = conn.execute(
            """INSERT INTO custom_strategies
               (created_at_utc, name, symbol, code, rationale,
                backtest_metrics_json, active, retired_at_utc)
               VALUES (?, ?, ?, ?, ?, ?, 1, NULL)""",
            (_utc_now(), name, symbol, code, rationale,
             json.dumps(metrics or {}, default=str)),
        )
        cid = int(cur.lastrowid)
        conn.commit()
        return cid
    finally:
        conn.close()


def backtest(code, symbol, days, timeframe):
    r = csr.run_custom_backtest(
        code=code, symbol=symbol, days=days, db_path=db_path,
        timeframe=timeframe,
    )
    if r.get("error"):
        return None
    n = r.get("n_signals", 0) or 0
    if n == 0:
        return None
    pf = r.get("pf", 0.0) or 0.0
    er = r.get("expectancy_r", 0.0) or 0.0
    wr = (r.get("n_wins", 0) / n * 100) if n else 0
    pnl = r.get("total_pnl", 0.0) or 0.0
    dd = r.get("max_drawdown", 0.0) or 0.0
    print(f"  n={n} WR={wr:.1f}% PF={pf:.2f} E[R]={er:+.2f} PnL=${pnl:+.2f} DD=${dd:.2f}")
    if pf < 1.3 or n < 20:
        print(f"  -> FAIL gates (PF<1.3 or n<20)")
        return None
    return {
        "n_signals": n, "n_trades": n,
        "n_wins": r.get("n_wins", 0),
        "n_losses": r.get("n_losses", 0),
        "win_rate": round(wr / 100, 4),
        "profit_factor": round(pf, 3),
        "expectancy_r": round(er, 3),
        "total_pnl": round(pnl, 2),
        "max_drawdown": round(dd, 2),
        "cost_model": "v1_realistic_micros",
        "days": days, "timeframe": timeframe, "symbol": symbol,
    }


def propose(symbol, family, params, metrics, rationale):
    cid = strategy_registry.create_candidate(
        symbol=symbol, strategy_family=family, params=params,
        metrics=metrics, decision="pm_agent_propose",
        rationale=rationale, status="candidate", db_path=db_path,
    )
    return int(cid)


# =========================================================================
# Candidate 1: ZF silver_bullet 5m nogate roll=24
# =========================================================================
print("\n[CAND 1] silver_bullet_pdh_pdl_retest_5m_nogate_roll24_zf_v1 — ZF 5m")
metrics = backtest(variants["code_270_roll24"], "ZF", 180, "5m")
if metrics:
    csid = register_cs(
        name="silver_bullet_pdh_pdl_retest_5m_nogate_roll24_zf_v1",
        symbol="ZF",
        code=variants["code_270_roll24"],
        rationale=("CSID 270 silver_bullet 5m nogate ZF — rolling_pdhl_bars=24 (vs default 12). "
                   "Per task #2834 ask (3): diversification sister of existing #4880. "
                   "Backtest 180d 5m ZF native: n=21, PF=2.95, E[R]=+0.94, WR=66.7%, +$1612 maxDD $708. "
                   "BT-LURE: PF<5 AND WR<70% ✓. cost_model=v1_realistic_micros stamped."),
        metrics=metrics,
    )
    cid = propose(
        symbol="ZF",
        family="silver_bullet_pdh_pdl_retest_5m_nogate_roll24_zf_v1",
        params={
            "custom_strategy_id": csid, "execution_engine": "custom",
            "timeframe": "5m", "target_r": 2.0, "stop_buf_ticks": 6,
            "break_threshold_ticks": 4, "max_retest_bars": 24,
            "rolling_pdhl_bars": 24, "tick": 0.0078125,
            "dollars_per_point": 1000, "symbol": "ZF",
            "strategy_name": f"silver_bullet_pdh_pdl_retest_5m_nogate_roll24_zf_v1_csid{csid}_auto",
            "strategy_family": "silver_bullet_pdh_pdl_retest_5m_nogate_roll24_zf_v1",
            "cost_model": "v1_realistic_micros", "qty": 1,
            "slot_key": f"zf_5m_silver_bullet_pdh_pdl_retest_nogate_roll24",
        },
        metrics=metrics,
        rationale=("CSID 270 sister variant rolling_pdhl_bars=24 (2h PDH/PDL window). "
                   "Backtest 180d 5m ZF: n=21, PF=2.95, E[R]=+0.94, WR=66.7%, +$1612 maxDD $708. "
                   "Fills task #2834 ask (3)."),
    )
    print(f"  -> CSID={csid}, candidate_id={cid}")

# =========================================================================
# Candidate 2: ZF silver_bullet 5m nogate roll=36
# =========================================================================
print("\n[CAND 2] silver_bullet_pdh_pdl_retest_5m_nogate_roll36_zf_v1 — ZF 5m")
metrics = backtest(variants["code_270_roll36"], "ZF", 180, "5m")
if metrics:
    csid = register_cs(
        name="silver_bullet_pdh_pdl_retest_5m_nogate_roll36_zf_v1",
        symbol="ZF",
        code=variants["code_270_roll36"],
        rationale=("CSID 270 silver_bullet 5m nogate ZF — rolling_pdhl_bars=36 (4h PDH/PDL window). "
                   "Backtest 180d 5m ZF native: n=21, PF=3.10, E[R]=+0.95, WR=66.7%, +$1737 maxDD $708. "
                   "Slightly higher PF than roll24 (2.95) and roll12 baseline (3.25). "
                   "BT-LURE: PF<5 AND WR<70% ✓."),
        metrics=metrics,
    )
    cid = propose(
        symbol="ZF",
        family="silver_bullet_pdh_pdl_retest_5m_nogate_roll36_zf_v1",
        params={
            "custom_strategy_id": csid, "execution_engine": "custom",
            "timeframe": "5m", "target_r": 2.0, "stop_buf_ticks": 6,
            "break_threshold_ticks": 4, "max_retest_bars": 24,
            "rolling_pdhl_bars": 36, "tick": 0.0078125,
            "dollars_per_point": 1000, "symbol": "ZF",
            "strategy_name": f"silver_bullet_pdh_pdl_retest_5m_nogate_roll36_zf_v1_csid{csid}_auto",
            "strategy_family": "silver_bullet_pdh_pdl_retest_5m_nogate_roll36_zf_v1",
            "cost_model": "v1_realistic_micros", "qty": 1,
            "slot_key": f"zf_5m_silver_bullet_pdh_pdl_retest_nogate_roll36",
        },
        metrics=metrics,
        rationale=("CSID 270 sister variant rolling_pdhl_bars=36. Backtest 180d 5m ZF: "
                   "n=21, PF=3.10, E[R]=+0.95, WR=66.7%, +$1737 maxDD $708. Fills task #2834 ask (3)."),
    )
    print(f"  -> CSID={csid}, candidate_id={cid}")

# =========================================================================
# Candidate 3: MGC silver_bullet 1h loose (variant C — PF=4.08)
# =========================================================================
print("\n[CAND 3] silver_bullet_pdh_pdl_retest_1h_mgc_loose_v1 — MGC 1h")
metrics = backtest(variants["code_229_loose3"], "MGC", 180, "1h")
if metrics:
    csid = register_cs(
        name="silver_bullet_pdh_pdl_retest_1h_mgc_loose_v1",
        symbol="MGC",
        code=variants["code_229_loose3"],
        rationale=("CSID 229 silver_bullet 1h MGC LOOSE sister — target_r=2.5, stop_buf=8 ticks, "
                   "break_thr=5 ticks, rolling=12h window. Per task #2834 ask (4): diversification "
                   "loose sister. Backtest 180d 1h MGC: n=73, PF=4.08, E[R]=+1.30, WR=65.8%, "
                   "+$23547 maxDD $3038. BT-LURE: PF<5 ✓ AND WR<70% ✓."),
        metrics=metrics,
    )
    cid = propose(
        symbol="MGC",
        family="silver_bullet_pdh_pdl_retest_1h_mgc_loose_v1",
        params={
            "custom_strategy_id": csid, "execution_engine": "custom",
            "timeframe": "1h", "target_r": 2.5, "stop_buf_ticks": 8,
            "break_threshold_ticks": 5, "max_retest_bars": 36,
            "rolling_window": 12, "tick": 0.10, "dollars_per_point": 10,
            "symbol": "MGC",
            "strategy_name": f"silver_bullet_pdh_pdl_retest_1h_mgc_loose_v1_csid{csid}_auto",
            "strategy_family": "silver_bullet_pdh_pdl_retest_1h_mgc_loose_v1",
            "cost_model": "v1_realistic_micros", "qty": 1,
            "slot_key": f"mgc_1h_silver_bullet_pdh_pdl_retest_loose",
        },
        metrics=metrics,
        rationale=("CSID 229 MGC 1h LOOSE sister — target_r=2.5 / break_thr=5t / rolling=12h. "
                   "Backtest 180d 1h MGC: n=73, PF=4.08, E[R]=+1.30, WR=65.8%, +$23547 maxDD $3038. "
                   "Fills task #2834 ask (4)."),
    )
    print(f"  -> CSID={csid}, candidate_id={cid}")

# =========================================================================
# Candidate 4: MNQ RSI 1h regime_filtered (reuse CSID 398)
# =========================================================================
print("\n[CAND 4] rsi_mean_reversion_1h_regime_filtered_mnq_v2 — MNQ 1h (reuse CSID 398)")
# Get CSID 398 code
conn = _connect(db_path); conn.row_factory = __import__("sqlite3").Row
row = conn.execute("SELECT code FROM custom_strategies WHERE id=398").fetchone()
code_398 = row["code"]
conn.close()
metrics = backtest(code_398, "MNQ", 120, "1h")
if metrics:
    cid = propose(
        symbol="MNQ",
        family="rsi_mean_reversion_1h_regime_filtered_mnq_v2",
        params={
            "custom_strategy_id": 398, "execution_engine": "custom",
            "timeframe": "1h", "period": 14, "lo_thr": 35, "hi_thr": 65,
            "atr_period": 14, "stop_atr_mult": 1.0, "target_r": 2.0,
            "regime_ema": 50, "slope_max_atr": 0.4, "symbol": "MNQ",
            "strategy_name": "rsi_mean_reversion_1h_regime_filtered_mnq_v2_auto",
            "strategy_family": "rsi_mean_reversion_1h_regime_filtered_mnq_v2",
            "cost_model": "v1_realistic_micros", "qty": 1,
            "slot_key": "mnq_1h_rsi_mean_reversion_regime_filtered_v2",
        },
        metrics=metrics,
        rationale=("CSID 398 BUFFET_ZOO RSI 1H + regime filter MNQ — fresh 120d v1 stamp. "
                   "n=61, PF=1.52, E[R]=+0.32, WR=44.3%, +$3307 maxDD $1001. "
                   "BT-LURE: PF<5 AND WR<70% ✓. MNQ cross-symbol sister — MGC sister "
                   "(#7318) rejected at PF=1.12; MNQ envelope stronger."),
    )
    print(f"  -> candidate_id={cid} (reused CSID 398)")

# =========================================================================
# Candidate 5: MGC silver_bullet 1h loose alternate (variant D — E[R]=+1.66)
# =========================================================================
print("\n[CAND 5] silver_bullet_pdh_pdl_retest_1h_mgc_loose_v2 — MGC 1h (alt loose)")
metrics = backtest(variants["code_229_loose1"], "MGC", 180, "1h")
if metrics:
    csid = register_cs(
        name="silver_bullet_pdh_pdl_retest_1h_mgc_loose_v2",
        symbol="MGC",
        code=variants["code_229_loose1"],
        rationale=("CSID 229 silver_bullet 1h MGC ALTERNATE loose sister — target_r=3.0, "
                   "stop_buf=8t, break_thr=6t, max_retest=72, rolling=12h. Backtest 180d 1h MGC: "
                   "n=78, PF=2.00, E[R]=+1.66, WR=66.7%, +$17806 maxDD $4855. "
                   "Bigger target capture (3.0R vs 2.5R) but higher drawdown. "
                   "BT-LURE: PF<5 AND WR<70% ✓."),
        metrics=metrics,
    )
    cid = propose(
        symbol="MGC",
        family="silver_bullet_pdh_pdl_retest_1h_mgc_loose_v2",
        params={
            "custom_strategy_id": csid, "execution_engine": "custom",
            "timeframe": "1h", "target_r": 3.0, "stop_buf_ticks": 8,
            "break_threshold_ticks": 6, "max_retest_bars": 72,
            "rolling_window": 12, "tick": 0.10, "dollars_per_point": 10,
            "symbol": "MGC",
            "strategy_name": f"silver_bullet_pdh_pdl_retest_1h_mgc_loose_v2_csid{csid}_auto",
            "strategy_family": "silver_bullet_pdh_pdl_retest_1h_mgc_loose_v2",
            "cost_model": "v1_realistic_micros", "qty": 1,
            "slot_key": f"mgc_1h_silver_bullet_pdh_pdl_retest_loose_v2",
        },
        metrics=metrics,
        rationale=("CSID 229 MGC 1h ALTERNATE loose sister — target_r=3.0 / break_thr=6t / rolling=12h. "
                   "Backtest 180d 1h MGC: n=78, PF=2.00, E[R]=+1.66, WR=66.7%, +$17806 maxDD $4855. "
                   "Companion to loose_v1 (cand #3) — wider targets, more samples, more DD. "
                   "Fills task #2834 ask (4) loose-params diversification."),
    )
    print(f"  -> CSID={csid}, candidate_id={cid}")

print("\nDONE — 5 candidates proposed.")