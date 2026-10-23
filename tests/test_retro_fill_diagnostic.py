"""Regression tests for the retro-fill (stale-fill) diagnostic.

Finding 07f3a4c2 follow-up, task #2867.

The first version of this counter shipped a FALSE ALL-CLEAR: it read
``stale_fill_pct: 0.0`` and ``backtest_fill_valid: true`` for EVERY strategy,
including ones that fill on 95% stale bars. The cause was a bare
``except: pass`` around a subtraction that always raised --
``bars.iloc[i]["ts"]`` is tz-aware, but the ``numpy.datetime64`` scalars a
strategy reads out of ``bars["ts"].values`` are tz-naive, so
``pd.Timestamp(aware) - pd.Timestamp(naive)`` is a TypeError.

The lesson these tests lock in: a diagnostic that cannot be measured must
report UNKNOWN, never clean.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research.custom_strategy_runtime import _stale_gap_seconds


def _bars(n: int = 5) -> pd.DataFrame:
    ts = pd.date_range("2026-10-21T15:00:00Z", periods=n, freq="5min")
    return pd.DataFrame({
        "ts": ts,
        "open": np.arange(n, dtype=float) + 100.0,
        "high": np.arange(n, dtype=float) + 101.0,
        "low": np.arange(n, dtype=float) + 99.0,
        "close": np.arange(n, dtype=float) + 100.5,
    })


def test_tz_naive_numpy_scalar_is_comparable_to_tz_aware_column():
    """The exact case that broke: strategy returns bars['ts'].values[i]."""
    bars = _bars()
    decision = bars.iloc[-1]["ts"]          # tz-aware Timestamp
    naive_scalar = bars["ts"].values[-2]     # tz-naive datetime64

    gap = _stale_gap_seconds(decision, naive_scalar)

    assert gap is not None, "measurement must not be silently dropped"
    # 5 minutes apart.
    assert gap == pytest.approx(300.0)


def test_same_bar_is_zero_not_none():
    bars = _bars()
    assert _stale_gap_seconds(bars.iloc[-1]["ts"], bars.iloc[-1]["ts"]) == 0.0


def test_stale_bar_reports_positive_gap():
    bars = _bars()
    gap = _stale_gap_seconds(bars.iloc[-1]["ts"], bars["ts"].values[0])
    assert gap == pytest.approx(4 * 300.0)


def test_unmeasurable_returns_none_not_zero():
    """Unknown must be None. Returning 0 would read as 'clean'."""
    assert _stale_gap_seconds(pd.Timestamp("2026-10-21T15:00:00Z"),
                             "not-a-timestamp") is None
    assert _stale_gap_seconds(None, None) is None


def test_backtest_stamps_contamination_and_never_a_false_all_clear():
    """End-to-end: a strategy that fills on an old bar must NOT stamp clean."""
    from src.research.custom_strategy_runtime import run_custom_backtest_on_bars

    code = (
        "import numpy as np\nimport pandas as pd\n"
        "def evaluate(bars, params):\n"
        "    n = len(bars)\n"
        "    if n < 4:\n"
        "        return None\n"
        "    # Fill on a bar from 3 bars ago -> retro/stale by construction.\n"
        "    k = n - 4\n"
        "    return {'ts': bars['ts'].values[k], 'direction': 'long',\n"
        "            'entry': float(bars['open'].iloc[k]),\n"
        "            'stop': float(bars['low'].iloc[k]) - 1.0,\n"
        "            'target': float(bars['open'].iloc[k]) + 1.0}\n"
    )
    bars = _bars(400)
    res = run_custom_backtest_on_bars(code=code, bars=bars,
                                      params={}, symbol="MGC", qty=1,
                                      timeout_s=60)

    assert res.get("error") is None
    assert res["n_signals"] > 0, "fixture must actually fire"
    assert res["stale_fill_n"] == res["n_signals"], \
        "every signal is filled 3 bars back -- all must count as stale"
    assert res["stale_fill_pct"] == pytest.approx(100.0)
    assert res["stale_measure_fail_n"] == 0, "measurement succeeded here"
    assert res["backtest_fill_valid"] is False, \
        "THIS is the false all-clear: it used to stamp True for everything"


def test_clean_strategy_stamps_valid():
    from src.research.custom_strategy_runtime import run_custom_backtest_on_bars

    code = (
        "import numpy as np\nimport pandas as pd\n"
        "def evaluate(bars, params):\n"
        "    n = len(bars)\n"
        "    if n < 4:\n"
        "        return None\n"
        "    # Fill on the DECISION bar -> legitimate.\n"
        "    return {'ts': bars['ts'].values[-1], 'direction': 'long',\n"
        "            'entry': float(bars['open'].iloc[-1]),\n"
        "            'stop': float(bars['open'].iloc[-1]) - 1.0,\n"
        "            'target': float(bars['open'].iloc[-1]) + 1.0}\n"
    )
    bars = _bars(400)
    res = run_custom_backtest_on_bars(code=code, bars=bars,
                                      params={}, symbol="MGC", qty=1,
                                      timeout_s=60)

    assert res.get("error") is None
    assert res["n_signals"] > 0
    assert res["stale_fill_n"] == 0
    assert res["stale_measure_fail_n"] == 0
    assert res["backtest_fill_valid"] is True