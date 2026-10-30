"""Task #2894 — entry allocation order + shadow fire rate.

READ-ONLY. Reproduces the runtime's exact active-strategy iteration order and
compares it against broker-confirmed live edge.

Runtime order (src/strategy_registry.py:945-951 list_active_strategies):
    SELECT * FROM active_strategies WHERE state='active'
    ORDER BY symbol, strategy_family, version DESC
src/portfolio_runtime.py:1923 filters that list by execution_engine, and
src/portfolio_runtime.py:2038 iterates `for row in active:` placing fires
FIRST-COME-FIRST-SERVED via _place_fire_with_record.
"""
import json
import sqlite3
from collections import defaultdict

DB = "judas_crew.db"
LIVE_ENGINES = ("judas", "buffet", "custom")

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

# ---------------------------------------------------------------- 1. order
rows = con.execute(
    """SELECT id, symbol, strategy_family, version, params_json, metrics_json,
              activated_at_utc
       FROM active_strategies WHERE state='active'
       ORDER BY symbol, strategy_family, version DESC"""
).fetchall()

active = []
for r in rows:
    p = json.loads(r["params_json"] or "{}")
    if not str(p.get("execution_engine", "")).startswith(LIVE_ENGINES):
        continue
    active.append({
        "id": r["id"], "symbol": r["symbol"], "family": r["strategy_family"],
        "version": r["version"], "params": p,
        "metrics": json.loads(r["metrics_json"] or "{}"),
        "activated": r["activated_at_utc"],
    })

print(f"=== RUNTIME ITERATION ORDER (n={len(active)}) ===")
print("idx  id    symbol family                              ver  qty  engine")
for i, a in enumerate(active):
    print(f"{i:3d}  {a['id']:<4d} {a['symbol']:<6s} {a['family'][:34]:<34s} "
          f"{a['version']:<3d} {str(a['params'].get('qty',''))[:4]:<4s} "
          f"{a['params'].get('execution_engine')}")

# ------------------------------------------------------- 2. broker-confirmed
# Real NT fills only: trades rows with a real fill (entry_fill not null) and a
# closed pnl. These are BROKER-CONFIRMED (class R).
bt = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0, "sym": None})
all_closed = con.execute(
    """SELECT strategy_id, symbol, pnl_dollars, exit_reason, opened_at, closed_at,
              entry_fill, exit_fill
       FROM trades WHERE status='closed' AND pnl_dollars IS NOT NULL"""
).fetchall()
for t in all_closed:
    sid = t["strategy_id"]
    if sid is None:
        continue
    b = bt[sid]
    b["n"] += 1
    b["pnl"] += float(t["pnl_dollars"] or 0.0)
    if float(t["pnl_dollars"] or 0.0) > 0:
        b["w"] += 1
    b["sym"] = t["symbol"]

tot_pnl = sum(float(t["pnl_dollars"] or 0) for t in all_closed)
print(f"\n=== BROKER-CONFIRMED CLOSED TRADES: n={len(all_closed)} net=${tot_pnl:,.2f} ===")
per_sym = defaultdict(float)
for t in all_closed:
    per_sym[t["symbol"]] += float(t["pnl_dollars"] or 0)
for s, v in sorted(per_sym.items(), key=lambda kv: -kv[1]):
    print(f"  {s:<5s} ${v:>10,.2f}")

# ------------------------------------------- 3. shadow (synthetic) signal fire
# signals.ts_utc is the bar that triggered. This is FORWARD-SIGNAL evidence
# (synthetic class S), NOT a fill.
sig = con.execute(
    """SELECT strategy_id, symbol, ts_utc, direction, quality_score, risk_decision
       FROM signals WHERE strategy_id IS NOT NULL
       ORDER BY ts_utc"""
).fetchall()
by_sid = defaultdict(list)
for s in sig:
    by_sid[s["strategy_id"]].append(s)

print(f"\n=== FORWARD-SIGNAL FIRE COUNTS (class S, synthetic) ===")
print(f"total signals rows={len(sig)}  distinct strategy_ids={len(by_sid)}")

# never fired
never = [a for a in active if not by_sid.get(a["id"])]
print(f"actives with ZERO signals ever: {len(never)}")

# per-session fire counts. Session = UTC date.
sess = defaultdict(lambda: defaultdict(int))
for s in sig:
    day = str(s["ts_utc"])[:10]
    sess[day][s["strategy_id"]] += 1
days = sorted(sess.keys())
print(f"distinct signal days: {len(days)}  range {days[0] if days else '-'} .. {days[-1] if days else '-'}")

print("\n--- actives firing per session (last 10 signal days) ---")
print("day        actives_with_>=1_signal  total_signals")
for d in days[-10:]:
    m = sess[d]
    print(f"{d}   {len(m):>4d}                    {sum(m.values()):>4d}")

# ---------------------------------------------------------------- 4. buckets
bucket = defaultdict(list)
for a in active:
    bucket[(a["symbol"], a["family"], a["params"].get("timeframe", "1h"))].append(a["id"])
print("\n=== (symbol, family, timeframe) buckets with 3+ actives (cap-cannibalizers) ===")
for k, v in sorted(bucket.items(), key=lambda kv: -len(kv[1])):
    if len(v) >= 3:
        print(f"  {k[0]:<5s} {k[1][:40]:<40s} tf={str(k[2]):<4s} n={len(v)} ids={v}")

# --------------------------------- 5. what an edge-ranked order would look
print("\n=== EDGE-RANKED COUNTERFACTUAL (broker-confirmed $ per trade) ===")
ranked = sorted(active, key=lambda a: (
    -(bt[a["id"]]["pnl"] / bt[a["id"]]["n"]) if bt[a["id"]]["n"] else 1e9,
    -bt[a["id"]]["n"],
))
print("rank id    sym  family                             n  pnl   $/trade")
for i, a in enumerate(ranked[:25]):
    b = bt[a["id"]]
    print(f"{i+1:3d} {a['id']:<4d} {a['symbol']:<5s} {a['family'][:32]:<32s} "
          f"{b['n']:>2d} ${b['pnl']:>8,.2f} "
          f"{(b['pnl']/b['n'] if b['n'] else 0):>8,.2f}")

# where do the runtime's first-N slots land under both orders?
for N in (10, 20):
    rt = [a for a in active[:N]]
    rk = ranked[:N]
    rt_syms = defaultdict(int)
    rk_syms = defaultdict(int)
    for a in rt:
        rt_syms[a["symbol"]] += 1
    for a in rk:
        rk_syms[a["symbol"]] += 1
    print(f"\nfirst {N} slots by RUNTIME order: {dict(rt_syms)}  "
          f"broker-PnL in those slots=${sum(bt[a['id']]['pnl'] for a in rt):,.2f}")
    print(f"first {N} slots by EDGE order : {dict(rk_syms)}  "
          f"broker-PnL in those slots=${sum(bt[a['id']]['pnl'] for a in rk):,.2f}")

json.dump(
    {"order": [{"id": a["id"], "symbol": a["symbol"], "family": a["family"],
                "version": a["version"], "tf": a["params"].get("timeframe"),
                "engine": a["params"].get("execution_engine")} for a in active]},
    open("research/task2894/runtime_order.json", "w"), indent=1)
print("\nwrote research/task2894/runtime_order.json")
con.close()
