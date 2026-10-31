"""Control check: do the KNOWN-GOOD market-entry strategies survive the
production-fill model?

csid 441 (MGC 5m) and csid 439 (MGC 15m) are the only two custom strategies
in the book that the Registrar promoted after an honest-fill audit
(#9146/#7382/#7383).  If the new production-fill model is calibrated
correctly they must NOT collapse the way the retro-fill library does --
otherwise the model, not the strategies, is what is broken.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import custom_strategy_runtime as csr  # noqa: E402

CONTROLS = [
    ("MGC", "5m", 441),
    ("MGC", "15m", 439),
]

# The params the active rows actually run with.
PARAMS = {
    441: {"lookback": 20, "min_sweep_atr": 0.6, "disp_k": 1.0, "rr": 2.0,
          "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
          "atr_period": 14},
    439: {"lookback": 12, "min_sweep_atr": 0.3, "disp_k": 1.0, "rr": 1.5,
          "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
          "atr_period": 14},
}


def main() -> None:
    conn = sqlite3.connect("judas_crew.db")
    bars_cache = {}
    out = []
    for sym, tf, csid in CONTROLS:
        code = conn.execute(
            "SELECT code FROM custom_strategies WHERE id=?", (csid,)).fetchone()[0]
        if (sym, tf) not in bars_cache:
            bars_cache[(sym, tf)] = csr._fetch_synthetic_or_live_bars(sym, 90, tf)
        df = bars_cache[(sym, tf)]
        params = PARAMS[csid]
        row = {"csid": csid, "symbol": sym, "tf": tf}
        for mode in ("legacy", "production", "production_close"):
            m = csr.run_custom_backtest_on_bars(
                code=code, bars=df, params=dict(params), symbol=sym, qty=1,
                timeout_s=240, fill_mode=mode)
            row[mode] = {k: m.get(k) for k in (
                "error", "n_trades", "n_wins", "n_losses", "total_pnl", "pf",
                "expectancy_r", "stale_fill_pct", "entry_slip_ticks_mean",
                "promotion_grade", "cost_model", "n_bars", "bars_span_days")}
            print(f"csid {csid} {sym} {tf} {mode:16s} n={m.get('n_trades')} "
                  f"pf={m.get('pf')} ${m.get('total_pnl')} "
                  f"E[R]={m.get('expectancy_r')} slip={m.get('entry_slip_ticks_mean')} "
                  f"stale%={m.get('stale_fill_pct')} grade={m.get('promotion_grade')}",
                  flush=True)
        out.append(row)
    Path("outputs/control_fill_check_20261030a.json").write_text(
        json.dumps(out, indent=2, default=str))
    print("wrote outputs/control_fill_check_20261030a.json")


if __name__ == "__main__":
    main()
