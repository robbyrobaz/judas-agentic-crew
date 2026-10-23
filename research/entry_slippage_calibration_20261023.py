"""ENTRY_SLIPPAGE_COST_CALIBRATION_AND_COUNTERFACTUAL (task #2873).

Read-only analysis of broker-confirmed fills joined to their originating signals.

Answers:
  (A) distribution of entry divergence in TICKS and in R (percentiles only)
  (B) counterfactual: skip entries whose fill diverged > k -> kept/rejected stats
  (C) attribution: by strategy_family, execution_engine, ET session; latency proxy
  (D) calibrated per-symbol slippage budget table + cost_model v2 spec

Never writes to the DB. Opens judas_crew.db read-only.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict

DB = "judas_crew.db"
LEGAL = ["MGC", "MNQ", "MCL", "ZF", "6J"]
BANNED = ["DX", "MET", "MBT"]

# src/portfolio_runtime.py:103-110  _CONTRACT_SPECS
SPEC = {
    "MGC": {"tick": 0.10, "tick_value": 1.0},
    "MNQ": {"tick": 0.25, "tick_value": 0.5},
    "MCL": {"tick": 0.01, "tick_value": 1.0},
    "ZF": {"tick": 0.0078125, "tick_value": 7.8125},
    "6J": {"tick": 0.0000005, "tick_value": 6.25},
    "MBT": {"tick": 5.0, "tick_value": 0.5},
    "MET": {"tick": 0.50, "tick_value": 0.05},
    "DX": {"tick": 0.005, "tick_value": 5.0},
}


def pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    v = sorted(vals)
    if len(v) == 1:
        return round(v[0], 4)
    # linear interpolation percentile
    pos = (len(v) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    frac = pos - lo
    return round(v[lo] + (v[hi] - v[lo]) * frac, 4)


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    pnl = [r["pnl_per_contract"] for r in rows]
    wins = sum(1 for r in rows if r["pnl_per_contract"] > 0)
    losses = sum(1 for r in rows if r["pnl_per_contract"] <= 0)
    gross_win = sum(p for p in pnl if p > 0)
    gross_loss = -sum(p for p in pnl if p < 0)
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    return {
        "n": n,
        "W": wins,
        "L": losses,
        "wr_pct": round(100.0 * wins / n, 1),
        "PF": round(pf, 3) if pf != float("inf") else None,
        "net_$": round(sum(pnl), 2),
        "$/contract": round(sum(pnl) / n, 2),
    }


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ---- engine map from active_strategies.params_json ----
    engine_by_strategy: dict[int, str] = {}
    for r in cur.execute("SELECT id, params_json FROM active_strategies"):
        eng = "unknown"
        try:
            pj = json.loads(r["params_json"] or "{}")
            eng = pj.get("execution_engine") or pj.get("engine") or "unknown"
        except Exception:
            pass
        engine_by_strategy[int(r["id"])] = str(eng)

    # ---- last decision bar ts per strategy (strategy_bar_state) ----
    bar_state = {int(r["strategy_id"]): r["last_bar_ts"]
                 for r in cur.execute("SELECT strategy_id, last_bar_ts FROM strategy_bar_state")}

    q = """
    SELECT t.signal_id, t.symbol, t.direction, t.qty, t.entry_fill, t.stop_price, t.target_price,
           t.exit_fill, t.pnl_dollars, t.status, t.opened_at, t.closed_at, t.strategy_id,
           t.strategy_family, t.exit_reason, t.ibkr_order_id,
           s.entry AS intended_entry, s.stop AS intended_stop, s.target AS intended_target,
           s.ts_utc AS signal_ts, s.strategy_id AS sig_strategy_id, s.strategy_family AS sig_family
    FROM trades t JOIN signals s ON s.id = t.signal_id
    """
    rows = []
    for src, r in enumerate(list(cur.execute(q)) + list(cur.execute(
            q.replace("FROM trades t", "FROM trades_archive t")))):
        d = dict(r)
        d["src"] = "trades" if src < 795 else "trades_archive"
        rows.append(d)

    # ---- build analysis rows ----
    out_rows = []
    pnl_recon = {"match_fill": 0, "match_intended": 0, "match_neither": 0}
    for d in rows:
        sym = (d["symbol"] or "").upper()
        if sym not in SPEC:
            continue
        tick = SPEC[sym]["tick"]
        tval = SPEC[sym]["tick_value"]
        direction = (d["direction"] or "").lower()
        sign = 1.0 if direction == "long" else -1.0
        intended = d["intended_entry"]
        fill = d["entry_fill"]
        if intended is None or fill is None:
            continue
        adv_pts = (fill - intended) * sign          # >0 == adverse (paid up / sold down)
        adv_ticks = adv_pts / tick if tick else 0.0
        adv_dollars = adv_ticks * tval * max(int(d["qty"] or 1), 1)
        stop = d["intended_stop"]
        R_price = abs(intended - stop) if stop is not None else None
        R_dollars = R_price * tval if R_price is not None else None
        # risk actually taken from the fill (stop stays anchored to the intended entry)
        risk_from_fill = abs(fill - stop) if stop is not None else None
        div_R = (adv_pts / R_price) if (R_price and R_price > 0) else None
        qty = max(int(d["qty"] or 1), 1)
        pnl_c = float(d["pnl_dollars"] or 0.0) / qty
        # reconstruct pnl from prices to see which entry price the ledger used
        recon_fill = recon_int = None
        if d["exit_fill"] is not None:
            dpp = tval / tick
            recon_fill = (float(d["exit_fill"]) - float(fill)) * sign * qty * dpp
            recon_int = (float(d["exit_fill"]) - float(intended)) * sign * qty * dpp
        if recon_fill is not None:
            if abs(recon_fill - float(d["pnl_dollars"] or 0.0)) < 0.011:
                pnl_recon["match_fill"] += 1
            elif abs(recon_int - float(d["pnl_dollars"] or 0.0)) < 0.011:
                pnl_recon["match_intended"] += 1
            else:
                pnl_recon["match_neither"] += 1
        sid = d["strategy_id"] or d["sig_strategy_id"]
        ts = d["opened_at"] or ""
        hour_utc = int(ts[11:13]) if len(ts) >= 13 else None
        hour_et = None if hour_utc is None else (hour_utc - 4) % 24  # EDT (UTC-4) in October
        out_rows.append({
            "src": d["src"], "symbol": sym, "direction": direction, "qty": qty,
            "intended": intended, "fill": fill, "adv_pts": adv_pts, "adv_ticks": adv_ticks,
            "adv_dollars": adv_dollars, "R_price": R_price, "R_dollars": R_dollars,
            "risk_from_fill": risk_from_fill, "div_R": div_R,
            "pnl": float(d["pnl_dollars"] or 0.0), "pnl_per_contract": pnl_c,
            "status": d["status"], "opened_at": ts, "hour_et": hour_et,
            "strategy_id": sid, "family": d["strategy_family"] or d["sig_family"] or "unknown",
            "engine": engine_by_strategy.get(int(sid), "not_in_active") if sid else "unknown",
            "exit_reason": d["exit_reason"], "bar_state_ts": bar_state.get(int(sid)) if sid else None,
            "signal_ts": d["signal_ts"],
        })

    legal = [r for r in out_rows if r["symbol"] in LEGAL]
    banned = [r for r in out_rows if r["symbol"] in BANNED]

    report: dict = {
        "generated_utc": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "counts": {
            "joined_all": len(out_rows), "legal": len(legal), "banned_control": len(banned),
            "pnl_reconstruction": pnl_recon,
        },
    }

    # ---- (A) distribution per symbol ----
    dist = {}
    for sym in LEGAL:
        rs = [r for r in legal if r["symbol"] == sym]
        signed = [r["adv_ticks"] for r in rs]
        adverse_only = [max(0.0, r["adv_ticks"]) for r in rs]
        adv_R = [r["div_R"] for r in rs if r["div_R"] is not None]
        adv_dollars = [r["adv_dollars"] for r in rs]
        d = {
            "n": len(rs),
            "ticks_signed": {f"p{q}": pct(signed, q) for q in (0.5, 0.75, 0.9, 0.95, 0.99)},
            "ticks_max": round(max(signed), 2) if signed else None,
            "ticks_adverse_only": {f"p{q}": pct(adverse_only, q) for q in (0.5, 0.75, 0.9, 0.95, 0.99)},
            "adverse_count": sum(1 for r in rs if r["adv_ticks"] > 0),
            "favorable_count": sum(1 for r in rs if r["adv_ticks"] < 0),
            "flat_count": sum(1 for r in rs if r["adv_ticks"] == 0),
            "mean_adv_dollars": round(sum(adv_dollars) / len(rs), 2) if rs else None,
            "sum_adv_dollars": round(sum(adv_dollars), 2),
            "divR_signed": {f"p{q}": pct(adv_R, q) for q in (0.5, 0.75, 0.9, 0.95, 0.99)},
            "share_exceed_R": {
                "0.25R": round(sum(1 for x in adv_R if x > 0.25) / len(rs), 3) if rs else None,
                "0.5R": round(sum(1 for x in adv_R if x > 0.5) / len(rs), 3) if rs else None,
                "1.0R": round(sum(1 for x in adv_R if x > 1.0) / len(rs), 3) if rs else None,
            },
            "realized": summarise(rs),
        }
        dist[sym] = d
    report["A_distribution"] = dist

    # ---- (B) counterfactual ----
    ks_ticks = [1, 2, 5, 10, 20, 50, 100]
    ks_R = [0.25, 0.5, 1.0]

    def cf(rs, keep_fn, label):
        kept = [r for r in rs if keep_fn(r)]
        rej = [r for r in rs if not keep_fn(r)]
        k = summarise(kept)
        j = summarise(rej)
        base = summarise(rs)
        return {
            "threshold": label,
            "kept": k, "rejected": j, "all": base,
            "delta_net_$": round(k.get("net_$", 0) - base.get("net_$", 0), 2),
            "delta_$/contract": round(k.get("$/contract", 0) - base.get("$/contract", 0), 2),
            "keep_rate": round(k.get("n", 0) / base.get("n", 1), 3),
        }

    book = []
    for k in ks_ticks:
        book.append(cf(legal, lambda r, k=k: r["adv_ticks"] <= k, f"{k} ticks"))
    for k in ks_R:
        book.append(cf(legal, lambda r, k=k: (r["div_R"] is None or r["div_R"] <= k), f"{k}R"))
    report["B_counterfactual_book"] = book

    per_sym_cf = {}
    for sym in LEGAL:
        rs = [r for r in legal if r["symbol"] == sym]
        rows_out = []
        for k in ks_ticks:
            rows_out.append(cf(rs, lambda r, k=k: r["adv_ticks"] <= k, f"{k} ticks"))
        for k in ks_R:
            rows_out.append(cf(rs, lambda r, k=k: (r["div_R"] is None or r["div_R"] <= k), f"{k}R"))
        per_sym_cf[sym] = rows_out
    report["B_counterfactual_per_symbol"] = per_sym_cf

    # ---- (C) attribution ----
    def group_stats(rs, keyfn):
        g = defaultdict(list)
        for r in rs:
            g[keyfn(r)].append(r)
        return {k: summarise(v) for k, v in sorted(g.items(), key=lambda kv: -len(kv[1]))}

    report["C_by_family"] = group_stats(legal, lambda r: r["family"])
    report["C_by_engine"] = group_stats(legal, lambda r: r["engine"])

    def session(h):
        if h is None:
            return "unknown"
        if h < 4:
            return "overnight(ET 00-03)"
        if h < 9:
            return "pre-London(ET 04-08)"
        if h < 12:
            return "London(ET 09-11)"
        if h < 16:
            return "NY-AM(ET 12-15)"
        if h < 20:
            return "NY-PM(ET 16-19)"
        return "off-hours(ET 20-23)"

    report["C_by_session"] = group_stats(legal, lambda r: session(r["hour_et"]))
    report["C_session_order"] = ["overnight(ET 00-03)", "pre-London(ET 04-08)", "London(ET 09-11)",
                                  "NY-AM(ET 12-15)", "NY-PM(ET 16-19)", "off-hours(ET 20-23)", "unknown"]

    # divergence by engine x session (mean adverse ticks + n)
    cross = defaultdict(list)
    for r in legal:
        cross[(r["engine"], session(r["hour_et"]))].append(r["adv_ticks"])
    report["C_engine_session_adv_ticks"] = {
        f"{k[0]}|{k[1]}": {"n": len(v), "mean_adv_ticks": round(sum(v) / len(v), 2),
                           "p50": pct(v, 0.5), "p90": pct(v, 0.9)}
        for k, v in sorted(cross.items())}

    # ---- (C2) latency proxy: signal bar ts vs fill ts ----
    lat = []
    for r in legal:
        st = r.get("signal_ts")
        if not st:
            continue
        lat.append((st, r["opened_at"], r["adv_ticks"], r["symbol"], r["family"]))
    report["C_latency_rows"] = len(lat)

    # ---- (D) budget table ----
    budget = {}
    for sym in LEGAL:
        rs = [r for r in legal if r["symbol"] == sym]
        adverse_only = [max(0.0, r["adv_ticks"]) for r in rs]
        adv_R = sorted([r["div_R"] for r in rs if r["div_R"] is not None])
        budget[sym] = {
            "tick_size": SPEC[sym]["tick"], "tick_value": SPEC[sym]["tick_value"],
            "n": len(rs),
            "p50_div_ticks": pct(adverse_only, 0.5), "p90_div_ticks": pct(adverse_only, 0.9),
            "p99_div_ticks": pct(adverse_only, 0.99), "max_div_ticks": round(max(adverse_only), 2) if adverse_only else None,
            "p50_div_R": pct(adv_R, 0.5), "p90_div_R": pct(adv_R, 0.9), "p99_div_R": pct(adv_R, 0.99),
            "current_v1_slippage_ticks": {"MGC": 1.0, "MNQ": 0.5, "MCL": 1.0, "ZF": 1.0, "6J": 1.0}[sym],
        }
    report["D_budget"] = budget
    report["B_banned_control"] = {s: summarise([r for r in banned if r["symbol"] == s]) for s in BANNED}

    with open("research/entry_slippage_calibration_20261023.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(json.dumps(report, indent=2, default=str)[:12000])


if __name__ == "__main__":
    main()
