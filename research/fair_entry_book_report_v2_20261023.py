"""Aggregate the fair-entry restamp (v2) parts into the #2869 deliverable.

(A) headline N/M/K/U over the 175 actives
(B) per-active table
(D) the two 9124-style failure markers
(E) explicit caveat text

READ-ONLY w.r.t. the registry and the account. Writes only JSON/CSV under outputs/.
"""
from __future__ import annotations

import csv
import glob
import json
import math
import sqlite3
from pathlib import Path

DB = "judas_crew.db"
POP = "outputs/fair_book_pop_20261022.json"
PARTS = "outputs/fair_entry_book_v2_20261023.part*"
# The resumable worker writes `worker{n}.json` parts; they carry the same
# row schema and MUST be included or the population looks half-measured.
PARTS_EXTRA = "outputs/fair_entry_book_v2_20261023.worker*.json"
LIMIT_PARTS = "outputs/fair_entry_limit_20261023.part*"
TABLE = "outputs/fair_entry_book_table_20261023.csv"
SUMMARY = "outputs/fair_entry_book_summary_20261023.json"
BONF_N = 175


def load_parts(pattern):
    rows = []
    for p in sorted(glob.glob(pattern)):
        try:
            rows.extend(json.load(open(p))["rows"])
        except Exception as exc:  # noqa: BLE001
            print("part unreadable", p, exc)
    return rows


def live_stats(con):
    out = {}
    for r in con.execute(
        "SELECT strategy_id, COUNT(*) n, SUM(CASE WHEN pnl_dollars>0 THEN 1 ELSE 0 END) w, "
        "SUM(COALESCE(pnl_dollars,0)) pnl FROM trades "
        "WHERE status='closed' AND strategy_id IS NOT NULL GROUP BY strategy_id"
    ):
        out[r["strategy_id"]] = dict(r)
    return out


def verdict(rec, f):
    """DEFENSIBLE / DEAD / UNVERIFIED / NOT-RESTAMPABLE / INCONCLUSIVE."""
    if rec is None:
        return "NOT-RESTAMPABLE (no archive)"
    if rec.get("verdict"):
        return rec["verdict"]
    meta = rec.get("FAIR_meta") or {}
    if meta.get("timed_out"):
        return "INCONCLUSIVE (replay timeout)"
    n = f.get("n", 0)
    if n == 0:
        return "NO-SIGNALS on archive"
    if n < 50:
        return "THIN (n<50)"
    if f.get("pf", 0) > 1.2:
        return "DEFENSIBLE"
    if f.get("pf", 0) <= 1.0:
        return "DEAD"
    return "UNVERIFIED (1.0<PF<=1.2)"


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    live = live_stats(con)
    acts = con.execute(
        "SELECT id, symbol, strategy_family, version, params_json, metrics_json, "
        "source_candidate_id, activated_at_utc FROM active_strategies "
        "WHERE state='active' ORDER BY id").fetchall()
    bt = {}
    for a in acts:
        try:
            m = json.loads(a["metrics_json"] or "{}")
        except Exception:  # noqa: BLE001
            m = {}
        p = json.loads(a["params_json"] or "{}")
        bt[a["id"]] = {
            "csid": p.get("custom_strategy_id"),
            "tf": p.get("timeframe"),
            "engine": p.get("execution_engine"),
            "cost_model": m.get("cost_model"),
            "bt_n": m.get("n_trades"),
            "bt_pf": m.get("pf"),
            "bt_pnl": m.get("total_pnl"),
            "bt_wr": m.get("winrate"),
            "cand": a["source_candidate_id"],
        }
    con.close()

    pop = json.load(open(POP))
    rows_by_id = {r["id"]: r for r in pop["rows"]}
    units = {tuple(u["ids"]): u for u in pop["units"]}

    rest = load_parts(PARTS) + load_parts(PARTS_EXTRA)
    by_key = {(str(r.get("csid")), r.get("symbol"), r.get("tf")): r for r in rest}
    lim = load_parts(LIMIT_PARTS)
    lim_by_key = {(str(r.get("csid")), r.get("symbol"), r.get("tf")): r for r in lim}

    table = []
    for a in acts:
        aid = a["id"]
        pr = rows_by_id.get(aid, {})
        key = (str(bt[aid]["csid"]), a["symbol"], pr.get("tf") or bt[aid]["tf"])
        rec = by_key.get(key)
        lrec = lim_by_key.get(key)
        f = (rec or {}).get("FAIR") or {}
        b = (rec or {}).get("BASE") or {}
        l = (lrec or {}).get("LIMIT") or {}
        bm = (rec or {}).get("BASE_meta") or {}
        v = verdict(rec, f)
        lv = live.get(aid)
        live_usd_tr = (lv["pnl"] / lv["n"]) if lv and lv["n"] else None
        live_wr = (100.0 * lv["w"] / lv["n"]) if lv and lv["n"] else None
        bt_usd_tr = (bt[aid]["bt_pnl"] / bt[aid]["bt_n"]) if bt[aid].get("bt_n") else None
        ratio_bt = (live_usd_tr / bt_usd_tr) if (live_usd_tr is not None and bt_usd_tr) else None
        ratio_fair = (live_usd_tr / f.get("usd_per_trade")) if (
            live_usd_tr is not None and f.get("usd_per_trade")) else None
        p1 = f.get("p_1sided")
        p_bonf = min(1.0, p1 * BONF_N) if p1 is not None else None
        wr_gap = (live_wr - f.get("wr_pct")) if (live_wr is not None and f.get("wr_pct") is not None) else None
        m1 = bool(ratio_bt is not None and ratio_bt > 10.0)
        m2 = bool(wr_gap is not None and wr_gap > 20.0 and lv and lv["n"] >= 10)
        table.append({
            "id": aid, "symbol": a["symbol"], "tf": pr.get("tf") or bt[aid]["tf"],
            "csid": bt[aid]["csid"], "family": a["strategy_family"],
            "covered": pr.get("covered"),
            "stale_pct": bm.get("pct_entry_outside_decision_bar"),
            "pct_entry_is_close": bm.get("pct_entry_is_decision_close"),
            "pct_stale_gt_2": bm.get("pct_stale_gt_2"),
            "restamp_n": f.get("n"), "restamp_pf": f.get("pf"),
            "restamp_wr": f.get("wr_pct"), "restamp_usd_per_trade": f.get("usd_per_trade"),
            "restamp_e_r": f.get("e_r"), "restamp_total_usd": f.get("total_usd"),
            "restamp_maxdd": f.get("max_dd"), "restamp_p": p1, "p_bonferroni": p_bonf,
            "base_n": b.get("n"), "base_pf": b.get("pf"), "base_wr": b.get("wr_pct"),
            "base_usd_per_trade": b.get("usd_per_trade"),
            "limit_n": l.get("n"), "limit_pf": l.get("pf"),
            "limit_wr": l.get("wr_pct"), "limit_usd_per_trade": l.get("usd_per_trade"),
            "bt_n": bt[aid]["bt_n"], "bt_pf": bt[aid]["bt_pf"], "bt_pnl": bt[aid]["bt_pnl"],
            "bt_cost_model": bt[aid]["cost_model"], "cand": bt[aid]["cand"],
            "live_n": lv["n"] if lv else 0, "live_pnl": lv["pnl"] if lv else None,
            "live_usd_per_trade": round(live_usd_tr, 2) if live_usd_tr is not None else None,
            "live_wr": round(live_wr, 1) if live_wr is not None else None,
            "ratio_live_over_BT": round(ratio_bt, 2) if ratio_bt is not None else None,
            "ratio_live_over_FAIR": round(ratio_fair, 2) if ratio_fair is not None else None,
            "wr_gap_live_minus_restamp": round(wr_gap, 1) if wr_gap is not None else None,
            "MARKER1_ratio_gt_10x": m1, "MARKER2_wr_gap_gt_20pt": m2,
            "span_days": (rec or {}).get("span_days"),
            "archive_bars": (rec or {}).get("archive_bars"),
            "timed_out": (bm.get("timed_out")),
            "verdict": v,
        })

    def cnt(pred):
        return sum(1 for r in table if pred(r))

    summary = {
        "n_actives": len(table),
        "N_defensible": cnt(lambda r: r["verdict"] == "DEFENSIBLE"),
        "M_dead": cnt(lambda r: r["verdict"] == "DEAD"),
        "K_unmeasurable_not_restampable": cnt(lambda r: r["verdict"].startswith("NOT-RESTAMPABLE")
                                              or r["verdict"].startswith("NO-SIGNALS")),
        "U_unverified_measurable": cnt(lambda r: r["verdict"].startswith("UNVERIFIED")
                                       or r["verdict"].startswith("THIN")
                                       or r["verdict"].startswith("INCONCLUSIVE")),
        "verdict_breakdown": {},
        "by_symbol_tf": {},
        "marker1_rows": [r["id"] for r in table if r["MARKER1_ratio_gt_10x"]],
        "marker2_rows": [r["id"] for r in table if r["MARKER2_wr_gap_gt_20pt"]],
        "cost_model": "v1_realistic_micros",
    }
    for r in table:
        summary["verdict_breakdown"][r["verdict"]] = summary["verdict_breakdown"].get(r["verdict"], 0) + 1
        k = f"{r['symbol']} {r['tf']}"
        b = summary["by_symbol_tf"].setdefault(k, {"n": 0, "DEFENSIBLE": 0, "DEAD": 0,
                                                   "UNVERIFIED": 0, "THIN": 0,
                                                   "NOT-RESTAMPABLE": 0, "INCONCLUSIVE": 0,
                                                   "NO-SIGNALS": 0})
        b["n"] += 1
        for v in ("DEFENSIBLE", "DEAD", "UNVERIFIED", "THIN", "NOT-RESTAMPABLE",
                  "INCONCLUSIVE", "NO-SIGNALS"):
            if r["verdict"].startswith(v):
                b[v] += 1
    with open(TABLE, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(table[0].keys()))
        w.writeheader()
        w.writerows(table)
    with open(SUMMARY, "w") as fh:
        json.dump(summary, fh, indent=1, default=str)
    print(json.dumps(summary, indent=1, default=str)[:4000])
    print(f"\nwrote {TABLE} ({len(table)} rows) and {SUMMARY}")


if __name__ == "__main__":
    main()
