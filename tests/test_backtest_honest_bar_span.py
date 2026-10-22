"""Regression test: a backtest envelope must state the window it ACTUALLY replayed.

`metrics_json.days` is the *requested* window, not the realised one. For 5m/15m
the realised span is far shorter than requested unless the deep paginated
archive (`src/bar_archive.py`) has been built -- see findings 9105 / 9108 /
9109. PF / WR / E[R] were always computed correctly; what the old stamp hid
was the WIDTH of the fit, which is exactly what a reader needs in order not to
mistake a 13-day fit for a 90-day validation.

These tests pin:
  * the envelope carries n_bars / bars_span_days / first_bar / last_bar
  * bars_span_days is derived from the bars actually replayed
  * a frame with no `ts` column degrades safely instead of raising
  * an empty frame still reports n_bars = 0
"""
from __future__ import annotations

import pandas as pd

from src.research import custom_strategy_runtime as csr


def _bars(n: int, freq: str = "5min") -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "ts": idx,
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [100] * n,
        }
    )


def test_span_fields_reflect_replayed_bars():
    # 12 days of 5m bars.
    df = _bars(12 * 288)
    got = csr._bar_span_fields(df)
    assert got["n_bars"] == 12 * 288
    assert got["bars_span_days"] == 11  # 12 days of bars => 11 full days elapsed
    assert got["first_bar"] is not None and got["last_bar"] is not None


def test_short_window_is_reported_honestly_not_stamped_as_requested():
    """The 13-day fit must not be able to read as a 90-day envelope."""
    df = _bars(13 * 288)
    got = csr._bar_span_fields(df)
    assert got["bars_span_days"] < 90
    # This is the whole point: a reader can tell the fit is narrow.
    assert got["n_bars"] < 288 * 90


def test_missing_ts_column_degrades_safely():
    df = _bars(100).drop(columns=["ts"])
    got = csr._bar_span_fields(df)
    assert got["n_bars"] == 100
    assert got["bars_span_days"] == 0
    assert got["first_bar"] is None


def test_empty_bars_are_safe():
    got = csr._bar_span_fields(_bars(0))
    assert got["n_bars"] == 0
    assert got["bars_span_days"] == 0
    assert csr._bar_span_fields(None)["n_bars"] == 0


def test_envelope_includes_span_fields():
    """The fields must actually reach the returned metrics dict."""
    df = _bars(3000)
    src = ("def evaluate(bars, params):\n"
           "    return None\n")
    res = csr.run_custom_backtest_on_bars(code=src, bars=df, symbol="MGC")
    for k in ("n_bars", "bars_span_days", "first_bar", "last_bar"):
        assert k in res, f"missing honest-window field {k!r} in envelope"
    assert res["n_bars"] == 3000