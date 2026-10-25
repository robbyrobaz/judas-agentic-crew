"""FAIR ENTRY RESTAMP v3 worker -- task #2869 (2026-10-24).

Same harness as v2 (`fair_entry_restamp_book2_20261022.run_variant` /
`metrics`, both of which are port-validated against the production engine --
see `outputs/fair_v2_validation_20261022.json`: v2_BASE reproduces the engine's
n / PF / total EXACTLY on csid 194 and 215).

What v3 adds
------------
1. A third fill mode, `live`, in addition to `base` and `fair`:
     base  = entry at the signal's own price      (what run_custom_backtest says)
     fair  = entry at the DECISION BAR'S CLOSE    (fair decision-time entry)
     live  = entry at the NEXT BAR'S OPEN         (production semantics,
             finding #9134: the live path is a MARKET order with
             fill-anchored brackets)
   The verdict must not depend on which entry model you believe, so all three
   are measured and reported side by side.
2. It re-measures EVERY unit from the current population with all three modes
   in ONE pass, so the three modes of a unit always come from the same
   archive/params snapshot (no cross-file mixing).
3. It re-runs the population rebuild so that the 6J archives (built 2026-10-23
   14:33) are included.

READ-ONLY w.r.t. registry and account. Writes only its own part file.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import bar_archive  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION  # noqa: E402

from fair_entry_restamp_book2_20261022 import run_variant, metrics  # noqa: E402

POP = "outputs/fair_book_pop_20261022.json"
PREFIX = "outputs/fair_entry_book_v3_20261024"


def _key(csid, symbol, tf):
    return (str(csid), str(symbol), str(tf))


def done_keys():
    keys = set()
    for p in glob.glob(PREFIX + "*.json"):
        try:
            for r in json.load(open(p))["rows"]:
                keys.add(_key(r.get("csid"), r.get("symbol"), r.get("tf")))
        except Exception:  # noqa: BLE001
            pass
    return keys


def _dump(path, rec):
    rows = []
    if path.exists():
        try:
            rows = json.load(open(path))["rows"]
        except Exception:  # noqa: BLE001
            rows = []
    rows = [r for r in rows
            if _key(r.get("csid"), r.get("symbol"), r.get("tf")) !=
            _key(rec.get("csid"), rec.get("symbol"), rec.get("tf"))]
    rows.append(rec)
    tmp = str(path) + f".tmp{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump({"cost_model": COST_MODEL_VERSION, "rows": rows}, fh, indent=1, default=str)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", type=int, required=True)
    ap.add_argument("--workers", type=int, required=True)
    ap.add_argument("--out", default=PREFIX + ".worker{}.json")
    ap.add_argument("--budget", type=float, default=300.0)
    ap.add_argument("--modes", default="base,fair,live")
    a = ap.parse_args()
    outpath = Path(a.out.format(a.worker))
    modes = a.modes.split(",")

    con = sqlite3.connect("file:judas_crew.db?mode=ro", uri=True)
    codes = {}
    for i, c in con.execute("SELECT id, code FROM custom_strategies"):
        codes[int(i)] = c
        codes[str(i)] = c
    con.close()

    pop = json.load(open(POP))
    rows_by_id = {r["id"]: r for r in pop["rows"]}
    units = pop["units"]

    done = done_keys()
    pending = [u for u in units
               if _key(u["csid"], u["symbol"], u["tf"]) not in done]
    mine = pending[a.worker::a.workers]
    print(f"worker {a.worker}: {len(pending)} pending, {len(mine)} claimed", flush=True)

    cache = {}
    for u in mine:
        t0 = time.time()
        rec = {"csid": u["csid"], "symbol": u["symbol"], "tf": u["tf"], "ids": u["ids"],
               "cost_model": COST_MODEL_VERSION}
        code = codes.get(u["csid"]) or codes.get(str(u["csid"]))
        if not code:
            rec["verdict"] = "NO_CODE"
            _dump(outpath, rec)
            continue
        ck = (u["symbol"], u["tf"])
        if ck not in cache:
            try:
                cache[ck] = bar_archive.read_archive(ck[0], ck[1])
            except Exception as exc:  # noqa: BLE001
                cache[ck] = None
                print(f"archive read fail {ck}: {exc}", flush=True)
        bars = cache[ck]
        if bars is None or len(bars) == 0:
            rec["verdict"] = "NOT-RESTAMPABLE (no archive)"
            _dump(outpath, rec)
            continue
        ts = bars["ts"]
        rec["archive_bars"] = int(len(bars))
        rec["span_days"] = int((ts.iloc[-1] - ts.iloc[0]).total_seconds() / 86400)
        rec["first_bar"] = str(ts.iloc[0])
        rec["last_bar"] = str(ts.iloc[-1])
        spec = _CONTRACT_SPECS.get(u["symbol"])
        dpp = float(spec["tick_value"]) / float(spec["tick"])
        params = dict(rows_by_id[u["ids"][0]]["params"])
        params.setdefault("__csid__", u["csid"])
        rec["params"] = {k: v for k, v in params.items() if k != "__csid__"}
        for mode in modes:
            tr, meta = run_variant(code, bars, params, u["symbol"], dpp, mode,
                                   budget_s=a.budget, want_stale=(mode == modes[0]))
            rec[mode.upper()] = metrics(tr or [])
            rec[f"{mode.upper()}_meta"] = meta
        b, f, lv = rec.get("BASE", {}), rec.get("FAIR", {}), rec.get("LIVE", {})
        rec["pf_inflation"] = round(b.get("pf", 0) / f["pf"], 3) if f.get("pf") else None
        rec["pf_inflation_live"] = round(b.get("pf", 0) / lv["pf"], 3) if lv.get("pf") else None
        rec["usd_ratio_BASE_over_FAIR"] = (round(b.get("usd_per_trade", 0) / f["usd_per_trade"], 2)
                                           if f.get("usd_per_trade") else None)
        rec["usd_ratio_BASE_over_LIVE"] = (round(b.get("usd_per_trade", 0) / lv["usd_per_trade"], 2)
                                           if lv.get("usd_per_trade") else None)
        rec["wall_s"] = round(time.time() - t0, 1)
        _dump(outpath, rec)
        print(f"[{u['symbol']} {u['tf']} csid={u['csid']} ids={u['ids']}] "
              f"BASE n={b.get('n')} pf={b.get('pf')} | FAIR n={f.get('n')} pf={f.get('pf')} "
              f"wr={f.get('wr_pct')} e_r={f.get('e_r')} | LIVE n={lv.get('n')} pf={lv.get('pf')} "
              f"wr={lv.get('wr_pct')} e_r={lv.get('e_r')} "
              f"stale%={(rec.get('BASE_meta') or {}).get('pct_entry_outside_decision_bar')} "
              f"({rec['wall_s']}s)", flush=True)


if __name__ == "__main__":
    main()
