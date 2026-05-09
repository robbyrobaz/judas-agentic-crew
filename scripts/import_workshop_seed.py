#!/usr/bin/env python3
"""Import workshop strategy seeds and backtest artifacts into this repo."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.db.models import init_db
from src.strategy_registry import activate_seed_strategy
from src.tools.db_tools import db_save_research_experiment_tool

WORKSHOP_ROOT = (REPO_ROOT.parent / "judas-futures-workshop").resolve()
SEED_OUT = REPO_ROOT / "outputs" / "research" / "workshop_seed"
KB_OUT = REPO_ROOT / "knowledge_base" / "buffet.yaml"


ARTIFACTS = [
    "RESEARCH_FINDINGS.md",
    "STATUS.md",
    "buffet.yaml",
    "buffet_top.csv",
    "buffet_pf_ranked.csv",
    "buffet_results.csv",
    "fast_battery_MGC_1h.csv",
    "fast_battery_NQ_1h.csv",
    "sweep_all_results.csv",
    "pairs_results.csv",
]


def _read_csv_rows(path: Path) -> list[dict]:
    import csv

    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _copy_artifacts() -> dict[str, str]:
    SEED_OUT.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    for name in ARTIFACTS:
        src = WORKSHOP_ROOT / name
        if not src.exists():
            continue
        dst = SEED_OUT / name
        shutil.copy2(src, dst)
        copied[name] = str(dst)
        if name == "buffet.yaml":
            shutil.copy2(src, KB_OUT)
    return copied


def _seed_active_strategies() -> list[dict]:
    with open(WORKSHOP_ROOT / "buffet.yaml") as f:
        buffet = yaml.safe_load(f)["strategies"]

    activated: list[dict] = []
    for strat in buffet:
        kind = strat["kind"]
        if kind == "zoo":
            symbol = strat["symbol"]
            strategy_family = f"buffet_strategy:{strat['id']}"
            params = {
                "symbol": symbol,
                "strategy_name": strat["id"],
                "strategy_family": strategy_family,
                "execution_engine": "buffet_zoo",
                "strategy_type": strat["type"],
                "qty": 1,
                **(strat.get("params") or {}),
            }
        elif kind == "pair":
            symbol = "/".join(strat["symbols"])
            strategy_family = f"buffet_strategy:{strat['id']}"
            params = {
                "symbol": symbol,
                "strategy_name": strat["id"],
                "strategy_family": strategy_family,
                "execution_engine": "buffet_pair",
                "qty": 1,
                **(strat.get("params") or {}),
            }
        else:
            continue

        metrics = {
            "profit_factor": float((strat.get("backtest") or {}).get("profit_factor", 0.0)),
            "trades": int((strat.get("backtest") or {}).get("trades", 0)),
            "winrate": float((strat.get("backtest") or {}).get("winrate", 0.0)),
            "total_pnl_dollars": float((strat.get("backtest") or {}).get("pnl_usd", 0.0)),
            "max_drawdown_dollars": float((strat.get("backtest") or {}).get("max_dd_usd", 0.0)),
            "fires_per_month": float(strat.get("fires_per_month", 0.0)),
        }
        strategy_id = activate_seed_strategy(
            symbol=symbol,
            strategy_family=strategy_family,
            params=params,
            metrics=metrics,
            notes="Seeded from judas-futures-workshop buffet.yaml baseline.",
        )
        activated.append({"strategy_id": strategy_id, "strategy_name": strat["id"], "symbol": symbol, "kind": kind})
    return activated


def main() -> int:
    db_path = REPO_ROOT / "judas_crew.db"
    init_db(db_path)
    copied = _copy_artifacts()
    activated = _seed_active_strategies()

    top_rows = []
    top_csv = WORKSHOP_ROOT / "buffet_top.csv"
    if top_csv.exists():
        top_rows = _read_csv_rows(top_csv)

    payload = {
        "symbol": "PORTFOLIO",
        "experiment_type": "workshop_seed_import",
        "name": "Workshop Seed Import",
        "status": "completed",
        "metrics": {
            "copied_artifact_count": len(copied),
            "activated_strategy_count": len(activated),
            "top_csv_rows": len(top_rows),
        },
        "parameters": {"source_repo": str(WORKSHOP_ROOT)},
        "artifacts": copied,
        "summary": "Imported workshop buffet config and backtest artifacts as the baseline incumbent.",
        "recommendations": [
            "Use these active seed strategies as the incumbent paper-trading baseline.",
            "Research should try to beat or replace them, not rediscover them.",
        ],
    }
    saved = json.loads(db_save_research_experiment_tool.run(input_json=json.dumps(payload)))
    print(
        json.dumps(
            {
                "saved_experiment_id": saved.get("experiment_id"),
                "copied": copied,
                "activated": activated,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
