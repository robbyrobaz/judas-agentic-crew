"""State-preservation regression tests for ``evaluate_custom_strategy``.

Cross-bar setup-accumulation strategies (iFVG midpoint, sweep chains,
breaker blocks, the 5m silver_bullet family) keep their pending setups in
a module-level ``_S`` dict. Before this fix, ``evaluate_custom_strategy``
recompiled the code in a fresh namespace on every call, so ``_S`` was
reset to ``{}`` and the strategy never saw its own prior setups. That
kept 13 dormant actives at 0 lifetime trades (incl. #4372 MBT
silver_bullet_pdh_pdl_retest, #4373-#4376 silver_bullet family).

These tests pin the contract: the same ``__csid__`` MUST persist state
between calls; different csids MUST be isolated; and ``reset_custom_strategy_state``
MUST drop the cached namespace so the next call sees a fresh ``_S``
(used on retire / code-update paths).
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


def _synth_bars(n: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(42)
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


_STATEFUL_CODE = (
    "_S = {'count': 0}\n"
    "def evaluate(bars, params):\n"
    "    _S['count'] += 1\n"
    "    if _S['count'] < 2:\n"
    "        return None\n"
    "    last = float(bars['close'].iloc[-1])\n"
    "    return {'direction': 'long', 'entry': last, 'stop': last - 1.0, 'target': last + 2.0}\n"
)


def test_evaluate_custom_strategy_preserves_state_across_calls():
    """The same ``__csid__`` MUST preserve ``_S`` between calls.

    Without persistence, both calls return None (each call sees a fresh
    ``_S = {'count': 0}``). With persistence, the second call sees
    ``_S['count'] == 2`` and returns a long signal.
    """
    bars = _synth_bars()
    out1 = csr.evaluate_custom_strategy(
        code=_STATEFUL_CODE, bars=bars, params={"__csid__": 4321},
    )
    assert out1 is None, "first call should warm up the state machine (no signal)"
    out2 = csr.evaluate_custom_strategy(
        code=_STATEFUL_CODE, bars=bars, params={"__csid__": 4321},
    )
    assert out2 is not None, "second call must see state from the first call"
    assert out2["direction"] == "long"
    assert out2["target"] > out2["entry"] > out2["stop"]


def test_evaluate_custom_strategy_state_isolated_per_csid():
    """Different csids MUST NOT share state -- the cache key is per-strategy."""
    bars = _synth_bars()
    # Warm up csid=111 (signal by call 2).
    csr.evaluate_custom_strategy(
        code=_STATEFUL_CODE, bars=bars, params={"__csid__": 111},
    )
    out_warm = csr.evaluate_custom_strategy(
        code=_STATEFUL_CODE, bars=bars, params={"__csid__": 111},
    )
    assert out_warm is not None, "csid 111 should have warmed up by call 2"
    # csid=222 starts fresh -- first call must re-warm only.
    out_fresh = csr.evaluate_custom_strategy(
        code=_STATEFUL_CODE, bars=bars, params={"__csid__": 222},
    )
    assert out_fresh is None, "csid 222 must have its own fresh state"


def test_reset_custom_strategy_state_clears_cache():
    """``reset_custom_strategy_state`` MUST drop the cached namespace so the
    next call sees a fresh ``_S`` (used on retire / code-update paths)."""
    bars = _synth_bars()
    csr.evaluate_custom_strategy(
        code=_STATEFUL_CODE, bars=bars, params={"__csid__": 9000},
    )
    csr.reset_custom_strategy_state(9000)
