"""Regression test: IBKR no-trade filler must not reach the backtest maths.

Finding (2026-10-29, cycle_20261029b): IBKR's TRADES feed emits a bar for
every bucket in the requested window, including buckets in which NOTHING
traded -- OHLC all equal to the previous close, volume == 0.  The deep
paginated archive is mostly filler in its OLD region (MGC_5m: 81% of
Nov-2025 -> 0% of Sep/Oct-2026; ZF_5m: 98% of May-2026 -> 0% of Oct-2026;
MCL_15m: 95% -> 0%).

`custom_strategy_runtime._merge_deep_archive` used to union the archive with
the live cache WITHOUT dropping that filler, so:

  * ATR computed over the replayed series was DEFLATED (measured
    clean/raw ATR ratio: ZF_5m 1.65x, 6J_5m 1.90x) -- the strategy was
    tuned against a bar smaller than the market produced, while the live
    scanner (which reads bar_cache only, 0% filler in the recent window)
    sees the TRUE bar;
  * a carried-forward high could be "swept" by a later real bar, feeding
    phantom liquidity-sweep signals to the detector.

These tests pin:
  * filler (volume<=0 AND high<=low) is dropped from the ARCHIVE contribution
  * a REAL single-print bar (range==0 but volume>0) is KEPT -- thin markets
    legitimately trade one price for a whole bucket
  * the live cache is never filtered (live semantics unchanged)
  * the envelope stamps how much filler was removed
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.research import custom_strategy_runtime as csr


def _frame(rows):
    return pd.DataFrame(
        rows,
        columns=["ts", "open", "high", "low", "close", "volume"],
    )


def _mk(n: int, start: int = 0, volume: int = 100) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "ts": idx,
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [volume] * n,
        }
    )


def test_filler_bars_are_dropped():
    df = _frame(
        [
            ("2026-01-01 00:00:00+00:00", 100, 101, 99, 100, 50),   # real
            ("2026-01-01 00:05:00+00:00", 100, 100, 100, 100, 0),    # FILLER
            ("2026-01-01 00:10:00+00:00", 101, 102, 100, 101, 30),  # real
        ]
    )
    out, n_dropped = csr._drop_filler_bars(df)
    assert n_dropped == 1
    assert len(out) == 2
    assert list(out["volume"]) == [50, 30]


def test_real_single_print_bar_is_kept():
    """range == 0 with volume > 0 is a genuine one-print trade, not filler."""
    df = _frame(
        [
            ("2026-01-01 00:00:00+00:00", 100, 100, 100, 100, 1),  # real single print
            ("2026-01-01 00:05:00+00:00", 100, 100, 100, 100, 0),  # filler
        ]
    )
    out, n_dropped = csr._drop_filler_bars(df)
    assert n_dropped == 1
    assert len(out) == 1
    assert float(out["volume"].iloc[0]) == 1.0


def test_no_filler_is_a_no_op():
    df = _mk(20)
    out, n_dropped = csr._drop_filler_bars(df)
    assert n_dropped == 0
    assert len(out) == 20


def test_merge_drops_archive_filler_but_keeps_live_cache_untouched(monkeypatch):
    arch = _mk(100).assign(ts=lambda d: d["ts"] + pd.Timedelta(days=-30))
    arch.loc[0:49, "volume"] = 0          # first half of the ARCHIVE is filler
    arch.loc[0:49, "high"] = arch.loc[0:49, "low"] = arch.loc[0:49, "close"]
    live = _mk(20)
    live.loc[0:4, "volume"] = 0           # live cache has filler too -- NOT filtered
    live.loc[0:4, "high"] = live.loc[0:4, "low"] = live.loc[0:4, "close"]
    monkeypatch.setattr(csr, "_read_archive", lambda s, tf: arch.copy(), raising=False)

    out = csr._merge_deep_archive("MGC", "5m", live.copy())

    # archive contributes 50 clean bars, live contributes all 20 (unfiltered)
    assert len(out) == 70
    live_rows = out[out["ts"] >= live["ts"].min()]
    assert len(live_rows) == 20, "live cache must be passed through unfiltered"
    assert int((live_rows["volume"] <= 0).sum()) == 5


def test_merge_stamps_the_filler_census(monkeypatch):
    arch = _mk(100).assign(ts=lambda d: d["ts"] + pd.Timedelta(days=-30))
    arch.loc[0:24, "volume"] = 0
    arch.loc[0:24, "high"] = arch.loc[0:24, "low"] = arch.loc[0:24, "close"]
    monkeypatch.setattr(csr, "_read_archive", lambda s, tf: arch.copy(), raising=False)

    csr._merge_deep_archive("ZF", "5m", _mk(10))
    fields = csr._filler_fields("ZF")
    assert fields.get("archive_filler_dropped_n") == 25
    assert fields.get("archive_filler_dropped_pct") == 25.0


def test_empty_and_malformed_frames_do_not_raise():
    assert csr._drop_filler_bars(None)[1] == 0
    assert csr._drop_filler_bars(pd.DataFrame())[1] == 0
    weird = pd.DataFrame({"ts": [1, 2, 3]})
    out, n = csr._drop_filler_bars(weird)
    assert n == 0
    assert len(out) == 3


def test_bar_span_fields_expose_the_widest_gap():
    """After filler is dropped the series is time-compressed -- the widest
    remaining gap must be visible to the reader."""
    df = _mk(10)
    df.loc[5, "ts"] = df.loc[5, "ts"] + pd.Timedelta(hours=3)
    fields = csr._bar_span_fields(df)
    # the gap is 3h PLUS the normal 5-minute step to the next bar
    assert fields["max_bar_gap_min"] == 185.0
