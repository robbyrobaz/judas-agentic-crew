"""Aggregate the fair-entry restamp parts into the (A)/(B)/(D) deliverable."""
from __future__ import annotations

import glob
import json
import math
from pathlib import Path

OUT = "outputs/fair_entry_book_20261022.json"
POP = "outputs/fair_book_pop_20261022.json"
TABLE = "outputs/fair_entry_book_table_20261022.csv"
SUMMARY = "outputs/fair_entry_book_summary_20261022.json"


def load_parts():
    rows = []
    for p in sorted(glob.glob("outputs/fair_entry_book_20261022.json.part*")):
        try:
            rows.extend(json.load(open(p))["rows"])
        except Exception as exc:  # noqa: BLE001
            print("part unreadable", p, exc)
    return rows


def verdict(r):
    if r.get("verdict"):
        return r["verdict"]
    f = r.get("FAIR") or {}
    meta = (r.get("FAIR_meta") or {})
    if meta.get("timed_out"):
        return "INCONCLUSIVE (replay timeout, partial span)"
    n = f.get("n", 0)
    if n == 0:
        return "NO-SIGNALS on archive"
    if n < 50:
        return "THIN (n<50)"
    if f.get("pf", 0) > 1.2:
        return "DEFENSIBLE"
    if f.get("pf", 0) <= 1.0:
        return "DEAD"
    return "UNVERIFIED"


def main():
    rows = load_parts()
    pop = json.load(open(POP))
    rows_by_id = {r["id"]: r for r in pop["rows"]}
    n_tests = len(pop["units"])

    table = []
    for r in rows:
        f = r.get("FAIR") or {}
        b = r.get("BASE") or {}
        fm = r.get("FAIR_meta") or {}
        bm = r.get("BASE_meta") or {}
        live = {"n": 0, "w": 0, "pnl": 0.0}
        for aid in r.get("ids", []):
            lv = rows_by_id.get(aid, {}).get("live")
            if lv:
                live = {"n": lv["n"], "w": lv["w"], "pnl": lv["pnl"]}
        live_usd_pt = round(live["pnl"] / live["n"], 2) if live["n"] else None
        ratio = (round(live_usd_pt / f["usd_per_trade"], 2)
                 if live_usd_pt is not None and f.get("usd_per_trade") else None)
        p_bonf = min(1.0, f.get("p_1sided", 1.0) * n_tests) if f.get("n") else 1.0
        live_wr = round(100.0 * live["w"] / live["n"], 1) if live["n"] else None
        wr_gap = (round(live_wr - f.get("wr_pct", 0), 1)
                  if live_wr is not None and f.get("n") else None)
        row = {
            "ids": "|".join(str(x) for x in r.get("ids", [])),
            "symbol": r["symbol"], "tf": r["tf"], "csid": r["csid"],
            "span_days": r.get("span_days"), "n_bars": r.get("archive_bars"),
            "pct_entry_outside_bar": bm.get("pct_entry_outside_decision_bar"),
            "pct_entry_is_close": bm.get("pct_entry_is_decision_close"),
            "BASE_n": b.get("n"), "BASE_pf": b.get("pf"), "BASE_wr": b.get("wr_pct"),
            "FAIR_n": f.get("n"), "FAIR_pf": f.get("pf"), "FAIR_wr": f.get("wr_pct"),
            "FAIR_usd_pt": f.get("usd_per_trade"), "FAIR_e_r": f.get("e_r"),
            "FAIR_total": f.get("total_usd"), "FAIR_maxdd": f.get("max_dd"),
            "FAIR_p": f.get("p_1sided"), "p_Bonferroni": round(p_bonf, 5),
            "FAIRNG_pf": (r.get("FAIRNG") or {}).get("pf"),
            "pf_inflation": r.get("pf_inflation"),
            "live_n": live["n"], "live_usd_pt": live_usd_pt, "live_wr": live_wr,
            "live_vs_fair_ratio": ratio, "wr_gap_live_minus_fair": wr_gap,
            "timed_out": bool(fm.get("timed_out")),
            "verdict": verdict(r),
        }
        table.append(row)

    json.dump({"cost_model": rows[0].get("cost_model") if rows else None,
               "n_tests": n_tests, "rows": rows}, open(OUT, "w"), indent=1, default=str)

    import csv
    if table:
        with open(TABLE, "w", newline="") as fh:
            wcsv = csv.DictWriter(fh, fieldnames=list(table[0].keys()))
            wcsv.writeheader()
            wcsv.writerows(table)

    from collections import Counter
    verd = Counter(t["verdict"] for t in table)
    # (A) headline
    defended_units = [t for t in table if t["verdict"] == "DEFENSIBLE"]
    defended_acts = sorted({int(i) for t in defended_units for i in t["ids"].split("|")})
    dead_units = [t for t in table if t["verdict"] == "DEAD"]
    dead_acts = sorted({int(i) for t in dead_units for i in t["ids"].split("|")})
    thin_units = [t for t in table if t["verdict"].startswith("THIN")]
    thin_acts = sorted({int(i) for t in thin_units for i in t["ids"].split("|")})
    unmeasured_acts = sorted({r["id"] for r in pop["rows"] if not r["covered"]})

    marker1 = [t for t in table if (t["live_vs_fair_ratio"] or 0) > 10 and t["live_n"] >= 5]
    marker2 = [t for t in table if (t["wr_gap_live_minus_fair"] or 0) > 20 and t["live_n"] >= 10]
    infl = [t for t in table if (t["pf_inflation"] or 1) > 2 and t["BASE_n"]]
    stale = [t for t in table if (t["pct_entry_outside_bar"] or 0) > 50]

    summary = {
        "n_actives_total": pop["n_actives"],
        "n_actives_archive_covered": pop["n_covered"],
        "n_units_measured": len(table),
        "n_tests_for_bonferroni": n_tests,
        "A_defensible_units": len(defended_units),
        "A_defensible_actives": len(defended_acts),
        "A_defensible_ids": defended_acts,
        "M_known_dead_units": len(dead_units),
        "M_known_dead_actives": len(dead_acts),
        "K_unmeasurable_actives": len(unmeasured_acts),
        "K_unmeasured_but_measurable_thin": len(thin_acts),
        "verdicts": dict(verd),
        "marker1_live_over_fair_gt10x_nlive_ge5": len(marker1),
        "marker1_rows": marker1,
        "marker2_wr_gap_gt20_nlive_ge10": len(marker2),
        "marker2_rows": marker2,
        "pf_inflation_gt2_units": len(infl),
        "pct_entry_outside_decision_bar_gt50_units": len(stale),
        "survivor_list": sorted(
            [{"ids": t["ids"], "symbol": t["symbol"], "tf": t["tf"], "csid": t["csid"],
              "FAIR_n": t["FAIR_n"], "FAIR_pf": t["FAIR_pf"], "FAIR_wr": t["FAIR_wr"],
              "FAIR_e_r": t["FAIR_e_r"], "FAIR_total": t["FAIR_total"],
              "p_Bonferroni": t["p_Bonferroni"], "BASE_pf": t["BASE_pf"],
              "pf_inflation": t["pf_inflation"], "span_days": t["span_days"],
              "pct_entry_outside_bar": t["pct_entry_outside_bar"]}
             for t in table if t["verdict"] in ("DEFENSIBLE", "UNVERIFIED") and t["FAIR_n"]],
            key=lambda d: -(d["FAIR_pf"] or 0)),
    }
    json.dump(summary, open(SUMMARY, "w"), indent=1, default=str)
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ("marker1_rows", "marker2_rows", "A_defensible_ids",
                                   "survivor_list")}, indent=1, default=str))
    print("\nSURVIVORS (PF desc):")
    for d in summary["survivor_list"]:
        print(f"  {d['symbol']:4s} {d['tf']:3s} csid={d['csid']:<4} ids={d['ids']:<12} "
              f"n={d['FAIR_n']:<5} PF={d['FAIR_pf']:<6} WR={d['FAIR_wr']:<5} "
              f"E[R]={d['FAIR_e_r']:<6} pB={d['p_Bonferroni']:<8} basePF={d['BASE_pf']:<6} "
              f"infl={d['pf_inflation']}")


if __name__ == "__main__":
    main()