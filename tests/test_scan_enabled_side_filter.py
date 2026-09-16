"""enabled=false pauses a strategy; side_filter drops wrong-side fires (2026-09-16).
Before this, both params were forwarded to custom code and otherwise ignored."""
from __future__ import annotations

from src.portfolio_runtime import (
    ActiveFire, apply_side_filter, side_filter_of, strategy_is_enabled,
)


def _fire(direction: str, sid: int = 1) -> ActiveFire:
    return ActiveFire(
        strategy_id=sid, strategy_name="s", strategy_family="custom_5m",
        strategy_version=1, symbol="MGC", direction=direction,
        entry=100.0, stop=99.0, target=102.0, qty=1, rationale="t", features={},
    )


def test_strategy_is_enabled_defaults_true_and_reads_strings():
    assert strategy_is_enabled({}) is True
    assert strategy_is_enabled({"enabled": True}) is True
    assert strategy_is_enabled({"enabled": False}) is False
    assert strategy_is_enabled({"enabled": "false"}) is False
    assert strategy_is_enabled({"enabled": "0"}) is False
    assert strategy_is_enabled({"enabled": "paused"}) is False
    assert strategy_is_enabled({"enabled": "true"}) is True


def test_side_filter_normalisation():
    assert side_filter_of({}) is None
    assert side_filter_of({"side_filter": "both"}) is None
    assert side_filter_of({"side_filter": "long_only"}) == "long"
    assert side_filter_of({"side_filter": "SHORT"}) == "short"
    assert side_filter_of({"side_filter": "garbage"}) is None


def test_apply_side_filter_drops_wrong_side_only():
    fires = [_fire("long"), _fire("short")]
    kept = apply_side_filter({"side_filter": "long_only"}, fires)
    assert [f.direction for f in kept] == ["long"]
    kept = apply_side_filter({"side_filter": "short_only"}, fires)
    assert [f.direction for f in kept] == ["short"]
    assert apply_side_filter({}, fires) == fires
    # pair engines are never filtered
    assert apply_side_filter({"side_filter": "long_only",
                              "execution_engine": "buffet_pair"}, fires) == fires
