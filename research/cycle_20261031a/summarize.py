"""Summarise the 2026-10-31a coordinate search + stage2 results."""
import json
import glob
import os

base_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(os.path.dirname(os.path.dirname(base_dir)))

for f in sorted(glob.glob("research/cycle_20261031a/coord_*.json")):
    d = json.load(open(f))
    print("==", f, len(d), "cells")
    d.sort(key=lambda r: (-(r.get("min_pf", 0)), -r.get("n_total", 0)))
    for r in d[:12]:
        flag = "PASS" if r.get("all_pos") else "    "
        print(f"{flag} {r.get('axis')}={r.get('value')} minPF={r.get('min_pf')} "
              f"meanPF={r.get('mean_pf')} n={r.get('n_total')} "
              f"folds={[f['pf'] for f in r.get('folds', [])]}")

for f in sorted(glob.glob("research/cycle_20261031a/stage2_*.json")):
    d = json.load(open(f))
    print("==", f, len(d), "cells")
    rows = []
    for r in d:
        wf = r["wf"]
        full = r["full"]
        rows.append((r["name"], wf["min_pf"], wf["mean_pf"], wf["n_total"],
                     wf["all_pos"], full["pf"], full["n_trades"], full["total_pnl"],
                     [x["pf"] for x in wf["folds"]]))
    rows.sort(key=lambda x: (-(x[1]), -x[3]))
    for r in rows[:14]:
        flag = "PASS" if (r[4] and r[3] >= 25) else "    "
        print(f"{flag} {r[0]:<26} minPF={r[1]:.3f} meanPF={r[2]:.3f} nOOS={r[3]:4d} "
              f"full_pf={r[5]:.3f} full_n={r[6]:4d} full_pnl=${r[7]:>9.2f} folds={r[8]}")
