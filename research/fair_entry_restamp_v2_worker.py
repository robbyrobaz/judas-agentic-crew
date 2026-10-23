"""Shared-queue fair-entry restamp worker -- task #2869.

Why this exists
---------------
The chunked launchers (`--chunk/--chunks`) partition the unit list by index,
so a unit that a slow chunk has not reached yet can be picked up by a second
worker and measured TWICE, and a unit that is slow can stall its whole chunk.
This worker instead uses a shared done-set: every worker scans ALL existing
part files, claims only units nobody has measured yet, and writes its own
part file. Re-running the whole fleet is therefore safe and resumable.

READ-ONLY w.r.t. registry and account.
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

from src import bar_archive  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.research.custom_strategy_runtime import _compile_and_load  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fair_entry_restamp_book2_20261022 import run_variant, metrics  # noqa: E402

POP = "outputs/fair_book_pop_20261022.json"
ALL_PARTS = ["outputs/fair_entry_book_v2_20261023.part*",
             "outputs/fair_entry_book_v2_20261023.worker*.json"]


def _key(csid, symbol, tf):
    """csid may be int or str across part files -- compare on a stable form."""
    return (str(csid), str(symbol), str(tf))


def done_keys():
    keys = set()
    for pat in ALL_PARTS:
        for p in glob.glob(pat):
            try:
                for r in json.load(open(p))["rows"]:
                    keys.add(_key(r.get("csid"), r.get("symbol"), r.get("tf")))
            except Exception:  # noqa: BLE001
                pass
    return keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", type=int, required=True)
    ap.add_argument("--workers", type=int, required=True)
    ap.add_argument("--out", default="outputs/fair_entry_book_v2_20261023.worker{}.json")
    ap.add_argument("--budget", type=float, default=420.0)
    ap.add_argument("--modes", default="base,fair")
    a = ap.parse_args()
    outpath = Path(a.out.format(a.worker))
    modes = a.modes.split(",")

    con = sqlite3.connect("file:judas_crew.db?mode=ro", uri=True)
    # `custom_strategy_id` is a STRING in some params_json rows and an int in
    # others.  Key the lookup by BOTH so a unit is never dropped as NO_CODE
    # purely because of JSON type drift.
    codes = {}
    for i, c in con.execute("SELECT id, code FROM custom_strategies"):
        codes[int(i)] = c
        codes[str(i)] = c
    con.close()
    pop = json.load(open(POP))
    rows_by_id = {r["id"]: r for r in pop["rows"]}
    units = pop["units"]

    done = done_keys()
    # deterministic claim: interleaved so every worker gets a mix of symbols
    pending = [u for u in units
               if _key(u["csid"], u["symbol"], u["tf"]) not in done]
    mine = pending[a.worker::a.workers]
    print(f"worker {a.worker}: {len(pending)} pending, {len(mine)} claimed", flush=True)

    cache = {}
    for u in mine:
        t0 = time.time()
        rec = {"csid": u["csid"], "symbol": u["symbol"], "tf": u["tf"], "ids": u["ids"],
               "cost_model": COST_MODEL_VERSION}
        code = codes.get(u["csid"])
        if code is None and u["csid"] is not None:
            code = codes.get(str(u["csid"]))
        if not code:
            rec["verdict"] = "NO_CODE"
            _dump(outpath, rec)
            continue
        ck = (u["symbol"], u["tf"])
        if ck not in cache:
            cache[ck] = bar_archive.read_archive(ck[0], ck[1])
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
        b, f = rec.get("BASE", {}), rec.get("FAIR", {})
        rec["pf_inflation"] = round(b.get("pf", 0) / f["pf"], 3) if f.get("pf") else None
        rec["usd_ratio_BASE_over_FAIR"] = (round(b.get("usd_per_trade", 0) / f["usd_per_trade"], 2)
                                           if f.get("usd_per_trade") else None)
        rec["wall_s"] = round(time.time() - t0, 1)
        _dump(outpath, rec)
        print(f"[{u['symbol']} {u['tf']} csid={u['csid']} ids={u['ids']}] "
              f"BASE n={b.get('n')} pf={b.get('pf')} wr={b.get('wr_pct')} "
              f"| FAIR n={f.get('n')} pf={f.get('pf')} wr={f.get('wr_pct')} "
              f"e_r={f.get('e_r')} p={f.get('p_1sided')} ({rec['wall_s']}s)", flush=True)


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


if __name__ == "__main__":
    main()
