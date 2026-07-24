"""
Walk-forward helper for custom backtest strategies.

Splits cached bars into expanding-window train/test folds and runs
run_custom_backtest_on_bars on each. Mirrors the strict gates from the
PM agent: avg_pf>=1.3, total_test_trades>=15, E[R]>0, robustness>=half folds.

Usage:
    python research/wf_custom.py --code-path research/ma_cross_strategy.py \
        --symbol MCL --timeframe 15m --params '{"fast":9,"slow":21,"target_r":3.0,"stop_atr_mult":1.5}'
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.bar_cache import read_cache
from src.research.custom_strategy_runtime import run_custom_backtest_on_bars


def wf_custom(*, code: str, symbol: str, timeframe: str, params: dict,
              n_folds: int = 3, train_frac: float = 0.5, test_frac: float = 0.18) -> dict:
    df = read_cache(symbol, timeframe)
    if df is None or df.empty:
        return {"error": "no bars"}
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.set_index("ts").reset_index()
    n = len(df)
    folds = []
    train_end = int(n * train_frac)
    while train_end + int(n * test_frac) <= n and len(folds) < n_folds:
        test_start = train_end
        test_end = min(int(test_start + n * test_frac), n)
        train_df = df.iloc[:train_end].reset_index(drop=True)
        test_df = df.iloc[test_start:test_end].reset_index(drop=True)
        train_res = run_custom_backtest_on_bars(code=code, bars=train_df, params=params, timeout_s=30)
        test_res = run_custom_backtest_on_bars(code=code, bars=test_df, params=params, timeout_s=30)
        folds.append(dict(
            train_idx=(0, train_end),
            test_idx=(test_start, test_end),
            train=train_res, test=test_res,
        ))
        train_end = test_start + int(n * test_frac)
    if not folds:
        return {"error": "no folds"}

    test_pfs = []
    test_ers = []
    test_n = 0
    test_pnl = 0.0
    for f in folds:
        pf = f["test"].get("pf", 0.0)
        er = f["test"].get("expectancy_r", 0.0)
        test_pfs.append(pf if pf != float("inf") else 999.0)
        test_ers.append(er)
        test_n += f["test"].get("n_signals", 0)
        test_pnl += f["test"].get("total_pnl", 0.0)

    avg_pf = float(np.mean(test_pfs)) if test_pfs else 0.0
    min_pf = float(min(test_pfs)) if test_pfs else 0.0
    avg_er = float(np.mean(test_ers)) if test_ers else 0.0
    robustness = sum(1 for pf in test_pfs if pf >= 1.0)

    return dict(
        symbol=symbol, timeframe=timeframe, params=params,
        n_folds=len(folds),
        avg_test_pf=avg_pf, min_test_pf=min_pf,
        avg_test_er=avg_er, total_test_trades=test_n,
        total_test_pnl=test_pnl, robustness=int(robustness),
        pass_pf=avg_pf >= 1.3,
        pass_trades=test_n >= 15,
        pass_er=avg_er > 0,
        pass_robust=robustness >= int(np.ceil(len(folds) / 2)),
        folds=folds,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--code-path", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--timeframe", default="15m")
    p.add_argument("--params", required=True, help="JSON string")
    p.add_argument("--n-folds", type=int, default=3)
    p.add_argument("--train-frac", type=float, default=0.5)
    p.add_argument("--test-frac", type=float, default=0.18)
    args = p.parse_args()

    code = Path(args.code_path).read_text()
    params = json.loads(args.params)
    res = wf_custom(
        code=code, symbol=args.symbol, timeframe=args.timeframe,
        params=params, n_folds=args.n_folds,
        train_frac=args.train_frac, test_frac=args.test_frac,
    )
    out = {k: v for k, v in res.items() if k != "folds"}
    out["per_fold_summary"] = [
        {
            "train_idx": f["train_idx"], "test_idx": f["test_idx"],
            "train_pf": f["train"].get("pf", 0.0), "test_pf": f["test"].get("pf", 0.0),
            "test_n": f["test"].get("n_signals", 0), "test_er": f["test"].get("expectancy_r", 0.0),
            "test_pnl": f["test"].get("total_pnl", 0.0),
        } for f in res.get("folds", [])
    ]
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()