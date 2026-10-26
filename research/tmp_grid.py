"""Grid explorer over strategy code x params x symbols (local, free)."""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.tmp_harness import fmt, run  # noqa: E402


def main():
    code_path = sys.argv[1]
    tf = sys.argv[2]
    days = int(sys.argv[3])
    symbols = sys.argv[4].split(",")
    grid = json.loads(sys.argv[5])          # {param: [values]}
    code = Path(code_path).read_text()
    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    rows = []
    for sym in symbols:
        for combo in combos:
            params = dict(zip(keys, combo))
            m = run(code, sym, tf, days, params)
            pf = m.get("pf", 0) or 0
            n = m.get("n_trades", 0)
            er = m.get("expectancy_r", 0)
            tag = ",".join(f"{k}={v}" for k, v in params.items())
            rows.append((pf, n, er, m.get("total_pnl", 0), sym, tag, m))
            print(f"{sym:4s} {tag:60s} {fmt(m)}", flush=True)
    print("\n=== TOP BY PF (n>=20) ===")
    for pf, n, er, pnl, sym, tag, m in sorted(rows, key=lambda r: -r[0]):
        if n >= 20:
            print(f"{sym:4s} PF={pf:.3f} n={n:4d} E[R]={er:+.3f} ${pnl:+8.2f}  {tag}")


if __name__ == "__main__":
    main()
