import json, sqlite3, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import bar_archive
from src.research.custom_strategy_runtime import run_custom_backtest_on_bars
from src.portfolio_runtime import _CONTRACT_SPECS
import research.fair_entry_restamp_book2_20261022 as H

con = sqlite3.connect("file:judas_crew.db?mode=ro", uri=True)
codes = {i: c for i, c in con.execute("SELECT id, code FROM custom_strategies")}
con.close()
pop = json.load(open("outputs/fair_book_pop_20261022.json"))
rows_by_id = {r["id"]: r for r in pop["rows"]}
want = {215, 194, 235, 357, 348, 401}
out = []
for u in pop["units"]:
    if u["csid"] not in want:
        continue
    bars = bar_archive.read_archive(u["symbol"], u["tf"])
    if bars is None or len(bars) == 0:
        continue
    params = dict(rows_by_id[u["ids"][0]]["params"]); params.setdefault("__csid__", u["csid"])
    code = codes[u["csid"]]
    spec = _CONTRACT_SPECS[u["symbol"]]; dpp = float(spec["tick_value"])/float(spec["tick"])
    t0 = time.time()
    eng = run_custom_backtest_on_bars(code=code, bars=bars, params=params, symbol=u["symbol"], qty=1, timeout_s=1800)
    tr, meta = H.run_variant(code, bars, params, u["symbol"], dpp, "base", budget_s=900, want_stale=True)
    m = H.metrics(tr or [])
    row = {"csid": u["csid"], "symbol": u["symbol"], "tf": u["tf"],
           "engine": {"n": eng.get("n_trades"), "pf": round(float(eng.get("pf",0)),3),
                      "W": eng.get("n_wins"), "L": eng.get("n_losses"),
                      "total": round(float(eng.get("total_pnl",0)),2),
                      "stale_fill_pct": eng.get("stale_fill_pct"), "err": eng.get("error")},
           "v2_BASE": {"n": m["n"], "pf": m["pf"], "W": round(m["wr_pct"]/100*m["n"]), "total": m["total_usd"]},
           "stale": {k: meta.get(k) for k in ("stale_bars_median","stale_bars_p90","stale_bars_max","pct_stale_gt_0","pct_stale_gt_2","pct_unmeasured_ts","timed_out","loop_s","ms_per_call")}}
    row["n_match"] = row["engine"]["n"] == row["v2_BASE"]["n"]
    out.append(row); print(json.dumps(row), flush=True)
json.dump(out, open("outputs/fair_v2_validation_20261022.json","w"), indent=1)
