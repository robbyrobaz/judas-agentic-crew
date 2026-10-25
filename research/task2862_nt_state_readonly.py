"""task #2862 (C) -- READ-ONLY live NT state probe.

Reads (never writes):
  * broker.working_orders()  -> NT Orders table, OrderState=1 (working/OCO legs)
  * broker.positions()       -> NT Positions table + per-instrument position files
  * broker.account_summary()-> NT account snapshot

NO cancel, NO flatten, NO order placement, NO DB write. This file only calls
read methods on NTBroker and prints JSON.
"""
from __future__ import annotations

import json
import sys
from collections import Counter

from src.broker.ninjatrader import NTBroker
from src.config import load_config


def main() -> int:
    cfg = load_config()
    nt = cfg.ninjatrader
    if nt is None:
        print(json.dumps({"error": "no ninjatrader section in config"}))
        return 1
    broker = NTBroker(
        account=nt.account,
        instrument_map={},
        host=nt.host,
        user=nt.user,
        python_exe=nt.python_exe,
        outgoing_dir=nt.outgoing_dir,
        fill_timeout_s=nt.fill_timeout_s,
        fill_poll_s=nt.fill_poll_s,
    )
    out: dict = {"account": nt.account, "host": nt.host}

    wo = broker.working_orders()
    out["working_orders_read_ok"] = wo is not None
    if wo is None:
        out["working_orders"] = None
        out["working_orders_note"] = "READ FAILED (None) -- WinRM/NT path down"
    else:
        out["working_orders_count"] = len(wo)
        by_sym = Counter(str(o.get("symbol", "?")).upper() for o in wo)
        by_act = Counter(str(o.get("action", "?")) for o in wo)
        by_type = Counter(str(o.get("order_type", "?")) for o in wo)
        out["working_orders_by_symbol"] = dict(by_sym)
        out["working_orders_by_action"] = dict(by_act)
        out["working_orders_by_type"] = dict(by_type)
        out["working_orders_qty_total"] = sum(int(o.get("qty") or 0) for o in wo)
        out["working_orders_sample"] = wo[:8]
        times = sorted(str(o.get("time_utc") or "") for o in wo)
        out["working_orders_time_min"] = times[0] if times else None
        out["working_orders_time_max"] = times[-1] if times else None

    pos = broker.positions()
    out["positions_read_ok"] = pos is not None
    if pos is None:
        out["positions"] = None
        out["positions_note"] = "READ FAILED (None) -- WinRM/NT path down"
    else:
        out["positions_count"] = len(pos)
        out["positions_contracts_total"] = sum(int(p.get("qty") or 0) for p in pos)
        out["positions"] = pos

    summ = broker.account_summary()
    out["account_summary_read_ok"] = summ is not None
    out["account_summary"] = summ

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
