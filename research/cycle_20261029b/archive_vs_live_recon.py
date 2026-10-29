"""ARCHIVE vs LIVE-CACHE contract reconciliation — the decisive test.

The deep archive is merged into every research backtest
(`custom_strategy_runtime._merge_deep_archive`):
    merged = concat([archive, live_cache]).drop_duplicates(ts, keep="last")
`keep="last"` means the LIVE cache wins on overlapping timestamps.  So on the
overlap the backtest sees live prices; everywhere else it sees ARCHIVE prices.

If the archive was built on a DIFFERENT contract than the live book (or on a
thin/expired one), the stitched series has a PRICE DISCONTINUITY at the seam.
A price discontinuity is indistinguishable from a real gap to every
sweep/displacement/FVG detector, so the backtest is fed FAKE DISPLACEMENT
EVENTS exactly where the deep history meets the live window.

This script:
  1. pulls the live cache for each cell,
  2. pulls the archive for the same cell,
  3. compares prices on the OVERLAP (should be identical if same contract),
  4. measures the price step across the seam (archive-last vs live-first),
  5. reports how many bars of each side survive the merge and how much of the
     merged series comes from the archive alone.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src import bar_archive, bar_cache  # noqa: E402
from src.config import load_config  # noqa: E402

SYMS = ["MGC", "MNQ", "MCL", "ZF", "6J"]
TFS = ["5m", "15m", "1h"]
TICK = {"MGC": 0.10, "MNQ": 0.25, "MCL": 0.01, "ZF": 1 / 32, "6J": 0.0000005}
TICKVAL = {"MGC": 10.0, "MNQ": 2.0, "MCL": 10.0, "ZF": 1000.0, "6J": 125000.0}


def main() -> None:
    cfg = load_config()
    ib = cfg.ibkr
    cid = int(ib.data_client_id) + 9
    rows = []

    for sym in SYMS:
        for tf in TFS:
            arch = bar_archive.read_archive(sym, tf)
            if arch.empty:
                continue
            try:
                live = bar_cache.get_bars(sym, host=ib.host, port=ib.port,
                                          client_id=cid, timeframe=tf)
            except Exception as exc:  # noqa: BLE001
                live = None
                live_err = f"{type(exc).__name__}"
            else:
                live_err = None
            if live is None or live.empty:
                rows.append({"cell": f"{sym}_{tf}", "live": "EMPTY", "err": live_err})
                print(f"{sym}_{tf:<9} live EMPTY ({live_err})")
                continue

            a = arch.copy()
            l = live.copy()
            a["ts"] = pd.to_datetime(a["ts"], utc=True)
            l["ts"] = pd.to_datetime(l["ts"], utc=True)
            a = a.sort_values("ts").reset_index(drop=True)
            l = l.sort_values("ts").reset_index(drop=True)

            merged = pd.concat([a, l], ignore_index=True)
            merged = merged.drop_duplicates(subset=["ts"], keep="last").sort_values("ts").reset_index(drop=True)
            n_arch_only = int(len(merged) - len(l))

            # --- overlap price comparison ---
            m = a.merge(l, on="ts", suffixes=("_a", "_l"))
            if len(m):
                diff = (m["close_a"] - m["close_l"]).abs()
                n_mismatch = int((diff > 1e-9).sum())
                max_diff = float(diff.max())
                med_diff = float(diff.median())
            else:
                n_mismatch, max_diff, med_diff = -1, float("nan"), float("nan")

            # --- seam step: last archive bar close vs first live bar close ---
            a_last_close = float(a["close"].iloc[-1])
            l_first_close = float(l["close"].iloc[0])
            # the archive rows that survive the merge (live has no ts there)
            live_ts = set(l["ts"])
            a_only = a[~a["ts"].isin(live_ts)]
            if len(a_only):
                seam_prev = float(a_only["close"].iloc[-1])
            else:
                seam_prev = a_last_close
            seam_step = l_first_close - seam_prev
            dpp = TICKVAL[sym] / TICK[sym]
            # typical 1-bar move for scale
            rets = l["close"].diff().abs().dropna()
            typical = float(rets.median()) if len(rets) else float("nan")

            rec = {
                "cell": f"{sym}_{tf}",
                "arch_n": int(len(a)), "live_n": int(len(l)), "merged_n": int(len(merged)),
                "arch_only_n": n_arch_only,
                "overlap_n": int(len(m)),
                "overlap_mismatch_n": n_mismatch,
                "overlap_max_diff": max_diff,
                "overlap_med_diff": med_diff,
                "arch_first": str(a["ts"].iloc[0]), "arch_last": str(a["ts"].iloc[-1]),
                "live_first": str(l["ts"].iloc[0]), "live_last": str(l["ts"].iloc[-1]),
                "arch_last_close": a_last_close,
                "live_first_close": l_first_close,
                "seam_step_pts": round(float(seam_step), 6),
                "seam_step_dollars": round(float(seam_step) * dpp, 2),
                "typical_1bar_move": round(float(typical), 6),
                "seam_in_typical_units": (round(float(seam_step) / typical, 1)
                                          if typical and typical == typical and typical != 0 else None),
                "arch_synth_pct": round(100.0 * float(((a["volume"] <= 0)).sum()) / len(a), 1),
                "live_synth_pct": round(100.0 * float(((l["volume"] <= 0)).sum()) / len(l), 1),
            }
            rows.append(rec)
            print(f"{rec['cell']:<9} arch={rec['arch_n']:<6} live={rec['live_n']:<5} "
                  f"merged={rec['merged_n']:<6} overlap={rec['overlap_n']:<5} "
                  f"MISMATCH={rec['overlap_mismatch_n']:<5} maxdiff={rec['overlap_max_diff']:<12} "
                  f"| seam ${rec['seam_step_dollars']:<10} = {rec['seam_in_typical_units']}x typical move "
                  f"| synth arch={rec['arch_synth_pct']}% live={rec['live_synth_pct']}%", flush=True)

    Path("outputs/archive_vs_live_reconciliation_20261029.json").write_text(
        json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main()
