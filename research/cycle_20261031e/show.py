"""Print the 20261031d hygiene/audit results compactly."""
import json
import glob

for f in sorted(glob.glob("research/cycle_20261031d/hyg_*.json")) + sorted(glob.glob("research/cycle_20261031d/audit_*.json")):
    print("=====", f)
    d = json.load(open(f))
    for k, v in d.items():
        if not isinstance(v, dict):
            print(" ", k, str(v)[:200])
            continue
        wf = v.get("wf", {})
        folds = [x["pf"] for x in wf.get("folds", [])]
        print(f"  {k:34s} minPF={wf.get('min_pf')} meanPF={wf.get('mean_pf')} "
              f"n={wf.get('n_total')} folds={folds} over={v.get('over')}")
