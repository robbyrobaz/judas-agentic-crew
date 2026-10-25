"""Task #2862 - READ-ONLY probe of the live NinjaTrader working-order wedge.

Answers:
  (C1) exact live count of resting working orders / OCO legs on SimJudasFutures
  (C2a) does the wedge count against OUR 10-contract aggregate cap? (code path)
  (C2b) does the wedge block new entry submission at the BROKER (NT MaxPositionSize)?
  (C3) which resting legs would fill immediately on the next liquid bar (hazard)

STRICTLY READ-ONLY: SELECTs on NT's own sqlite + local bar cache reads.
No order placement, cancellation, flattening, or account mutation.
"""
from __future__ import annotations

import base64
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.broker.ninjatrader import NTBroker  # noqa: E402
from src.config import load_config  # noqa: E402

OUT = Path(__file__).resolve().parent
NT_SQLITE = r"C:/Users/hartw/Documents/NinjaTrader 8/db/NinjaTrader.sqlite"

# NT OrderType enum (NinjaTrader.Common.OrderType) as persisted in Orders.OrderType
NT_ORDER_TYPE = {0: "Limit", 1: "Market", 2: "Stop", 3: "StopLimit",
                 4: "StopMarket", 5: "MarketIfTouched", 6: "LimitIfTouched"}
NT_ORDER_STATE = {1: "Working", 2: "Accepted", 3: "Submitted", 4: "Filled",
                  5: "Cancelled", 6: "Rejected", 7: "PendingCancel"}


def nt_sqlite_read(sql: str, broker: NTBroker) -> str:
    """Run a SELECT against NT's own sqlite over the WinRM bridge. READ-ONLY."""
    body = (
        "import sqlite3, json\n"
        f"con=sqlite3.connect(r'{NT_SQLITE}')\n"
        "con.row_factory=sqlite3.Row\n"
        f"rows=con.execute({sql!r}).fetchall()\n"
        "out=[dict(r) for r in rows]\n"
        "print('SQLJSON'+json.dumps(out, default=str)+'ENDSQL')\n"
    )
    b64 = base64.b64encode(body.encode()).decode()
    ps = (
        '$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString() + ".py"); '
        f'[System.IO.File]::WriteAllBytes($tmp, [System.Convert]::FromBase64String("{b64}")); '
        f'& "{broker.python_exe}" $tmp; Remove-Item $tmp'
    )
    import winrm

    sess = winrm.Session(broker.host, auth=(broker.user, broker.password),
                         operation_timeout_sec=60, read_timeout_sec=90)
    r = sess.run_ps(ps)
    out = r.std_out.decode(errors="replace")
    a, b = out.find("SQLJSON"), out.find("ENDSQL")
    if a < 0 or b < 0:
        raise RuntimeError(f"NT sqlite read failed: {out[:400]}")
    return json.loads(out[a + 6:b])


def main() -> None:
    cfg = load_config()
    nt = cfg.ninjatrader
    broker = NTBroker(account=nt.account, instrument_map={}, host=nt.host,
                      user=nt.user, python_exe=nt.python_exe,
                      outgoing_dir=nt.outgoing_dir,
                      fill_timeout_s=nt.fill_timeout_s, fill_poll_s=nt.fill_poll_s)

    report: dict = {"account": nt.account, "nt_sqlite": NT_SQLITE}

    # --- C1: live working orders (OrderState=1) via the repo's own reader -----
    wo = broker.working_orders()
    report["working_orders_read_ok"] = wo is not None
    report["working_order_count"] = None if wo is None else len(wo)
    if wo:
        (OUT / "task2862_nt_working_orders.json").write_text(json.dumps(wo, indent=1))
        report["by_symbol"] = dict(Counter(o["symbol"] for o in wo))
        report["by_action"] = dict(Counter(o["action"] for o in wo))
        report["by_type"] = {NT_ORDER_TYPE.get(k, k): v
                             for k, v in Counter(int(o["order_type"]) for o in wo).items()}
        report["oldest_order_utc"] = min(o["time_utc"] for o in wo)
        report["newest_order_utc"] = max(o["time_utc"] for o in wo)
        report["total_resting_qty"] = sum(int(o["qty"]) for o in wo)

        # --- OCO pairing: how many legs lost their sibling? --------------------
        by_oco: dict[str, list[dict]] = defaultdict(list)
        for o in wo:
            by_oco[o["oco"] or "NO_OCO"].append(o)
        report["distinct_oco_groups"] = len(by_oco)
        report["oco_group_size_hist"] = dict(Counter(len(v) for v in by_oco.values()))
        report["oco_groups_missing_stop"] = sum(
            1 for v in by_oco.values()
            if not any(NT_ORDER_TYPE.get(int(o["order_type"])) == "StopMarket" for o in v))
        report["oco_groups_missing_target"] = sum(
            1 for v in by_oco.values()
            if not any(NT_ORDER_TYPE.get(int(o["order_type"])) == "Limit" for o in v))
        report["market_orders_resting"] = [
            {k: o[k] for k in ("order_id", "symbol", "contract", "action", "qty", "oco", "time_utc")}
            for o in wo if NT_ORDER_TYPE.get(int(o["order_type"])) == "Market"
        ]

    # --- live positions + account ------------------------------------------
    pos = broker.positions()
    report["positions"] = pos
    report["positions_count"] = None if pos is None else len(pos)
    report["account_summary"] = broker.account_summary()

    # --- C2b: NT account-level position limit (MaxPositionSize) ------------
    try:
        acct = nt_sqlite_read(
            "SELECT * FROM Accounts WHERE Name = '%s'" % nt.account, broker)
        report["nt_account_row"] = acct
        if acct:
            row = acct[0]
            for k in list(row):
                if any(t in k.lower() for t in ("position", "size", "margin", "leverage",
                                                "dollarn", "name", "type", "domestic")):
                    report.setdefault("nt_account_limit_fields", {})[k] = row[k]
    except Exception as exc:  # noqa: BLE001
        report["nt_account_row_error"] = repr(exc)

    # --- order-state histogram over the whole NT Orders table --------------
    try:
        states = nt_sqlite_read(
            "SELECT o.OrderState st, COUNT(*) n FROM Orders o JOIN Accounts a ON a.Id=o.Account "
            "WHERE a.Name='%s' GROUP BY o.OrderState" % nt.account, broker)
        report["nt_orders_by_state"] = {NT_ORDER_STATE.get(int(r["st"]), r["st"]): r["n"]
                                        for r in states}
    except Exception as exc:  # noqa: BLE001
        report["nt_orders_by_state_error"] = repr(exc)

    # --- C3: immediate-fill hazard vs last cached price --------------------
    try:
        from src import bar_cache
        marks: dict[str, float] = {}
        for sym in sorted({o["symbol"] for o in (wo or [])}):
            for tf in ("5m", "15m", "1h"):
                try:
                    df = bar_cache.read_cache(sym, tf)
                    if df is not None and len(df):
                        marks[f"{sym}@{tf}"] = float(df["close"].iloc[-1])
                        break
                except Exception:  # noqa: BLE001
                    continue
        report["last_cached_marks"] = marks
        hazard = []
        for o in (wo or []):
            tfmark = marks.get(f"{o['symbol']}@5m") or marks.get(f"{o['symbol']}@15m") \
                or marks.get(f"{o['symbol']}@1h")
            if tfmark is None:
                continue
            otype = NT_ORDER_TYPE.get(int(o["order_type"]))
            px = o["limit_price"] if otype == "Limit" else o["stop_price"]
            if not px:
                continue
            would_fill = (o["action"] == "BUY" and tfmark >= px) or \
                         (o["action"] == "SELL" and tfmark <= px)
            if would_fill:
                hazard.append({"order_id": o["order_id"], "symbol": o["symbol"],
                               "action": o["action"], "type": otype, "px": px,
                               "mark": tfmark, "oco": o["oco"], "qty": o["qty"],
                               "time_utc": o["time_utc"]})
        report["immediate_fill_hazard_count"] = len(hazard)
        report["immediate_fill_hazard"] = hazard[:60]
        report["immediate_fill_hazard_by_symbol"] = dict(Counter(h["symbol"] for h in hazard))
    except Exception as exc:  # noqa: BLE001
        report["hazard_error"] = repr(exc)

    # --- empirical NT OrderType mapping from OUR OWN trades table -----------
    # The repo places "STOPMARKET" (protective stop) and "LIMIT" (target).
    # Join the NT Orders table on the GUIDs we recorded in trades.sl_order_id
    # (stop leg) / trades.tp_order_id (target leg) to prove which NT
    # OrderType int is which - no guessing from enum docs.
    try:
        from src.db.models import get_conn
        with get_conn("judas_crew.db") as conn:
            legs = conn.execute(
                "SELECT id, symbol, sl_order_id, tp_order_id, ibkr_order_id FROM trades "
                "WHERE sl_order_id IS NOT NULL OR tp_order_id IS NOT NULL"
            ).fetchall()
        guids: list[str] = []
        for r in legs:
            for k in ("sl_order_id", "tp_order_id"):
                v = r[k]
                if v and len(str(v)) > 8:
                    guids.append(str(v))
        guids = sorted(set(guids))[:400]
        if guids:
            inlist = ",".join("'%s'" % g for g in guids)
            rows = nt_sqlite_read(
                "SELECT OrderId oid, OrderType typ, LimitPrice lp, StopPrice sp, "
                "Quantity qty, OrderState st FROM Orders WHERE OrderId IN (%s)" % inlist, broker)
            legmap = {}
            for r in legs:
                if r["sl_order_id"]:
                    legmap[str(r["sl_order_id"])] = "STOP_LEG"
                if r["tp_order_id"]:
                    legmap[str(r["tp_order_id"])] = "TARGET_LEG"
            xt = Counter()
            for r in rows:
                leg = legmap.get(str(r["oid"]), "?")
                xt[(leg, int(r["typ"]))] += 1
            report["empirical_ordertype_by_leg"] = {
                f"{leg}|type={t}": n for (leg, t), n in sorted(xt.items())}
    except Exception as exc:  # noqa: BLE001
        report["empirical_ordertype_error"] = repr(exc)

    # --- local DB: open trades + guard state -------------------------------
    try:
        from src.db.models import get_conn
        with get_conn("judas_crew.db") as conn:
            report["db_open_trades"] = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
            report["db_open_trades_rows"] = [
                dict(r) for r in conn.execute(
                    "SELECT id, symbol, direction, qty, entry_fill, sl_order_id, tp_order_id "
                    "FROM trades WHERE status='open'").fetchall()]
    except Exception as exc:  # noqa: BLE001
        report["db_error"] = repr(exc)

    (OUT / "task2862_nt_wedge_report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
