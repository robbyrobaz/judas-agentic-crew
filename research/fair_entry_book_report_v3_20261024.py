"""FAIR ENTRY BOOK REMEASURE -- #2869 deliverable (v3, 2026-10-24).

Aggregates the v3 restamp (base / fair / live) into:

  (A) the headline N / M / K / U over the 175 actives
  (B) the per-active table (archive-covered population)
  (D) the two 9124-style failure markers
  (E) the honest caveat text

READ-ONLY w.r.t. registry and account. Writes only outputs/*.csv|json.
"""
from __future__ import annotations

import csv
import glob
import json
import sqlite3
from pathlib import Path

DB = "judas_crew.db"
POP = "outputs/fair_book_pop_20261022.json"
V3 = "outputs/fair_entry_book_v3_20261024.worker*.json"
STALE_SCAN = "outputs/stale_fill_scan_20261022.json"
TABLE = "outputs/fair_entry_book_table_20261024.csv"
SUMMARY = "outputs/fair_entry_book_summary_20261024.json"
BONF_N = 175


def load_json(path, default=None):
    try:
        return json.load(open(path))
    except Exception:  # noqa: BLE001
        return default


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    live = {}
    for r in con.execute(
        "SELECT strategy_id, COUNT(*) n, SUM(CASE WHEN pnl_dollars>0 THEN 1 ELSE 0 END) w, "
        "SUM(COALESCE(pnl_dollars,0)) pnl FROM trades "
        "WHERE status='closed' AND strategy_id IS NOT NULL GROUP BY strategy_id"
    ):
        live[r["strategy_id"]] = dict(r)
    acts = con.execute(
        "SELECT id, symbol, strategy_family, version, params_json, metrics_json, "
        "source_candidate_id, activated_at_utc FROM active_strategies "
        "WHERE state='active' ORDER BY id").fetchall()
    con.close()

    pop = load_json(POP, {"rows": [], "units": []})
    rows_by_id = {r["id"]: r for r in pop["rows"]}
    unit_by_key = {(str(u["csid"]), u["symbol"], u["tf"]): u for u in pop["units"]}

    # ---- v3 restamp rows (base/fair/live) ---------------------------------
    v3 = {}
    for p in sorted(glob.glob(V3)):
        try:
            for r in json.load(open(p))["rows"]:
                v3[(str(r.get("csid")), r.get("symbol"), r.get("tf"))] = r
        except Exception as exc:  # noqa: BLE001
            print("part unreadable", p, exc)

    # ---- 07f3a4c2 stale-fill scan (per symbol/csid/tf) ---------------------
    stale = {}
    sc = load_json(STALE_SCAN, {}) or {}
    for r in (sc.get("rows") or []):
        stale[(str(r.get("csid")), r.get("symbol"), r.get("tf"))] = r

    table = []
    for a in acts:
        aid = a["id"]
        pr = rows_by_id.get(aid, {})
        try:
            m = json.loads(a["metrics_json"] or "{}")
        except Exception:  # noqa: BLE001
            m = {}
        p = json.loads(a["params_json"] or "{}")
        csid = pr.get("csid", p.get("custom_strategy_id"))
        tf = pr.get("tf") or p.get("timeframe")
        key = (str(csid), a["symbol"], tf)
        rec = v3.get(key)
        st = stale.get(key) or {}
        f = (rec or {}).get("FAIR") or {}
        b = (rec or {}).get("BASE") or {}
        lv = (rec or {}).get("LIVE") or {}
        bm = (rec or {}).get("BASE_meta") or {}

        lvst = live.get(aid)
        live_n = lvst["n"] if lvst else 0
        live_pnl = lvst["pnl"] if lvst else None
        live_usd_tr = (live_pnl / live_n) if (live_pnl is not None and live_n) else None
        live_wr = (100.0 * lvst["w"] / live_n) if (live_n) else None
        # metrics_json is NOT type-safe: many rows carry numeric fields as
        # STRINGS (finding #9135: 301 string-typed rows), so a bare
        # `bt_pnl / bt_n` raises TypeError. Coerce defensively.
        def _num(v):
            if v is None or isinstance(v, bool):
                return None
            if isinstance(v, (int, float)):
                return float(v)
            try:
                return float(str(v).strip().replace(",", "").replace("$", ""))
            except (TypeError, ValueError):
                return None

        bt_n = _num(m.get("n_trades"))
        bt_pf = _num(m.get("pf"))
        bt_pnl = _num(m.get("total_pnl"))
        bt_usd_tr = (bt_pnl / bt_n) if (bt_n and bt_pnl is not None and bt_n > 0) else None
        ratio_bt = (live_usd_tr / bt_usd_tr) if (live_usd_tr is not None and bt_usd_tr) else None
        ratio_fair = (live_usd_tr / f.get("usd_per_trade")) if (
            live_usd_tr is not None and f.get("usd_per_trade")) else None
        p1 = f.get("p_1sided")
        p_bonf = min(1.0, p1 * BONF_N) if p1 is not None else None
        wr_gap = (live_wr - f.get("wr_pct")) if (
            live_wr is not None and f.get("wr_pct") is not None) else None

        # port check: does BASE reproduce the registered backtest?
        bt_repro = None
        if b.get("n") and bt_n:
            bt_repro = (int(b["n"]) == int(bt_n)) and (
                not bt_pf or abs(float(b["pf"]) - float(bt_pf)) / max(1e-9, float(bt_pf)) < 0.05)

        # ---- verdict -----------------------------------------------------
        if rec is None:
            v = "NOT-RESTAMPABLE (no archive)"
        elif rec.get("verdict"):
            v = rec["verdict"]
        else:
            timed_out = bool((rec.get("FAIR_meta") or {}).get("timed_out") or
                              (rec.get("LIVE_meta") or {}).get("timed_out"))
            nf = f.get("n", 0)
            pff = f.get("pf", 0)
            pfl = lv.get("pf", 0)
            if timed_out and nf < 50:
                v = "INCONCLUSIVE (replay timeout)"
            elif nf == 0:
                v = "NO-SIGNALS on archive"
            elif nf < 50:
                v = "THIN (n<50)"
            elif pff > 1.2 and pfl > 1.2:
                v = "DEFENSIBLE"
            elif pff <= 1.0 and pfl <= 1.0:
                v = "DEAD"
            else:
                v = "UNVERIFIED (1.0<PF<=1.2 or mode split)"

        table.append({
            "id": aid, "symbol": a["symbol"], "tf": tf, "csid": csid,
            "family": a["strategy_family"], "covered": pr.get("covered"),
            "stale_pct_07f3a4c2": st.get("stale_pct"),
            "stale_minutes_mean_07f3a4c2": st.get("stale_mean_min"),
            "pct_entry_outside_decision_bar": bm.get("pct_entry_outside_decision_bar"),
            "pct_entry_is_decision_close": bm.get("pct_entry_is_decision_close"),
            "restamp_n": f.get("n"), "restamp_pf": f.get("pf"), "restamp_wr": f.get("wr_pct"),
            "restamp_usd_per_trade": f.get("usd_per_trade"), "restamp_e_r": f.get("e_r"),
            "restamp_total_usd": f.get("total_usd"), "restamp_maxdd": f.get("max_dd"),
            "restamp_p": p1, "p_bonferroni": p_bonf,
            "live_model_n": lv.get("n"), "live_model_pf": lv.get("pf"),
            "live_model_wr": lv.get("wr_pct"), "live_model_usd_per_trade": lv.get("usd_per_trade"),
            "live_model_e_r": lv.get("e_r"), "live_model_total_usd": lv.get("total_usd"),
            "base_n": b.get("n"), "base_pf": b.get("pf"), "base_wr": b.get("wr_pct"),
            "base_usd_per_trade": b.get("usd_per_trade"),
            "pf_inflation_base_over_fair": (rec or {}).get("pf_inflation"),
            "pf_inflation_base_over_live": (rec or {}).get("pf_inflation_live"),
            "bt_n": bt_n, "bt_pf": bt_pf, "bt_pnl": bt_pnl,
            "bt_cost_model": m.get("cost_model"), "bt_reproduced_by_BASE": bt_repro,
            "cand": a["source_candidate_id"],
            "live_n": live_n, "live_pnl": live_pnl,
            "live_usd_per_trade": round(live_usd_tr, 2) if live_usd_tr is not None else None,
            "live_wr": round(live_wr, 1) if live_wr is not None else None,
            "ratio_live_over_BT": round(ratio_bt, 2) if ratio_bt is not None else None,
            "ratio_live_over_FAIR": round(ratio_fair, 2) if ratio_fair is not None else None,
            "wr_gap_live_minus_restamp": round(wr_gap, 1) if wr_gap is not None else None,
            "MARKER1_ratio_gt_10x": bool(ratio_bt is not None and ratio_bt > 10.0),
            "MARKER2_wr_gap_gt_20pt": bool(wr_gap is not None and wr_gap > 20.0 and live_n >= 10),
            "span_days": (rec or {}).get("span_days"),
            "archive_bars": (rec or {}).get("archive_bars"),
            "verdict": v,
        })

    def cnt(pred):
        return sum(1 for r in table if pred(r))

    summary = {
        "n_actives": len(table),
        "n_measured_units": len(v3),
        "N_defensible": cnt(lambda r: r["verdict"] == "DEFENSIBLE"),
        "M_dead": cnt(lambda r: r["verdict"] == "DEAD"),
        "K_unmeasurable": cnt(lambda r: r["verdict"].startswith("NOT-RESTAMPABLE")
                              or r["verdict"].startswith("NO-SIGNALS")
                              or r["verdict"].startswith("NO_CODE")),
        "U_unverified": cnt(lambda r: r["verdict"].startswith("UNVERIFIED")
                            or r["verdict"].startswith("THIN")
                            or r["verdict"].startswith("INCONCLUSIVE")),
        "verdict_breakdown": {},
        "by_symbol_tf": {},
        "marker1_rows": [r["id"] for r in table if r["MARKER1_ratio_gt_10x"]],
        "marker2_rows": [r["id"] for r in table if r["MARKER2_wr_gap_gt_20pt"]],
        "bt_reproduced_rows": cnt(lambda r: r["bt_reproduced_by_BASE"] is True),
        "bt_reproduced_checked": cnt(lambda r: r["bt_reproduced_by_BASE"] is not None),
        "cost_model": "v1_realistic_micros",
        "fill_models": {"BASE": "signal's own entry price (what run_custom_backtest reports)",
                        "FAIR": "decision bar close",
                        "LIVE": "next bar open (production semantics, finding #9134)"},
    }
    for r in table:
        summary["verdict_breakdown"][r["verdict"]] = summary["verdict_breakdown"].get(r["verdict"], 0) + 1
        k = f"{r['symbol']} {r['tf']}"
        b = summary["by_symbol_tf"].setdefault(k, {"n": 0})
        b["n"] += 1
        b[r["verdict"]] = b.get(r["verdict"], 0) + 1

    with open(TABLE, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(table[0].keys()))
        w.writeheader()
        w.writerows(table)
    with open(SUMMARY, "w") as fh:
        json.dump(summary, fh, indent=1, default=str)
    print(json.dumps(summary, indent=1, default=str))
    print(f"\nwrote {TABLE} ({len(table)} rows) and {SUMMARY}")


if __name__ == "__main__":
    main()
