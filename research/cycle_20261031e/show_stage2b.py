"""Print 20261031a stage2 results WITH candidate names + archive coverage."""
import json
import glob
import os
import sys

sys.path.insert(0, "research/cycle_20261031a")
from stage2 import CANDS  # noqa: E402

for f in sorted(glob.glob("research/cycle_20261031a/stage2_*.json")):
    print("=====", os.path.basename(f))
    d = json.load(open(f))
    rows = d["results"] if isinstance(d, dict) and "results" in d else d
    if isinstance(rows, dict):
        rows = list(rows.values())
    for k, v in enumerate(rows):
        if not isinstance(v, dict):
            print("  ", k, str(v)[:200])
            continue
        wf = v.get("wf", v)
        name = v.get("name") or (CANDS[k][0] if k < len(CANDS) else str(k))
        folds = [x["pf"] for x in wf.get("folds", [])]
        flag = "PASS" if wf.get("all_pos") and (wf.get("n_total") or 0) >= 20 else "    "
        print(f"  {flag} {name:26s} minPF={wf.get('min_pf')} meanPF={wf.get('mean_pf')} "
              f"n={wf.get('n_total')} folds={folds}")
