#!/usr/bin/env python3
"""SUPERSEDURE_COLLATERAL_RESTORE_PLAN analyzer (read-only).

Each row in the sqlite3 -separator '|' export starts with NN|<sym>|<fam>|...|
The notes field may contain literal pipes and newlines; we anchor on
the leading numeric ID + pipe pattern to re-segment.
"""
import json
import re
from collections import defaultdict

DEC = "_restore_ledger/deactivated.tsv"
ACT = "_restore_ledger/actives.tsv"

# A row of the deactivated file starts with `<id>|<sym>|...`
# Build regex to split on these row starts.
ROW_START_RE = re.compile(r"\n?\n?(?=\b\d{4,5}\|)")
# Or process line-by-line: find lines beginning with NN and treat them as
# row starts, and concatenate trailing content (notes spilling) into the
# last field.

def parse_pipe(path, n_expected_fields):
    """Parse where each row begins with line starting `<id>|`. Continuation
    lines (notes/etc.) get appended to the last field."""
    rows = []
    with open(path) as f:
        text = f.read()
    # Use a simpler splitter: split on the pattern `NEWROW{id}|`
    pattern = r"(?:^|\n)(?P<id>\d+)\|(?P<rest>.*?)(?=(?:\n\d+\|)|$)"
    for m in re.finditer(pattern, text, flags=re.DOTALL):
        rest = m.group("rest").rstrip("\n")
        fields = rest.split("|", n_expected_fields - 1)  # split into n-1 parts, last is rest
        if len(fields) < n_expected_fields - 1:
            continue
        # The last field may contain newline chars; trim trailing $ if present
        rec = [m.group("id")] + fields
        rows.append(rec)
    return rows

dec_rows = parse_pipe(DEC, 10)
act_rows = parse_pipe(ACT, 6)
print(f"Deactivated rows parsed: {len(dec_rows)}")
print(f"Active rows parsed: {len(act_rows)}")

if dec_rows and act_rows:
    print(f"First dec record (id, sym): {dec_rows[0][0]} {dec_rows[0][1]}")
    print(f"First act record (id, sym): {act_rows[0][0]} {act_rows[0][1]}")
else:
    print("PARSING STILL BROKEN")
    exit(1)

# === Build slot coverage from ACTIVES ===
slot_actives = defaultdict(list)
csid_actives = defaultdict(list)

for a in act_rows:
    try:
        params = json.loads(a[5]) if a[5].startswith("{") else {}
    except Exception:
        params = {}
    csid = str(params.get("custom_strategy_id", ""))
    tf_ = params.get("timeframe", "") or ""
    slot_actives[(a[1], tf_)].append({"family": a[2], "version": a[3], "csid": csid, "params": params, "active_id": a[0]})
    csid_actives[csid].append({"sym": a[1], "tf": tf_, "fam": a[2], "ver": a[3], "active_id": a[0], "csid": csid})

# === Classify each deactivated row ===
def field(m, *keys, default=None):
    for k in keys:
        if k in m:
            return m[k]
    return default

def _fnum(v):
    try: return float(v)
    except Exception: return None

def _inum(v):
    try: return int(v)
    except Exception: return None

def pf(m): return _fnum(field(m, "profit_factor", "PF", "pf"))
def n(m):  return _inum(field(m, "n_trades", "n_signals", "n", "count"))
def wr(m): return _fnum(field(m, "win_rate", "winrate", "WR"))
def er(m): return _fnum(field(m, "expectancy_r", "ER"))
def pnl(m): return _fnum(field(m, "total_pnl", "pnl"))
def maxdd(m): return _fnum(field(m, "max_drawdown", "max_dd", "maxDD"))
def cm(m): return field(m, "cost_model") or ""
def tf(m): return field(m, "timeframe") or ""
def days(m):
    v = field(m, "days", "window_days", "backtest_window_days")
    if v in (None, ""): return None
    try: return int(v)
    except Exception: return v

classified = []
for d in dec_rows:
    metrics_raw = d[8]
    notes = d[9]
    try:
        m = json.loads(metrics_raw) if metrics_raw.startswith("{") else {}
    except Exception:
        m = {}
    pf_ = pf(m)
    n_ = n(m)
    wr_ = wr(m)
    er_ = er(m)
    pnl_ = pnl(m)
    maxdd_ = maxdd(m)
    cost = cm(m)
    tf_ = tf(m) or ""
    days_ = days(m)
    if days_ is None or days_ == "":
        span_str = "UNVERIFIED"
    elif isinstance(days_, int):
        if tf_ in ("5m", "15m"):
            span_str = f"{days_}d (UNVERIFIED-5m15m-cap-suspect)"
        else:
            span_str = f"{days_}d"
    else:
        span_str = f"{days_} (UNVERIFIED)"
    is_lure = False
    try:
        if pf_ and wr_:
            is_lure = (pf_ > 3) and (wr_ > 0.90)
    except Exception:
        pass

    occupants = slot_actives.get((d[1], tf_), [])

    csid_match_re = re.search(r"CSID\s*[=:\s]+(\d+)", notes) or re.search(r"csid[=:\s]+(\d+)", notes)
    csid = csid_match_re.group(1) if csid_match_re else ""
    csid_match_list = csid_actives.get(csid, [])

    metric_ok = (pf_ is not None and pf_ >= 1.3
                 and n_ is not None and n_ >= 20
                 and cost == "v1_realistic_micros"
                 and not is_lure)
    slot_free = len(occupants) == 0

    if is_lure:
        verdict = "LEAVE-DEAD-LURE"
    elif not metric_ok:
        verdict = "LEAVE-DEAD-LOW-EVIDENCE"
    elif slot_free:
        verdict = "RESTORE"
    else:
        verdict = "BLOCKED-ON-SLOT-CONFLICT"

    classified.append({
        "id": int(d[0]),
        "sym": d[1],
        "fam": d[2],
        "ver": d[3],
        "tf": tf_,
        "src_cand": d[4],
        "state": d[5],
        "deact": d[7],
        "BT_PF": pf_,
        "BT_n": n_,
        "BT_WR": wr_,
        "BT_ER": er_,
        "BT_$": pnl_,
        "BT_maxDD": maxdd_,
        "cost_model": cost,
        "span": span_str,
        "is_lure": is_lure,
        "metric_ok": metric_ok,
        "slot_free": slot_free,
        "occupants": occupants,
        "csid": csid,
        "csid_match_count": len(csid_match_list),
        "csid_match_list": csid_match_list,
        "verdict": verdict,
        "notes_first200": notes[:200],
    })

classified.sort(key=lambda r: (r["BT_$"] or 0), reverse=True)

out_path = "_restore_ledger/classified.tsv"
with open(out_path, "w") as f:
    cols = ["id","sym","fam","ver","tf","src_cand","state","deact","BT_PF","BT_n","BT_WR","BT_ER","BT_$","BT_maxDD","cost_model","span","is_lure","metric_ok","slot_free","csid","csid_match_count","verdict","active_match_detail"]
    f.write("\t".join(cols) + "\n")
    for r in classified:
        match_str = ";".join(f"{m['sym']}/{m['tf']}/csid{m['csid']}/{m['fam']}/#{m['active_id']}" for m in r["csid_match_list"])
        f.write("\t".join(str(r[c]) for c in cols[:-1]) + f"\t{match_str}\n")

print(f"\nWrote {len(classified)} classified rows to {out_path}")

totals = defaultdict(int)
totals_d = defaultdict(float)
for r in classified:
    totals[r["verdict"]] += 1
    totals_d[r["verdict"]] += (r["BT_$"] or 0)
print("\nVERDICT TALLY:")
for k in ["RESTORE","BLOCKED-ON-SLOT-CONFLICT","LEAVE-DEAD-LURE","LEAVE-DEAD-LOW-EVIDENCE"]:
    print(f"  {k:32s} {totals[k]:3d}  ${totals_d[k]:10.2f}")
print(f"  TOTAL                         {len(classified):3d}  ${sum(totals_d.values()):10.2f}")

print("\nTOP 25 BY BT $:")
print(f"{'id':5} {'sym':4} {'fam':40} {'tf':4} {'PF':>5} {'n':>5} {'WR':>5} {'ER':>5} {'$':>9} verdict")
for r in classified[:25]:
    print(f"{r['id']:5} {r['sym']:4} {r['fam'][:40]:40} {str(r['tf'])[:4]:4} {(r['BT_PF'] or 0):5.2f} {int(r['BT_n'] or 0):5d} {((r['BT_WR'] or 0)*100):5.1f} {(r['BT_ER'] or 0):5.2f} {(r['BT_$'] or 0):9.2f} {r['verdict']}")

print("\n2026-10-16T10:02 BATCH BREAKDOWN:")
oct16 = [r for r in classified if r["deact"].startswith("2026-10-16T10:02")]
print(f"  total: {len(oct16)}")
for v in sorted(set(r["verdict"] for r in oct16)):
    rows = [r for r in oct16 if r["verdict"] == v]
    pairs = defaultdict(int)
    for r in rows:
        pairs[(r["sym"], r["fam"])] += 1
    print(f"  {v}: {len(rows)} rows, ${sum((r['BT_$'] or 0) for r in rows):.0f}")
    for (s, f_), c in sorted(pairs.items(), key=lambda x: (-x[1], x[0])):
        if c >= 2:
            print(f"      {s:4} {f_[:55]:55} x{c}")

with open("_restore_ledger/classified_full.json", "w") as f:
    json.dump(classified, f, indent=2, default=str)
print("\nDONE")
