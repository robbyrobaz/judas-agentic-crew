"""SCREEN: does the edge survive an ACHIEVABLE fill?  (2026-10-25 cycle)

Runs every selected config under three fill modes:
    base  = what the house backtester does today (entry = sig["entry"])
    fair  = entry = close of the decision bar
    live  = entry = open of the bar AFTER the decision bar  (production)

and prints base-vs-live so the fill artefact is visible per config.

Usage:  .venv/bin/python research/cycle_20261025/screen_live_fill.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))

from fast_replay import load_bars, metrics, replay, _patch_state_guard  # noqa: E402

OUT = ROOT / "outputs" / "live_fill_screen_20261025.json"
DB = ROOT / "judas_crew.db"

# (label, symbol, tf) -> which prior engine run to validate BASE against
WANT = [
    ("csid203", "MGC", "15m"),
    ("csid203", "MNQ", "15m"),
    ("csid295", "MGC", "5m"),
    ("csid332", "MGC", "15m"),
    ("csid345", "MNQ", "5m"),
    ("csid345", "MCL", "5m"),
    ("csid346", "MGC", "5m"),
    ("csid346", "MNQ", "5m"),
    ("csid357", "MGC", "5m"),
    ("ASWEEP15", "MGC", "15m"),
    ("ASWEEP15tight", "MGC", "15m"),
    ("csid203", "6J", "15m"),
    ("csid295", "MNQ", "5m"),
    ("csid345", "MGC", "5m"),
    ("csid332", "MNQ", "15m"),
    ("csid346", "6J", "5m"),
]

FILE_CODE = {
    "ASWEEP15": ROOT / "research" / "asian_sweep_continuation_fvg_15m_v1.py",
    "ASWEEP15tight": ROOT / "research" / "asian_sweep_continuation_fvg_15m_v1.py",
    "ASWEEP5": ROOT / "research" / "asian_sweep_continuation_fvg_15m_v1.py",
    "AMD15": ROOT / "research" / "amd_midpoint_disp_continuation_15m_v1.py",
}


def code_for(label: str) -> str | None:
    if label in FILE_CODE and FILE_CODE[label].exists():
        return FILE_CODE[label].read_text()
    if label.startswith("csid"):
        with sqlite3.connect(DB) as conn:
            row = conn.execute("SELECT code FROM custom_strategies WHERE id=?",
                               (int(label[4:]),)).fetchone()
        return row[0] if row else None
    return None


def collect_combos():
    """Pull the exact (params, days) the previous cycle used."""
    combos = []
    for f in sorted((ROOT / "research").glob("refill_combos_b*.json")):
        for spec, sym, tf, params, days in json.loads(f.read_text()):
            lab = spec.get("name") or f"csid{spec.get('csid')}"
            combos.append({"label": lab, "symbol": sym, "tf": tf,
                           "params": params, "days": days, "src": f.name})
    return combos


def main() -> int:
    combos = collect_combos()
    seen = set()
    rows = []
    for label, sym, tf in WANT:
        key = (label, sym, tf)
        if key in seen:
            continue
        seen.add(key)
        cands = [c for c in combos if c["label"] == label and c["symbol"] == sym
                 and c["tf"] == tf]
        if not cands:
            print(f"!! no combo for {label} {sym} {tf}")
            continue
        raw = code_for(label)
        if not raw:
            print(f"!! no code for {label}")
            continue
        code = _patch_state_guard(raw)
        for c in cands:
            bars = load_bars(sym, tf, days=int(c["days"]))
            if bars is None or len(bars) < 50:
                print(f"!! no bars {sym} {tf}")
                continue
            row = {"label": label, "symbol": sym, "tf": tf,
                   "params": c["params"], "days": c["days"],
                   "n_bars": int(len(bars)),
                   "first_bar": str(bars["ts"].iloc[0]), "last_bar": str(bars["ts"].iloc[-1])}
            for mode in ("base", "fair", "live"):
                trades, meta = replay(code, bars, c["params"], sym, mode=mode)
                m = metrics(trades or [])
                row[mode] = m
                row[mode + "_meta"] = {k: meta.get(k) for k in (
                    "n_signals", "eval_errors", "timed_out", "loop_s",
                    "pct_entry_outside_decision_bar", "pct_entry_is_decision_close",
                    "entry_offset_x_bar_range_median", "cost_model")}
            rows.append(row)
            b, f, lv = row["base"], row["fair"], row["live"]
            print(f"{label:14s} {sym:4s} {tf:3s} | base n={b['n']:5d} pf={b['pf']:6} "
                  f"E[R]={b['e_r']:6} $={b['total_usd']:10.0f} | fair n={f['n']:5d} "
                  f"pf={f['pf']:6} E[R]={f['e_r']:6} | live n={lv['n']:5d} "
                  f"pf={lv['pf']:6} E[R]={lv['e_r']:6} $={lv['total_usd']:9.0f} | "
                  f"outside={row['base_meta'].get('pct_entry_outside_decision_bar')}%",
                  flush=True)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=2, default=str))
    print(f"\nwrote {OUT} ({len(rows)} configs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
