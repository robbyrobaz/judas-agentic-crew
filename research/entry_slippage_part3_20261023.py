"""Task #2873 part 3 — (C3) is the intended entry a PAST bar's open? (mechanism proof)
(D2) cost_model v2 spec + impact on the stamped candidate population (#2869's N).

Read-only. Uses data/bars_archive/*.parquet + judas_crew.db (ro).
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict

import pandas as pd

DB = "judas_crew.db"
ARCH = "data/bars_archive"
LEGAL = ["MGC", "MNQ", "MCL", "ZF", "6J"]
SPEC = {
    "MGC": {"tick": 0.10, "tick_value": 1.0},
    "MNQ": {"tick": 0.25, "tick_value": 0.5},
    "MCL": {"tick": 0.01, "tick_value": 1.0},
    "ZF": {"tick": 0.0078125, "tick_value": 7.8125},
    "6J": {"tick": 0.0000005, "tick_value": 6.25},
}


def pct(v, q):
    if not v:
        return None
    v = sorted(v)
    if len(v) == 1:
        return round(v[0], 4)
    pos = (len(v) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (pos - lo), 4)


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    meta = {}
    for r in cur.execute("SELECT id, symbol, params_json FROM active_strategies"):
        try:
            pj = json.loads(r["params_json"] or "{}")
        except Exception:
            pj = {}
        meta[int(r["id"])] = {"tf": pj.get("timeframe"), "engine": pj.get("execution_engine", "?"),
                              "csid": pj.get("custom_strategy_id")}

    q = """SELECT t.signal_id,t.symbol,t.direction,t.qty,t.entry_fill,t.stop_price,t.exit_fill,
                  t.pnl_dollars,t.opened_at,t.strategy_id,t.strategy_family,
                  s.entry intended,s.stop intended_stop
           FROM {tab} t JOIN signals s ON s.id=t.signal_id"""
    raw = []
    for tab in ("trades", "trades_archive"):
        raw += [dict(r) for r in cur.execute(q.format(tab=tab))]

    bars_cache = {}

    def get_bars(sym, tf):
        k = f"{sym}_{tf}"
        if k not in bars_cache:
            p = os.path.join(ARCH, k + ".parquet")
            if not os.path.exists(p):
                bars_cache[k] = None
            else:
                df = pd.read_parquet(p)
                df["ts"] = pd.to_datetime(df["ts"], utc=True)
                bars_cache[k] = df.sort_values("ts").reset_index(drop=True)
        return bars_cache[k]

    hits = []
    for d in raw:
        sym = (d["symbol"] or "").upper()
        if sym not in LEGAL or d["intended"] is None:
            continue
        sid = d["strategy_id"]
        m = meta.get(int(sid), {}) if sid else {}
        tf = m.get("tf")
        if not tf:
            continue
        df = get_bars(sym, tf)
        if df is None:
            continue
        t = pd.Timestamp(str(d["opened_at"]).replace("Z", "+00:00"))
        idx = df.index[df["ts"] <= t]
        if len(idx) == 0:
            continue
        i = int(idx[-1])           # decision bar (last closed bar at fill time)
        tick = SPEC[sym]["tick"]
        E = float(d["intended"])
        age_open = age_close = age_hl = None
        for j in range(i, max(-1, i - 60), -1):
            if age_open is None and abs(float(df.at[j, "open"]) - E) <= 0.5 * tick:
                age_open = i - j
            if age_close is None and abs(float(df.at[j, "close"]) - E) <= 0.5 * tick:
                age_close = i - j
            if age_hl is None and (abs(float(df.at[j, "high"]) - E) <= 0.5 * tick
                                   or abs(float(df.at[j, "low"]) - E) <= 0.5 * tick):
                age_hl = i - j
            if age_open is not None and age_close is not None and age_hl is not None:
                break
        # divergence of the FILL from the decision bar close (pure fill-time move)
        close_i = float(df.at[i, "close"])
        sign = 1.0 if (d["direction"] or "").lower() == "long" else -1.0
        qty = max(int(d["qty"] or 1), 1)
        hits.append({
            "symbol": sym, "tf": tf, "engine": m.get("engine"), "csid": m.get("csid"),
            "family": d["strategy_family"], "age_open": age_open, "age_close": age_close,
            "age_hl": age_hl,
            "fill_vs_decision_close_ticks": (float(d["entry_fill"]) - close_i) * sign / tick,
            "intended_vs_decision_close_ticks": (E - close_i) * sign / tick,
        })

    out = {"C3_rows": len(hits)}
    if hits:
        for key in ("age_open", "age_close", "age_hl"):
            vals = [h[key] for h in hits if h[key] is not None]
            out[f"C3_{key}"] = {
                "n_matched": len(vals), "n_total": len(hits),
                "match_rate": round(len(vals) / len(hits), 3),
                "age_bars": {f"p{q}": pct(vals, q) for q in (0.5, 0.75, 0.9, 0.95)},
                "share_age_0": round(sum(1 for v in vals if v == 0) / len(vals), 3) if vals else None,
                "share_age_le_2": round(sum(1 for v in vals if v <= 2) / len(vals), 3) if vals else None,
                "share_age_ge_6": round(sum(1 for v in vals if v >= 6) / len(vals), 3) if vals else None,
            }
        out["C3_fill_vs_decision_close"] = {
            "p50": pct([h["fill_vs_decision_close_ticks"] for h in hits], 0.5),
            "p90": pct([h["fill_vs_decision_close_ticks"] for h in hits], 0.9),
            "max": round(max(h["fill_vs_decision_close_ticks"] for h in hits), 2),
        }
        out["C3_intended_vs_decision_close"] = {
            "p50": pct([h["intended_vs_decision_close_ticks"] for h in hits], 0.5),
            "p90": pct([h["intended_vs_decision_close_ticks"] for h in hits], 0.9),
        }
        byc = defaultdict(list)
        for h in hits:
            byc[(h["symbol"], h["tf"])].append(h)
        out["C3_by_cell"] = {
            f"{k[0]}_{k[1]}": {
                "n": len(v),
                "open_match_rate": round(sum(1 for x in v if x["age_open"] is not None) / len(v), 3),
                "open_age_p50": pct([x["age_open"] for x in v if x["age_open"] is not None], 0.5),
                "fill_vs_close_p50_ticks": pct([x["fill_vs_decision_close_ticks"] for x in v], 0.5),
                "fill_vs_close_p90_ticks": pct([x["fill_vs_decision_close_ticks"] for x in v], 0.9),
            } for k, v in sorted(byc.items())}

    # ---------- (D2) cost-model v2 impact on the stamped candidate population ----------
    # observed adverse divergence percentiles (from part 1) — recompute here from the DB
    import statistics
    obs = defaultdict(list)
    for d in raw:
        sym = (d["symbol"] or "").upper()
        if sym not in LEGAL or d["intended"] is None or d["entry_fill"] is None:
            continue
        sign = 1.0 if (d["direction"] or "").lower() == "long" else -1.0
        obs[sym].append(((float(d["entry_fill"]) - float(d["intended"])) * sign) / SPEC[sym]["tick"])
    budgets = {}
    for sym, v in obs.items():
        adv = [max(0.0, x) for x in v]
        budgets[sym] = {
            "tick": SPEC[sym]["tick"], "tick_value": SPEC[sym]["tick_value"], "n": len(v),
            "p50": pct(adv, 0.5), "p75": pct(adv, 0.75), "p90": pct(adv, 0.9),
            "p99": pct(adv, 0.99), "max": round(max(adv), 2),
            "v1_charge_ticks": {"MGC": 1.0, "MNQ": 0.5, "MCL": 1.0, "ZF": 1.0, "6J": 1.0}[sym],
            "v2_p50_dollars_per_contract": round(max(0.0, pct(adv, 0.5) - 1.0) * SPEC[sym]["tick_value"], 2),
            "v2_p90_dollars_per_contract": round(max(0.0, pct(adv, 0.9) - 1.0) * SPEC[sym]["tick_value"], 2),
        }
    out["D2_budget"] = budgets

    stamped = []
    for r in cur.execute("SELECT id, symbol, strategy_family, params_json, metrics_json, status "
                          "FROM strategy_candidates WHERE metrics_json LIKE '%v1_realistic_micros%'"):
        try:
            m = json.loads(r["metrics_json"] or "{}")
        except Exception:
            continue
        sym = (r["symbol"] or "").upper()
        if sym not in LEGAL:
            continue
        n = float(m.get("n_signals") or 0)
        tot = float(m.get("total_pnl") or 0)
        try:
            pf = float(m.get("profit_factor"))
        except (TypeError, ValueError):
            pf = 0.0
        try:
            er = float(m.get("expectancy_r"))
        except (TypeError, ValueError):
            er = None
        if not n:
            continue
        per_trade = tot / n
        b = budgets.get(sym)
        extra50 = b["v2_p50_dollars_per_contract"] if b else None
        extra90 = b["v2_p90_dollars_per_contract"] if b else None
        risk_dollars = (per_trade / float(er)) if (er and float(er) != 0) else None
        stamped.append({
            "id": r["id"], "symbol": sym, "family": r["strategy_family"], "status": r["status"],
            "n": n, "PF": pf, "expectancy_r": er, "total_pnl": tot, "per_trade_net_$": round(per_trade, 2),
            "extra_p50_$": extra50, "extra_p90_$": extra90,
            "dies_at_p50": (extra50 is not None and extra50 >= per_trade and per_trade > 0),
            "dies_at_p90": (extra90 is not None and extra90 >= per_trade and per_trade > 0),
            "extra_p50_in_R": round(extra50 / risk_dollars, 2) if (extra50 is not None and risk_dollars) else None,
            "extra_p90_in_R": round(extra90 / risk_dollars, 2) if (extra90 is not None and risk_dollars) else None,
        })
    out["D2_stamped_population"] = {
        "n_stamped_legal": len(stamped),
        "n_with_PF_gt_1": sum(1 for s in stamped if (s["PF"] or 0) > 1),
        "n_die_at_p50": sum(1 for s in stamped if s["dies_at_p50"]),
        "n_die_at_p90": sum(1 for s in stamped if s["dies_at_p90"]),
        "n_PF_gt_1_but_die_at_p50": sum(1 for s in stamped if (s["PF"] or 0) > 1 and s["dies_at_p50"]),
        "median_extra_p50_in_R": pct([s["extra_p50_in_R"] for s in stamped if s["extra_p50_in_R"] is not None], 0.5),
        "p90_extra_p50_in_R": pct([s["extra_p50_in_R"] for s in stamped if s["extra_p50_in_R"] is not None], 0.9),
        "share_extra_p50_over_0.5R": round(sum(1 for s in stamped if (s["extra_p50_in_R"] or 0) > 0.5)
                                            / max(1, len([s for s in stamped if s["extra_p50_in_R"] is not None])), 3),
    }
    out["D2_examples"] = sorted(stamped, key=lambda s: -(s["PF"] or 0))[:12]

    with open("research/entry_slippage_part3_20261023.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str)[:11000])


if __name__ == "__main__":
    main()
