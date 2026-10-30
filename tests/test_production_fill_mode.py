"""Production-fill (decision-time entry) regression tests.

Finding 9164 / 07f3a4c2 -- the RETRO-FILL DEFECT.

The custom backtest engine used to credit an entry at the price the
STRATEGY DERIVED, on the bar the strategy had already closed.  That grants
the strategy its perfect price on every trade, which is the upstream cause of
the whole "PF>3 + 90% winrate" BT-LURE envelope family.

The runtime does NOT do this.  ``portfolio_runtime._build_bracket_orders``
places a **MarketOrder** parent with a LIMIT take-profit and a STOP
stop-loss, so:

  * the entry fills at the next price the market offers after the decision
    (the OPEN of bar T+1 when the decision was taken on the close of bar T);
  * a stop already breached at that open fills at the OPEN (a stop-MARKET
    triggers into the gap), not at the stop level;
  * a bar spanning both stop and target books the STOP (adverse-first).

These tests lock that behaviour in, and lock the ``legacy`` mode to its
historical numbers so the two models can be compared without ambiguity.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research.custom_strategy_runtime import run_custom_backtest_on_bars


def _bars(rows) -> pd.DataFrame:
    """Build a bar frame from (open, high, low, close) tuples."""
    n = len(rows)
    ts = pd.date_range("2026-10-21T15:00:00Z", periods=n, freq="5min")
    return pd.DataFrame({
        "ts": ts,
        "open": [float(r[0]) for r in rows],
        "high": [float(r[1]) for r in rows],
        "low": [float(r[2]) for r in rows],
        "close": [float(r[3]) for r in rows],
        "volume": [100.0] * n,
    })


_ALWAYS_LONG = (
    "import numpy as np\nimport pandas as pd\n"
    "def evaluate(bars, params):\n"
    "    n = len(bars)\n"
    "    if n < 4:\n"
    "        return None\n"
    "    e = float(bars['close'].iloc[-1])\n"
    "    return {'ts': bars['ts'].values[-1], 'direction': 'long',\n"
    "            'entry': e, 'stop': e - 1.0, 'target': e + 1.0}\n"
)


def test_production_fill_does_not_grant_the_derived_entry_price():
    """The strategy asks for 100; the market opens at 105 -> we pay 105.

    Under the legacy retro-fill model the trade was booked at 100 and the
    target/stop were measured from 100 -- a price the market never offered.
    """
    rows = [(100.0, 100.5, 99.5, 100.0)] * 3
    # bar 3 (index 3) is the decision bar; bar 4 opens at 105 (a gap up)
    rows.append((100.0, 100.5, 99.5, 100.0))     # index 3 - decision
    rows.append((105.0, 106.0, 104.5, 105.5))    # index 4 - fill bar, gaps up
    rows.append((105.5, 106.5, 105.0, 106.0))
    bars = _bars(rows)

    res = run_custom_backtest_on_bars(code=_ALWAYS_LONG, bars=bars,
                                      params={}, symbol="MGC", qty=1,
                                      timeout_s=30, fill_mode="production")

    assert res.get("error") is None
    assert res["fill_mode"] == "production"
    assert res["fill_model"] == "production_market_next_bar_open"
    assert res["n_signals"] >= 1
    # We filled at 105, the strategy wanted 100 -> 50 ticks of slippage on MGC
    # (tick 0.10). It must be VISIBLE, not silently zero.
    assert res["entry_slip_ticks_mean"] is not None
    assert res["entry_slip_ticks_mean"] > 0, \
        "a gap-up fill must register as worse-than-wanted slippage"


def test_production_fill_stop_gap_books_a_loss_not_a_phantom_profit():
    """A long whose stop sits ABOVE the fill open is stopped out at the open.

    The legacy model booked the loss as ``-abs(entry - stop)`` from the
    DERIVED entry, which can print a profit on a stop.
    """
    code = (
        "import numpy as np\nimport pandas as pd\n"
        "def evaluate(bars, params):\n"
        "    n = len(bars)\n"
        "    if n < 4:\n"
        "        return None\n"
        "    e = float(bars['close'].iloc[-1])\n"
        "    # stop ABOVE the entry -> only a gap-down fill can stop us out\n"
        "    return {'ts': bars['ts'].values[-1], 'direction': 'long',\n"
        "            'entry': e, 'stop': e + 5.0, 'target': e + 50.0}\n"
    )
    rows = [(100.0, 100.5, 99.5, 100.0)] * 4
    rows.append((95.0, 95.5, 94.0, 95.0))   # index 4 - fill bar gaps DOWN
    rows.append((95.0, 96.0, 94.0, 95.5))
    bars = _bars(rows)

    res = run_custom_backtest_on_bars(code=code, bars=bars, params={},
                                      symbol="MGC", qty=1, timeout_s=30,
                                      fill_mode="production")

    assert res.get("error") is None
    assert res["n_trades"] >= 1
    # Filled at 95, stopped at 95 -> gross 0, net negative after costs.
    assert res["total_pnl"] < 0, \
        "a gap through the stop must book a loss, never a profit"


def test_production_fill_stop_hits_use_the_bar_low_not_the_stop_level():
    """A stop hit inside the bar fills at the stop level (long)."""
    code = (
        "import numpy as np\nimport pandas as pd\n"
        "def evaluate(bars, params):\n"
        "    n = len(bars)\n"
        "    if n < 4:\n"
        "        return None\n"
        "    e = float(bars['close'].iloc[-1])\n"
        "    return {'ts': bars['ts'].values[-1], 'direction': 'long',\n"
        "            'entry': e, 'stop': e - 2.0, 'target': e + 20.0}\n"
    )
    rows = [(100.0, 100.5, 99.5, 100.0)] * 4
    rows.append((100.0, 100.5, 97.0, 98.0))   # index 4: fill 100, low 97 hits stop 98
    rows.append((98.0, 99.0, 97.5, 98.5))
    bars = _bars(rows)

    res = run_custom_backtest_on_bars(code=code, bars=bars, params={},
                                      symbol="MGC", qty=1, timeout_s=30,
                                      fill_mode="production")

    assert res.get("error") is None
    assert res["n_trades"] >= 1
    # MGC: tick 0.10, tick_value $1.00 -> $10 per point. Filled at 100, stop
    # at 98 -> -2.00 points = -$20.00 gross, minus the round-trip cost.
    from src.tools.cost_model import round_trip_cost
    expected = -(2.0 * 10.0) - round_trip_cost("MGC", "stop")
    assert res["total_pnl"] == pytest.approx(expected), \
        (f"expected the stop to fill AT THE STOP LEVEL (98), i.e. {expected:.2f}; "
         f"got {res['total_pnl']:.2f} -- a fill at the bar low (97) would be "
         f"{-(3.0 * 10.0) - round_trip_cost('MGC', 'stop'):.2f}")


def test_adverse_first_books_the_stop_when_a_bar_spans_both():
    """One bar spans stop AND target -> the STOP wins."""
    code = (
        "import numpy as np\nimport pandas as pd\n"
        "def evaluate(bars, params):\n"
        "    n = len(bars)\n"
        "    if n < 4:\n"
        "        return None\n"
        "    e = float(bars['close'].iloc[-1])\n"
        "    return {'ts': bars['ts'].values[-1], 'direction': 'long',\n"
        "            'entry': e, 'stop': e - 1.0, 'target': e + 1.0}\n"
    )
    rows = [(100.0, 100.5, 99.5, 100.0)] * 4
    rows.append((100.0, 102.0, 98.0, 100.0))   # spans both stop 99 and target 101
    rows.append((100.0, 100.5, 99.5, 100.0))
    bars = _bars(rows)

    res = run_custom_backtest_on_bars(code=code, bars=bars, params={},
                                      symbol="MGC", qty=1, timeout_s=30,
                                      fill_mode="production")

    assert res.get("error") is None
    assert res["n_trades"] >= 1
    assert res["total_pnl"] < 0, "adverse-first must book the stop, not the target"


def test_legacy_mode_is_unchanged_and_stamped_as_non_promotion_grade():
    """legacy keeps the historical retro-fill numbers and is flagged."""
    rows = [(100.0, 100.5, 99.5, 100.0)] * 6
    bars = _bars(rows)

    res = run_custom_backtest_on_bars(code=_ALWAYS_LONG, bars=bars, params={},
                                      symbol="MGC", qty=1, timeout_s=30,
                                      fill_mode="legacy")

    assert res.get("error") is None
    assert res["fill_mode"] == "legacy"
    assert res["fill_model"] == "legacy_retro_derived_entry"
    assert res["promotion_grade"] is False, \
        "the retro-fill path must never read as promotion-grade evidence"


def test_production_mode_is_promotion_grade_when_no_stale_reference():
    rows = [(100.0, 100.5, 99.5, 100.0)] * 6
    bars = _bars(rows)

    res = run_custom_backtest_on_bars(code=_ALWAYS_LONG, bars=bars, params={},
                                      symbol="MGC", qty=1, timeout_s=30,
                                      fill_mode="production")

    assert res.get("error") is None
    assert res["promotion_grade"] is True
    assert res["backtest_fill_valid"] is True
    assert res["cost_model"] == "v1_realistic_micros"


def test_unknown_fill_mode_is_rejected_not_silently_defaulted():
    rows = [(100.0, 100.5, 99.5, 100.0)] * 6
    bars = _bars(rows)

    res = run_custom_backtest_on_bars(code=_ALWAYS_LONG, bars=bars, params={},
                                      symbol="MGC", qty=1, timeout_s=30,
                                      fill_mode="bogus")

    assert res.get("error"), "an unknown fill mode must error, not default"
    assert "fill_mode" in res["error"]


def test_production_close_mode_fills_at_the_decision_bar_close():
    """The optimistic bound: fill at the close of the decision bar."""
    rows = [(100.0, 100.5, 99.5, 100.0)] * 4
    rows.append((100.0, 100.5, 99.5, 101.5))   # decision bar closes at 101.5
    rows.append((105.0, 106.0, 104.5, 105.5))   # next bar opens at 105
    rows.append((105.5, 106.5, 105.0, 106.0))
    bars = _bars(rows)

    res = run_custom_backtest_on_bars(code=_ALWAYS_LONG, bars=bars, params={},
                                      symbol="MGC", qty=1, timeout_s=30,
                                      fill_mode="production_close")

    assert res.get("error") is None
    assert res["fill_model"] == "production_market_decision_close"
    # wanted 101.5, filled at 101.5 -> zero slippage (unlike production, which
    # would have filled at the 105 open).
    assert res["entry_slip_ticks_abs_mean"] == pytest.approx(0.0, abs=1e-6)
