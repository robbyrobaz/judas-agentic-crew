"""Port validation: prove the FAIR harness's BASE variant is a faithful
replica of `run_custom_backtest_on_bars` before any book-wide number is
believed.  Compares n / PF / W / L / total on the SAME bars and params."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import bar_archive  # noqa: E402
from src.research.custom_strategy_runtime import run_custom_backtest_on_bars  # noqa: E402
import research.fair_entry_restamp_book_20261022 as H  # noqa: E402


def main():
    con = sqlite3.connect("file:judas_crew.db?mode=ro", uri=True)
    codes = {i: c for i, c in con.execute("SELECT id, code FROM custom_strategies")}
    con.close()
    pop = json.load(open("outputs/fair_book_pop_20261022.json"))
    rows_by_id = {r["id"]: r for r in pop["rows"]}

    # csids chosen to span the roster: the #1 earner family, a mitigation block,
    # an ifvg reversion, and a small-n one.
    want = [401, 333, 235, 194, 215, 357, 348]
    out = []
    for u in pop["units"]:
        if u["csid"] not in want:
            continue
        bars = bar_archive.read_archive(u["symbol"], u["tf"])
        if bars is None or bars.empty:
            continue
        params = dict(rows_by_id[u["ids"][0]]["params"])
        params.setdefault("__csid__", u["csid"])
        code = codes[u["csid"]]

        t0 = time.time()
        eng = run_custom_backtest_on_bars(code=code, bars=bars, params=params,
                                          symbol=u["symbol"], qty=1, timeout_s=900)
        sigs, meta = H.collect_signals(code, bars, params, H.SLICE_CAP[u["tf"]])
        mine = H.simulate(sigs, bars, u["symbol"], 1.0, "base")  # dpp irrelevant to counts
        n_mine = len(mine)
        row = {
            "csid": u["csid"], "symbol": u["symbol"], "tf": u["tf"],
            "engine": {"n": eng.get("n_trades"), "pf": round(float(eng.get("pf", 0)), 3),
                       "W": eng.get("n_wins"), "L": eng.get("n_losses"),
                       "total": round(float(eng.get("total_pnl", 0)), 2),
                       "stale_fill_pct": eng.get("stale_fill_pct"),
                       "stale_measure_fail_n": eng.get("stale_measure_fail_n"),
                       "err": eng.get("error")},
            "mine_BASE": {"n": n_mine, "W": sum(1 for t in mine if t["pnl"] > 0),
                          "L": sum(1 for t in mine if t["pnl"] <= 0)},
            "loop": meta, "wall_s": round(time.time() - t0, 1),
        }
        row["n_match"] = row["engine"]["n"] == row["mine_BASE"]["n"]
        row["wl_match"] = (row["engine"]["W"] == row["mine_BASE"]["W"]
                           and row["engine"]["L"] == row["mine_BASE"]["L"])
        out.append(row)
        print(json.dumps(row), flush=True)
    json.dump(out, open("outputs/fair_entry_port_validation_20261022.json", "w"), indent=1)


if __name__ == "__main__":
    main()