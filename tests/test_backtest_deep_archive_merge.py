"""Regression test: the research backtest path unions the deep paginated archive.

Findings 9105/9109 established that ``bar_cache`` can only ever return ~13-27
days of 5m/15m (a single-request-size ceiling), while ``src/bar_archive.py``
reaches ~120 days by paginating backwards. ``_fetch_synthetic_or_live_bars``
used to consult ONLY the cache, so every ``days=90`` envelope in the book was
silently stamped over a ~2 week window.

These tests pin the union behaviour:
  * archive present  -> depth comes from the archive, freshness from the cache
  * archive missing  -> cache returned unchanged (graceful fallback)
  * no live cache    -> archive returned
  * live scanner untouched
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.research import custom_strategy_runtime as csr


def _bars(n: int, start: int = 0) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "ts": idx,
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [100] * n,
        }
    )


def test_union_takes_depth_from_archive_and_freshness_from_cache(monkeypatch):
    # Archive: 2000 bars of OLD history. Cache: 100 bars of FRESH history.
    arch = _bars(2000).assign(ts=lambda d: d["ts"] + pd.Timedelta(days=-30))
    live = _bars(100)
    monkeypatch.setattr(
        csr, "_read_archive", lambda s, tf: arch.copy(), raising=False
    )

    out = csr._merge_deep_archive("MGC", "5m", live.copy())

    assert len(out) == 2100, "union should hold BOTH windows, deduped by ts"
    assert out["ts"].is_monotonic_increasing, "merged frame must be time-sorted"
    # Archive contributes the depth...
    assert out["ts"].min() < arch["ts"].min() or len(out) > len(arch)
    # ...cache contributes the freshest bars.
    assert out["ts"].max() == live["ts"].max()


def test_missing_archive_falls_back_to_cache_unchanged(monkeypatch):
    live = _bars(120)
    monkeypatch.setattr(
        csr, "_read_archive", lambda s, tf: pd.DataFrame(), raising=False
    )

    out = csr._merge_deep_archive("MNQ", "5m", live.copy())

    assert len(out) == 120
    assert out["ts"].max() == live["ts"].max()


def test_no_live_cache_returns_archive(monkeypatch):
    arch = _bars(500)
    monkeypatch.setattr(
        csr, "_read_archive", lambda s, tf: arch.copy(), raising=False
    )

    out = csr._merge_deep_archive("ZF", "15m", None)

    assert len(out) == 500


def test_both_empty_returns_empty(monkeypatch):
    monkeypatch.setattr(
        csr, "_read_archive", lambda s, tf: pd.DataFrame(), raising=False
    )
    out = csr._merge_deep_archive("MCL", "1h", pd.DataFrame())
    assert out is not None and len(out) == 0


def test_archive_error_does_not_break_backtest(monkeypatch):
    """A corrupt/missing archive must never break the research backtest."""

    def _boom(s, tf):
        raise OSError("parquet unreadable")

    monkeypatch.setattr(csr, "_read_archive", _boom, raising=False)
    live = _bars(60)
    out = csr._merge_deep_archive("6J", "5m", live.copy())
    assert len(out) == 60


def test_live_scanner_does_not_import_archive():
    """The live signal path must stay on bar_cache (finding 9109 scope note)."""
    src = (
        __import__("pathlib").Path("src/portfolio_runtime.py").read_text()
    )
    assert "bar_archive" not in src, (
        "live portfolio_runtime must not read the deep archive — "
        "that would change live indicator warmup for all active strategies"
    )


def test_fetch_is_wired_to_merge(monkeypatch):
    """Guard against the merge being written but never called again."""
    import inspect

    src = inspect.getsource(csr._fetch_synthetic_or_live_bars)
    assert "_merge_deep_archive" in src