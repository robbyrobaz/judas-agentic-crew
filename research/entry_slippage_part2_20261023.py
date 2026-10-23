"""Task #2873 part 2 — (B2) entry-divergence P&L decomposition, (C2) reference-age
(latency) test on archived bar cells, (B3) per-symbol counterfactual table.

Read-only. Uses data/bars_archive/*.parquet (real archived bars) + judas_crew.db (ro).
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
    "MBT": {"tick": 5.0, "tick_value": 0.5},
    "MET": {"tick": 0.50, "tick_value": 0.05},
    "DX": {"tick": 0.005, "tick_value": 5.0},
}
BAR_SEC = {"5m": 300, "15m": 900, "1h": 3600}


def pct(v, q):
    if not v:
        return None
    v = sorted(v)
    if len(v) == 1:
        return round(v[0], 4)
    pos = (len(v) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (pos - lo), 4)


def summ(rs):
    if not rs:
        return {"n": 0}
    p = [r["pnl_c"] for r in rs]
    gw = sum(x for x in p if x > 0)
    gl = -sum(x for x in p if x < 0)
    return {
        "n": len(rs), "W": sum(1 for x in p if x > 0), "L": sum(1 for x in p if x <= 0),
        "PF": round(gw / gl, 3) if gl > 0 else None,
        "net_$": round(sum(p), 2), "$/contract": round(sum(p) / len(p), 2),
    }


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    strat_meta = {}
    for r in cur.execute("SELECT id, symbol, strategy_family, params_json FROM active_strategies"):
        try:
            pj = json.loads(r["params_json"] or "{}")
        except Exception:
            pj = {}
        strat_meta[int(r["id"])] = {
            "symbol": r["symbol"], "family": r["strategy_family"],
            "engine": pj.get("execution_engine", "unknown"),
            "tf": pj.get("timeframe"), "csid": pj.get("custom_strategy_id"),
        }

    q = """SELECT t.signal_id,t.symbol,t.direction,t.qty,t.entry_fill,t.stop_price,t.exit_fill,
                  t.pnl_dollars,t.status,t.opened_at,t.strategy_id,t.strategy_family,t.exit_reason,
                  s.entry intended,s.stop intended_stop,s.ts_utc
           FROM {tab} t JOIN signals s ON s.id=t.signal_id"""
    raw = []
    for tab in ("trades", "trades_archive"):
        for r in cur.execute(q.format(tab=tab)):
            raw.append(dict(r))

    rows = []
    for d in raw:
        sym = (d["symbol"] or "").upper()
        if sym not in LEGAL:
            continue
        if d["intended"] is None or d["entry_fill"] is None:
            continue
        spec = SPEC[sym]
        tick, tval = spec["tick"], spec["tick_value"]
        sign = 1.0 if (d["direction"] or "").lower() == "long" else -1.0
        qty = max(int(d["qty"] or 1), 1)
        dpp = tval / tick
        E = float(d["intended"])
        F = float(d["entry_fill"])
        adv_pts = (F - E) * sign
        adv_ticks = adv_pts / tick
        R_price = abs(E - float(d["intended_stop"])) if d["intended_stop"] is not None else None
        pnl_real = float(d["pnl_dollars"] or 0.0)
        pnl_c = pnl_real / qty
        pnl_at_intended = None
        if d["exit_fill"] is not None:
            pnl_at_intended = (float(d["exit_fill"]) - E) * sign * qty * dpp
        sid = d["strategy_id"]
        meta = strat_meta.get(int(sid), {}) if sid else {}
        rows.append({
            "symbol": sym, "ts": d["opened_at"], "direction": d["direction"], "qty": qty,
            "intended": E, "fill": F, "adv_ticks": adv_ticks, "adv_pts": adv_pts,
            "adv_dollars": adv_ticks * tval * qty,
            "R_price": R_price, "div_R": (adv_pts / R_price) if R_price else None,
            "pnl": pnl_real, "pnl_c": pnl_c, "pnl_at_intended": pnl_at_intended,
            "entry_cost": (pnl_real - pnl_at_intended) if pnl_at_intended is not None else None,
            "family": d["strategy_family"] or meta.get("family") or "unknown",
            "engine": meta.get("engine", "not_in_active"), "tf": meta.get("tf"),
            "csid": meta.get("csid"), "strategy_id": sid, "exit_reason": d["exit_reason"],
        })

    out = {"n_legal_fills": len(rows)}

    # ---------- (B2) entry-divergence P&L decomposition ----------
    def agg(rs, key):
        g = defaultdict(list)
        for r in rs:
            g[key(r)].append(r)
        res = {}
        for k, v in sorted(g.items(), key=lambda kv: -len(kv[1])):
            ec = [x["entry_cost"] for x in v if x["entry_cost"] is not None]
            s = summ(v)
            s["entry_cost_sum_$"] = round(sum(ec), 2) if ec else None
            s["entry_cost_mean_$"] = round(sum(ec) / len(ec), 2) if ec else None
            s["entry_cost_median_$"] = pct(ec, 0.5)
            s["entry_cost_p90_$"] = pct(ec, 0.9)
            res[str(k)] = s
        return res

    out["B2_by_symbol"] = agg(rows, lambda r: r["symbol"])
    out["B2_by_engine"] = agg(rows, lambda r: r["engine"])
    out["B2_by_family"] = agg(rows, lambda r: r["family"])
    all_ec = [r["entry_cost"] for r in rows if r["entry_cost"] is not None]
    out["B2_book"] = {
        "sum_entry_cost_$": round(sum(all_ec), 2),
        "mean_entry_cost_$/contract": round(sum(all_ec) / len(rows), 2),
        "median_entry_cost_$/contract": pct(all_ec, 0.5),
        "p90_entry_cost_$/contract": pct(all_ec, 0.9),
        "realized_net_$": round(sum(r["pnl"] for r in rows), 2),
        "pnl_if_filled_at_intended_$": round(sum(r["pnl_at_intended"] for r in rows), 2),
    }

    # ---------- (B3) per-symbol counterfactual ----------
    def cf(rs, keep):
        kept = [r for r in rs if keep(r)]
        rej = [r for r in rs if not keep(r)]
        k, j, base = summ(kept), summ(rej), summ(rs)
        return {"kept": k, "rejected": j, "all": base,
                "delta_net_$": round(k.get("net_$", 0) - base.get("net_$", 0), 2),
                "delta_$/c": round(k.get("$/contract", 0) - base.get("$/contract", 0), 2)}

    cf_tab = {}
    for sym in LEGAL:
        rs = [r for r in rows if r["symbol"] == sym]
        e = {}
        for k in (1, 2, 5, 10, 20, 50, 100):
            e[f"{k}t"] = cf(rs, lambda r, k=k: r["adv_ticks"] <= k)
        for k in (0.25, 0.5, 1.0):
            e[f"{k}R"] = cf(rs, lambda r, k=k: (r["div_R"] is None or r["div_R"] <= k))
        cf_tab[sym] = e
    out["B3_per_symbol_cf"] = cf_tab

    # ---------- (C2) reference-age (latency) test on archived cells ----------
    bars_cache = {}

    def get_bars(sym, tf):
        key = f"{sym}_{tf}"
        if key in bars_cache:
            return bars_cache[key]
        p = os.path.join(ARCH, key + ".parquet")
        if not os.path.exists(p):
            bars_cache[key] = None
            return None
        df = pd.read_parquet(p)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.sort_values("ts").reset_index(drop=True)
        bars_cache[key] = df
        return df

    age_rows = []
    missing_cell = defaultdict(int)
    for r in rows:
        tf = r["tf"]
        sym = r["symbol"]
        if tf not in BAR_SEC:
            missing_cell[f"{sym}:{tf}"] += 1
            continue
        df = get_bars(sym, tf)
        if df is None:
            missing_cell[f"{sym}:{tf}"] += 1
            continue
        t = pd.Timestamp(r["ts"].replace("Z", "+00:00"))
        idx = df.index[df["ts"] <= t]
        if len(idx) == 0:
            missing_cell[f"{sym}:{tf}"] += 1
            continue
        i = int(idx[-1])
        tick = SPEC[sym]["tick"]
        E = r["intended"]
        # age of the bar whose CLOSE equals the intended entry price
        age = None
        for j in range(i, max(-1, i - 400), -1):
            if abs(float(df.at[j, "close"]) - E) <= 0.5 * tick:
                age = i - j
                break
        # alternative: any of open/high/low of the last 30 bars
        age_any = None
        for j in range(i, max(-1, i - 30), -1):
            if any(abs(float(df.at[j, c]) - E) <= 0.5 * tick for c in ("open", "high", "low", "close")):
                age_any = i - j
                break
        close_i = float(df.at[i, "close"])
        age_rows.append({
            "symbol": sym, "tf": tf, "family": r["family"], "engine": r["engine"],
            "adv_ticks": r["adv_ticks"], "adv_dollars": r["adv_dollars"],
            "age_close_bars": age, "age_any_bars": age_any,
            "E_minus_lastclose_ticks": (E - close_i) / tick,
            "fill_minus_lastclose_ticks": (r["fill"] - close_i) / tick,
            "pnl_c": r["pnl_c"], "entry_cost": r["entry_cost"],
        })

    out["C2_age_rows"] = len(age_rows)
    out["C2_missing_cells"] = dict(missing_cell)
    if age_rows:
        ages = [a["age_close_bars"] for a in age_rows if a["age_close_bars"] is not None]
        ages_any = [a["age_any_bars"] for a in age_rows if a["age_any_bars"] is not None]
        out["C2_age_summary"] = {
            "n_with_close_match": len(ages),
            "n_total": len(age_rows),
            "close_age_bars": {f"p{q}": pct(ages, q) for q in (0.5, 0.75, 0.9, 0.95, 0.99)},
            "close_age_max": max(ages) if ages else None,
            "any_price_age_bars": {f"p{q}": pct(ages_any, q) for q in (0.5, 0.75, 0.9, 0.95, 0.99)},
            "share_age_0": round(sum(1 for a in ages if a == 0) / len(ages), 3) if ages else None,
            "share_age_ge_6": round(sum(1 for a in ages if a >= 6) / len(ages), 3) if ages else None,
        }
        # correlation: age vs adverse ticks
        xs = [a["age_close_bars"] for a in age_rows if a["age_close_bars"] is not None]
        ys = [a["adv_ticks"] for a in age_rows if a["age_close_bars"] is not None]
        if len(xs) > 2:
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
            den = (sum((a - mx) ** 2 for a in xs) ** 0.5) * (sum((b - my) ** 2 for b in ys) ** 0.5)
            out["C2_corr_age_vs_adv_ticks"] = round(num / den, 3) if den else None
        # age vs entry cost
        ecx = [a["entry_cost"] for a in age_rows if a["entry_cost"] is not None and a["age_close_bars"] is not None]
        if len(ecx) > 2 and len(ecx) == len(xs):
            my = sum(ecx) / len(ecx)
            num = sum((a - mx) * (b - my) for a, b in zip(xs, ecx))
            den = (sum((a - mx) ** 2 for a in xs) ** 0.5) * (sum((b - my) ** 2 for b in ecx) ** 0.5)
            out["C2_corr_age_vs_entry_cost"] = round(num / den, 3) if den else None
        byf = defaultdict(list)
        for a in age_rows:
            byf[(a["symbol"], a["tf"])].append(a)
        out["C2_by_cell"] = {
            f"{k[0]}_{k[1]}": {
                "n": len(v),
                "median_age_bars": pct([x["age_close_bars"] for x in v if x["age_close_bars"] is not None], 0.5),
                "p90_age_bars": pct([x["age_close_bars"] for x in v if x["age_close_bars"] is not None], 0.9),
                "median_adv_ticks": pct([x["adv_ticks"] for x in v], 0.5),
                "median_entry_cost_$": pct([x["entry_cost"] for x in v if x["entry_cost"] is not None], 0.5),
                "sum_entry_cost_$": round(sum(x["entry_cost"] for x in v if x["entry_cost"] is not None), 2),
            } for k, v in sorted(byf.items())}

    with open("research/entry_slippage_part2_20261023.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(json.dumps({k: v for k, v in out.items() if k not in ("B3_per_symbol_cf",)}, indent=2, default=str)[:14000])


if __name__ == "__main__":
    main()
