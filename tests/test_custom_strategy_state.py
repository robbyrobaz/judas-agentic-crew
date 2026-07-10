"""Tests for the custom-strategy module-state persistence fix.

Background: autofix #396 / finding 7f8ba150 — the live ``custom_strategy``
dispatch path re-creates the agent's namespace on every ``evaluate()`` call,
so module-level state variables (``_STATE = []`` / ``_events = ...`` / etc.)
defined by the agent code reset between scans. Strategies that build up a
cross-bar setup (e.g. ``if len(_history) >= 3 and ...: emit_signal``) never
complete their setup and never fire. This file guards the fix.

The fix is a module-level ``_STATE_CACHE`` keyed by ``(csid, code)`` in
``src/research/custom_strategy_runtime.py``: same csid + same code -> reuse
the same namespace dict, so module-level globals survive across ``evaluate()``
calls. Different csids and different code strings still get isolated
namespaces (no cross-strategy / cross-version state leakage).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.research import custom_strategy_runtime as csr  # noqa: E402


# -------------------------------------------------------------------- helpers

def _synth_bars(n: int = 80, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base = 100 + np.cumsum(rng.normal(0, 0.5, n))
    return pd.DataFrame(
        {
            "ts": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
            "open": base,
            "high": base + 0.5,
            "low": base - 0.5,
            "close": base,
            "volume": 1000,
        }
    )


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch):
    """Each test sees a fresh ``_STATE_CACHE`` so cross-test state never leaks."""
    monkeypatch.setattr(csr, "_STATE_CACHE", {}, raising=False)
    yield


# ---------------------------------------------------------------------- tests

def test_module_state_persists_across_evaluate_calls():
    """A counter defined at module level must accumulate across calls for the
    same csid + code. Without persistence, the setup never completes and the
    strategy never fires (this is the dormant-cluster root cause for 12 of 12
    MBT / MGC / MNQ / MCL customs on 2026-07-10)."""
    code = (
        "_setup_count = 0\n"
        "def evaluate(bars, params):\n"
        "    global _setup_count\n"
        "    _setup_count += 1\n"
        "    if _setup_count < 3:\n"
        "        return None\n"
        "    last = float(bars['close'].iloc[-1])\n"
        "    return {'direction': 'long', 'entry': last,\n"
        "            'stop': last - 1.0, 'target': last + 2.0}\n"
    )

    csid = 99001  # well outside any real custom_strategy_id range
    bars_a = _synth_bars(60)
    bars_b = _synth_bars(60)
    bars_c = _synth_bars(60)

    # First two calls: cross-bar setup < 3, must NOT fire.
    out_a = csr.evaluate_custom_strategy(
        code=code, bars=bars_a, params={"__csid__": csid}
    )
    out_b = csr.evaluate_custom_strategy(
        code=code, bars=bars_b, params={"__csid__": csid}
    )
    assert out_a is None, f"call 1 should not fire (counter=1), got {out_a!r}"
    assert out_b is None, f"call 2 should not fire (counter=2), got {out_b!r}"

    # Third call: counter at 3 -> signal fires. WITHOUT the fix, the
    # module-level _setup_count would be back at 0 in a fresh namespace
    # and this would still be None.
    out_c = csr.evaluate_custom_strategy(
        code=code, bars=bars_c, params={"__csid__": csid}
    )
    assert out_c is not None, (
        "call 3 should fire (counter=3) but module-level state was reset "
        "between calls — _STATE_CACHE missing or keyed wrong."
    )
    assert out_c["direction"] == "long"

    # Confirm the cached ns survived: a 4th call sees counter=4 (so 4 emits).
    out_d = csr.evaluate_custom_strategy(
        code=code, bars=_synth_bars(60), params={"__csid__": csid}
    )
    assert out_d is not None and out_d["direction"] == "long"


def test_state_cache_isolated_by_csid():
    """Two different csids must get independent caches. A counter on csid=A
    must not contribute to csid=B (otherwise we leak cross-strategy state
    and silently double-fire the wrong strategy on the wrong bars)."""
    code_a = (
        "_counter = 0\n"
        "def evaluate(bars, params):\n"
        "    global _counter\n"
        "    _counter += 1\n"
        "    if _counter < 2:\n"
        "        return None\n"
        "    last = float(bars['close'].iloc[-1])\n"
        "    return {'direction': 'long', 'entry': last,\n"
        "            'stop': last - 1.0, 'target': last + 2.0}\n"
    )
    # Same source, identical code text — but different csids means independent
    # caches. A bug keyed only on ``code`` would conflate them.
    csid_a = 99010
    csid_b = 99011

    bars = _synth_bars(60)

    out_a1 = csr.evaluate_custom_strategy(code=code_a, bars=bars, params={"__csid__": csid_a})
    out_b1 = csr.evaluate_custom_strategy(code=code_a, bars=bars, params={"__csid__": csid_b})

    assert out_a1 is None, "csid A call 1 must not fire (counter=1)"
    assert out_b1 is None, (
        "csid B call 1 must NOT inherit csid A's counter — csid isolation broken"
    )

    # Both csids should now fire on their 2nd call.
    out_a2 = csr.evaluate_custom_strategy(code=code_a, bars=bars, params={"__csid__": csid_a})
    out_b2 = csr.evaluate_custom_strategy(code=code_a, bars=bars, params={"__csid__": csid_b})
    assert out_a2 is not None and out_a2["direction"] == "long"
    assert out_b2 is not None and out_b2["direction"] == "long"


def test_state_cache_invalidated_when_code_changes():
    """Strategy iteration: a new version's code must invalidate the cache so
    the previous version's accumulated state doesn't bleed into the new one.
    This is the safety rail that keeps code changes from leaking state across
    promotions."""
    code_v1 = (
        "_counter = 0\n"
        "def evaluate(bars, params):\n"
        "    global _counter\n"
        "    _counter += 1\n"
        "    if _counter < 5:\n"
        "        return None\n"
        "    last = float(bars['close'].iloc[-1])\n"
        "    return {'direction': 'long', 'entry': last,\n"
        "            'stop': last - 1.0, 'target': last + 2.0}\n"
    )
    # Tiny tweak: v2 uses a shorter horizon (counter < 1). The counter must
    # reset because the SOURCE changed, even if the shape is identical.
    code_v2 = code_v1.replace("if _counter < 5:", "if _counter < 1:")

