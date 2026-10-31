"""Summarise the 20261031a stage2 walk-forward results per symbol."""
import json
import glob
import os

for f in sorted(glob.glob("research/cycle_20261031a/stage2_*.json")):
    print("=====", os.path.basename(f))
    d = json.load(open(f))
    if isinstance(d, dict) and "results" in d:
        d = d["results"]
    if isinstance(d, dict):
        items = list(d.items())
    else:
        items = [(str(i), v) for i, v in enumerate(d)]
    for k, v in items:
        if not isinstance(v, dict):
            print("  ", k, str(v)[:160])
            continue
        wf = v.get("wf", v)
        folds = [x["pf"] for x in wf.get("folds", [])]
        print(f"  {str(k):32s} minPF={wf.get('min_pf')} meanPF={wf.get('mean_pf')} "
              f"n={wf.get('n_total')} allpos={wf.get('all_pos')} folds={folds}")
