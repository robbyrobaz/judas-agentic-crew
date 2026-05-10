"""Regression: Phase 0 sweep loop fix.

The 2026-05-09 production timeout was caused by the default Judas parameter
sweep grid having 1728 combinations -- far too many to finish within the
45-min run. Worse, `research_experiments` was only persisted at the end so
a timeout produced zero rows. This test pins:

- A small explicit grid completes in well under 60 seconds on a modest bar
  slice.
- Exactly one `research_experiments` row is written when the tool returns
  successfully.
- The new `max_combinations` cap surfaces a `truncated` flag in the payload.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def temp_research_db(tmp_path, monkeypatch):
    db_path = tmp_path / "judas_crew_test.db"
    monkeypatch.setenv("JUDAS_DB_PATH", str(db_path))
    from src.db.models import init_db

    init_db(str(db_path))
    return str(db_path)


def _has_workshop_parquet(symbol: str = "MGC") -> bool:
    workshop = Path(os.environ.get("JUDAS_WORKSHOP_PATH", "/home/rob/judas-futures-workshop"))
    return (workshop / "cache_1h" / f"{symbol}_1h.parquet").exists()


@pytest.mark.skipif(not _has_workshop_parquet(), reason="workshop MGC parquet not available")
def test_sweep_returns_within_60s_and_persists_one_row(temp_research_db):
    """Tiny grid finishes fast and writes exactly one experiment row."""
    from src.tools.research_tools import judas_parameter_sweep_tool

    payload = {
        "symbol": "MGC",
        # Trim the bar slice so the inner per-combo loop cannot blow past
        # the 60s budget even on a slow runner.
        "bars_lookback": 720,  # ~30 trading days of 1H bars.
        "min_trades": 0,
        "grid": {
            "target_r": [2.0],
            "stop_buffer_ticks": [2],
            "min_sweep_ticks": [3],
            "min_displacement_strength": [1.0],
            "min_displacement_body_ratio": [0.5],
            "max_sweep_age_bars": [2],
            "require_fvg": [False],
            "session_filter": ["all"],
        },
    }

    t0 = time.time()
    raw = judas_parameter_sweep_tool.run(input_json=json.dumps(payload))
    elapsed = time.time() - t0

    assert elapsed < 60.0, f"sweep took {elapsed:.1f}s, expected <60s"

    result = json.loads(raw)
    assert result["symbol"] == "MGC"
    assert result["evaluated_combinations"] == 1
    assert result["truncated"] is False
    assert result["experiment_id"] is not None

    with sqlite3.connect(temp_research_db) as conn:
        rows = conn.execute(
            "SELECT id, experiment_type FROM research_experiments"
        ).fetchall()
    assert len(rows) == 1, f"expected exactly one research_experiments row, got {rows}"
    assert rows[0][1] == "judas_threshold_sweep"


def test_sweep_truncates_when_grid_exceeds_cap(temp_research_db, monkeypatch):
    """Cap engages and `truncated` flips; we don't need real bars for this."""
    import pandas as pd

    from src.tools import research_tools

    # Stub bar loader and the heavy per-combo evaluation so the cap-test
    # stays deterministic and fast even without workshop parquet.
    fake_bars = pd.DataFrame(
        {"ts": pd.date_range("2024-01-01", periods=200, freq="h", tz="UTC"),
         "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0}
    )
    monkeypatch.setattr(research_tools, "_load_bars", lambda symbol: fake_bars)
    monkeypatch.setattr(
        research_tools,
        "_evaluate_judas_variant",
        lambda bars, symbol, params: {
            "metrics": {
                "trades": 0, "wins": 0, "losses": 0, "winrate": 0.0,
                "profit_factor": 0.0, "avg_r": 0.0, "expectancy_r": 0.0,
                "total_pnl_dollars": 0.0, "max_drawdown_dollars": 0.0,
                "sharpe_ish": 0.0, "recovery_trades": None,
                "skipped_signals": 0, "a_plus_trade_count": 0,
                "a_plus_winrate": 0.0, "rank_score": 0.0,
            },
            "trades": [],
        },
    )

    payload = {
        "symbol": "MGC",
        "min_trades": 0,
        "max_combinations": 4,
        "grid": {
            "target_r": [1.5, 2.0, 2.5],
            "stop_buffer_ticks": [2, 3],
            "min_sweep_ticks": [3, 5],  # 3 * 2 * 2 = 12 combos > cap of 4
            "min_displacement_strength": [1.0],
            "min_displacement_body_ratio": [0.5],
            "max_sweep_age_bars": [2],
            "require_fvg": [False],
            "session_filter": ["all"],
        },
    }

    raw = research_tools.judas_parameter_sweep_tool.run(input_json=json.dumps(payload))
    result = json.loads(raw)
    assert result["total_grid_combinations"] == 12
    assert result["evaluated_combinations"] == 4
    assert result["truncated"] is True
