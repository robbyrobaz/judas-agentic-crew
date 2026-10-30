"""Verify that signals.signal_id order == the runtime's iteration order.

If true, sorting a scan batch by signal_id is a faithful proxy for the
runtime's `for row in active:` order, which validates the "runtime (as-is)"
policy in research/cycle_20261030a/task2894_allocation_v2.py.
"""
import json
import sqlite3
from collections import defaultdict

con = sqlite3.connect("file:judas_crew.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row

actives = {}
for r in con.execute(
    "SELECT id, symbol, strategy_family, version, params_json FROM active_strategies "
    "WHERE state='active' ORDER BY symbol, strategy_family, version DESC"
):
    p = json.loads(r["params_json"] or "{}")
    if not str(p.get("execution_engine", "")).startswith(("judas", "buffet", "custom")):
        continue
    actives[r["id"]] = {
        "symbol": r["symbol"], "family": r["strategy_family"], "version": r["version"],
        "rank": None,
    }
# rank in runtime iteration order
order = sorted(actives.items(), key=lambda kv: (kv[1]["symbol"], kv[1]["family"], -kv[1]["version"]))
for i, (sid, a) in enumerate(order):
    a["rank"] = i
    actives[sid] = a

sigs = con.execute(
    """SELECT id, strategy_id, symbol, ts_utc, created_at FROM signals
       WHERE strategy_id IS NOT NULL AND ts_utc >= '2026-10-20'
       ORDER BY id"""
).fetchall()

# group into scan batches by created_at proximity (a scan writes its signals together)
batches = defaultdict(list)
for s in sigs:
    batches[str(s["created_at"])[:19]].append(s)

mono = 0
nonmono = 0
examples = []
for k, b in batches.items():
    ranks = [actives[s["strategy_id"]]["rank"] for s in b
             if s["strategy_id"] in actives]
    if len(ranks) < 2:
        continue
    if all(ranks[i] <= ranks[i + 1] for i in range(len(ranks) - 1)):
        mono += 1
    else:
        nonmono += 1
        if len(examples) < 3:
            examples.append((k, b))

print(f"scan batches examined (created_at groups, >=2 known actives): "
      f"monotonic={mono}  NON-monotonic={nonmono}")
for k, b in examples:
    print(f"\n  non-monotonic batch {k}:")
    for s in b[:14]:
        a = actives.get(s["strategy_id"])
        print(f"    sig {s['id']:<6d} sid={s['strategy_id']:<5d} "
              f"{s['symbol']:<5s} rank={a['rank'] if a else '?'} "
              f"{(a['family'][:34] if a else '')} v{(a['version'] if a else '')}")

# Which symbols win a contested batch? Take batches where 6J and MCL both fire.
print("\n=== who is served first when 6J and MCL fire in the same scan batch ===")
n_both = 0
first_is_6j = 0
first_is_mcl = 0
first_is_other = 0
for k, b in batches.items():
    syms = [s["symbol"] for s in b]
    if "6J" in syms and "MCL" in syms and len(syms) > 1:
        n_both += 1
        first = b[0]["symbol"]
        if first == "6J":
            first_is_6j += 1
        elif first == "MCL":
            first_is_mcl += 1
        else:
            first_is_other += 1
print(f"  batches containing BOTH 6J and MCL: {n_both}")
print(f"    first signal served = 6J  : {first_is_6j}")
print(f"    first signal served = MCL : {first_is_mcl}")
print(f"    first signal served = other: {first_is_other}")
con.close()
