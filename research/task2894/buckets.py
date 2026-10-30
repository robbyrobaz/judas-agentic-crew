"""Task #2894 part B: cap-cannibalizer buckets + which caps actually bind."""
import json
import re
import sqlite3
from collections import defaultdict

con = sqlite3.connect("file:judas_crew.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row

actives = []
for r in con.execute(
    "SELECT id, symbol, strategy_family, version, params_json FROM active_strategies "
    "WHERE state='active' ORDER BY symbol, strategy_family, version DESC"
):
    p = json.loads(r["params_json"] or "{}")
    if not str(p.get("execution_engine", "")).startswith(("judas", "buffet", "custom")):
        continue
    actives.append({"id": r["id"], "symbol": r["symbol"], "family": r["strategy_family"],
                    "version": r["version"], "tf": str(p.get("timeframe", "1h")).lower()})

# coarse architecture family: strip timeframe tokens + variant suffixes
SUFFIX = re.compile(
    r"_(5m|15m|1h|1hour|hourly|roll\d+|v\d+|n\d+|csid\d+|opposite|loose|loosened|"
    r"tight|fresh|nogate\w*|_nogate|roll\d+|longonly\w*|diff|body\d+|_50pct|"
    r"filter_roll\d+|ny_only\w*|hygiene\w*|halfrange\w*|octant\w*|sweep_filter|"
    r"cross_asset|timegated\w*|session_disp|pullback\w*|_rr|50pct\w*|level\w*)$",
    re.I)


def coarse(fam: str) -> str:
    f = fam.lower()
    for _ in range(4):
        new = SUFFIX.sub("", f)
        if new == f:
            break
        f = new
    return f or fam.lower()


print("=== (symbol, architecture_family, timeframe) buckets with 3+ actives ===")
bucket = defaultdict(list)
for a in actives:
    bucket[(a["symbol"], coarse(a["family"]), a["tf"])].append(a["id"])
n3 = 0
for k, v in sorted(bucket.items(), key=lambda kv: (-len(kv[1]), kv[0])):
    if len(v) >= 3:
        n3 += 1
        print(f"  {k[0]:<5s} {k[1]:<38s} tf={k[2]:<4s} n={len(v)} ids={v}")
print(f"  buckets with 3+ actives: {n3}")

print("\n=== per-symbol active counts (runtime order = alphabetical) ===")
c = defaultdict(int)
for a in actives:
    c[a["symbol"]] += 1
for s in sorted(c):
    print(f"  {s:<5s} n_actives={c[s]:<4d}  runtime_rank_band="
          f"{1 if s=='6J' else 2 if s=='MCL' else 3 if s=='MGC' else 4 if s=='MNQ' else 5}")

# how many signals per day per symbol, and how many are in the first-10 band
print("\n=== signals/day by symbol (last 5 complete sessions) ===")
rows = con.execute(
    """SELECT substr(ts_utc,1,10) d, symbol, COUNT(*) n FROM signals
       WHERE strategy_id IS NOT NULL AND ts_utc >= '2026-10-23'
       GROUP BY d, symbol ORDER BY d"""
).fetchall()
per = defaultdict(lambda: defaultdict(int))
for r in rows:
    per[r["d"]][r["symbol"]] = r["n"]
for d in sorted(per):
    tot = sum(per[d].values())
    print(f"  {d} total={tot:<5d} " +
          " ".join(f"{s}={per[d].get(s,0)}" for s in ("6J", "MCL", "MGC", "MNQ", "ZF")))

# concentration: top symbol share of daily signals
print("\n=== daily signal share by symbol (last 5 sessions) ===")
for d in sorted(per):
    tot = sum(per[d].values())
    shares = sorted(((v / tot, s) for s, v in per[d].items()), reverse=True)
    print(f"  {d} top={shares[0][1]} {shares[0][0]*100:.1f}%  " +
          " ".join(f"{s}:{p*100:.0f}%" for p, s in shares))

con.close()
