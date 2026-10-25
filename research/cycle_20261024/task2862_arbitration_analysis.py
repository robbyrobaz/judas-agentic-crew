"""Task #2862 - (A) entry arbitration order + (B) duplicate-fire blast radius.

READ-ONLY analysis of the local registry + the live scan ordering code path.
No orders, no registry mutations.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.strategy_registry import list_active_strategies  # noqa: E402

OUT = Path(__file__).resolve().parent

# Architecture families: strip variant suffixes so near-identical strategies
# (same code, different roll window / filter / csid) collapse into one bucket.
ARCH_RULES = [
    (r"asian_range_sweep", "asian_range_sweep"),
    (r"bsl_run_judas", "bsl_run_judas"),
    (r"silver_bullet|sb_pdh|pdh_pdl", "silver_bullet_pdh_pdl"),
    (r"ifvg", "ifvg_midpoint_reversion"),
    (r"ob_midpoint", "ob_midpoint_reversion"),
    (r"mitigation_block", "ict_mitigation_block"),
    (r"inside_bar_disp", "ict_inside_bar_disp_breakout"),
    (r"turtle_soup", "turtle_soup"),
    (r"session_disp_fvg", "session_disp_fvg_pullback"),
    (r"propulsion", "propulsion_block"),
    (r"judas_continuation", "judas_continuation"),
    (r"atr_disp_continuation", "atr_disp_continuation"),
    (r"htf_1h_disp", "htf_1h_disp_5m_fvg_pullback"),
    (r"frankfurt", "frankfurt_silver_bullet"),
    (r"sniper_chain", "sniper_chain"),
    (r"unicorn_model", "unicorn_model"),
    (r"sweep_first_ob", "sweep_first_ob"),
    (r"sfp_liquidity", "sfp_liquidity_grab"),
    (r"ny_am", "ny_am_variants"),
    (r"crt_3candle", "crt_3candle"),
    (r"ict_silver_bullet", "ict_silver_bullet_5step"),
    (r"custom", "custom"),
]


def arch_of(family: str) -> str:
    f = family.lower()
    for pat, name in ARCH_RULES:
        if re.search(pat, f):
            return name
    return f


def main() -> None:
    rows = list_active_strategies()
    report: dict = {"n_active": len(rows)}

    # --- (A) the deterministic order the scan actually walks ---------------
    # src/portfolio_runtime.py:1923 keeps list_active_strategies() order;
    # the loop at :2018 walks it; fires are placed in that order (:2060).
    order = [
        {"rank": i + 1, "id": r["id"], "symbol": r["symbol"],
         "family": r["strategy_family"], "version": r["version"],
         "timeframe": r["params"].get("timeframe", "1h"),
         "engine": r["params"].get("execution_engine"),
         "csid": r["params"].get("custom_strategy_id"),
         "qty": r["params"].get("qty", 1)}
        for i, r in enumerate(rows)
    ]
    report["first_25_in_scan_order"] = order[:25]
    report["symbol_block_sizes"] = dict(Counter(r["symbol"] for r in rows))
    report["symbol_order"] = list(dict.fromkeys(r["symbol"] for r in rows))
    # what the first 10 CONTRACTS would be if every active fired in one scan
    first10 = []
    for r in order:
        if len(first10) >= 10:
            break
        first10.append({"id": r["id"], "symbol": r["symbol"], "family": r["family"],
                        "timeframe": r["timeframe"], "csid": r["csid"]})
    report["first_10_contracts_if_all_fire"] = first10
    report["first_10_all_one_symbol"] = len({f["symbol"] for f in first10}) == 1
    # cumulative: how many of the first N are 6J
    report["first_N_symbol_run"] = {
        str(n): dict(Counter(r["symbol"] for r in order[:n])) for n in (10, 20, 30, 50)
    }

    # --- (B1) architecture x symbol x timeframe buckets ----------------------
    buckets: dict[tuple, list[int]] = defaultdict(list)
    for r in rows:
        tf = str(r["params"].get("timeframe", "1h")).lower()
        buckets[(r["symbol"], arch_of(r["strategy_family"]), tf)].append(r["id"])
    b_rows = sorted(
        ({"symbol": k[0], "arch": k[1], "timeframe": k[2], "n": len(v), "ids": v}
         for k, v in buckets.items()),
        key=lambda d: (-d["n"], d["symbol"], d["arch"]))
    report["architecture_buckets_top20"] = b_rows[:20]
    report["architecture_buckets_ge3"] = [b for b in b_rows if b["n"] >= 3]
    report["architecture_bucket_count"] = len(b_rows)

    # --- (B1b) EXACT duplicate fire pairs: same custom_strategy_id ---------
    by_csid: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        csid = r["params"].get("custom_strategy_id")
        if csid:
            by_csid[(str(csid), r["symbol"], str(r["params"].get("timeframe", "1h")).lower())].append(
                {"id": r["id"], "family": r["strategy_family"], "version": r["version"]})
    dupes = {f"csid={k[0]}|{k[1]}|{k[2]}": v for k, v in by_csid.items() if len(v) > 1}
    report["exact_duplicate_fire_groups"] = dupes
    report["n_exact_duplicate_groups"] = len(dupes)
    report["n_actives_in_duplicate_groups"] = sum(len(v) for v in dupes.values())

    # --- (B2) one strategy consuming multiple contracts ---------------------
    # _eval_sized_qty (portfolio_runtime.py:162) can raise qty to _EVAL_MAX_QTY=5
    # for MNQ/MGC/MCL; the cap check counts PLACED TRADES not CONTRACTS
    # (portfolio_runtime.py:1791 + :2078).
    report["eval_max_qty"] = 5
    report["eval_risk_symbols"] = ["MNQ", "MGC", "MCL"]
    report["sizing_symbol_count"] = sum(
        1 for r in rows if r["symbol"] in ("MNQ", "MGC", "MCL"))
    report["non_sizing_symbol_count"] = sum(
        1 for r in rows if r["symbol"] not in ("MNQ", "MGC", "MCL"))

    # --- (B3) family-level concentration per symbol (ignoring timeframe) ---
    fam: dict[tuple, list[int]] = defaultdict(list)
    for r in rows:
        fam[(r["symbol"], arch_of(r["strategy_family"]))].append(r["id"])
    fam_rows = sorted(({"symbol": k[0], "arch": k[1], "n": len(v)} for k, v in fam.items()),
                      key=lambda d: -d["n"])
    report["symbol_arch_counts_top15"] = fam_rows[:15]
    report["symbol_arch_counts_ge4"] = [f for f in fam_rows if f["n"] >= 4]

    (OUT / "task2862_arbitration_report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=str)[:9000])


if __name__ == "__main__":
    main()
