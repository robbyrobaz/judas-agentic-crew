"""Regression: asyncio loop handling.

The previous code used ``asyncio.get_event_loop().run_until_complete(...)``
in synchronous wrappers. On Python 3.12 that emits a DeprecationWarning
when no loop is set in the main thread, and on 3.14+ raises
``RuntimeError: no current event loop``. Replace with ``asyncio.run`` and
ensure no event-loop-related warning is raised during the call.

Also pins removal of ``util.startLoop()`` from inside async coroutines —
that helper is for IPython only and patches the running loop incorrectly.
"""
from __future__ import annotations

import asyncio
import warnings

import pytest


def test_fetch_bars_uses_asyncio_run(monkeypatch):
    """fetch_bars wraps the async helper without get_event_loop()."""
    from src import portfolio_runtime

    captured: dict = {}

    async def fake_fetch(symbols, host, port, client_id, bar_size="1 hour"):
        captured["symbols"] = sorted(symbols)
        return {s: f"bars-{s}" for s in symbols}

    monkeypatch.setattr(portfolio_runtime, "_fetch_bars_async", fake_fetch)

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        out = portfolio_runtime.fetch_bars({"MGC"}, host="x", port=4002, client_id=150)

    assert captured["symbols"] == ["MGC"]
    assert out == {"MGC": "bars-MGC"}


def test_place_bracket_uses_asyncio_run(monkeypatch):
    from src import portfolio_runtime

    async def fake_place(**kwargs):
        return {"parent_order_id": 1, "tp_order_id": 2, "sl_order_id": 3,
                "local_symbol": "X", "status": "Submitted"}

    monkeypatch.setattr(portfolio_runtime, "_place_bracket_async", fake_place)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        out = portfolio_runtime.place_bracket(
            symbol="MGC", side="BUY", quantity=1,
            stop_price=1.0, target_price=2.0,
            host="x", port=4002, client_id=151,
        )
    assert out["parent_order_id"] == 1


def test_no_util_startloop_in_fetch_bars_async_source():
    """Static guard: util.startLoop() must not be called inside the async
    coroutine. It belongs in IPython kernels, not asyncio coroutines."""
    import inspect

    from src import portfolio_runtime

    src = inspect.getsource(portfolio_runtime._fetch_bars_async)
    assert "startLoop" not in src, "util.startLoop() reintroduced in async coroutine"


def test_get_event_loop_removed_from_runtime_modules():
    """Static guard across the three audit-flagged files."""
    import importlib
    import inspect

    targets = [
        "src.portfolio_runtime",
        "src.tools.ibkr_data",
        "src.tools.ibkr_executor",
    ]
    for mod_name in targets:
        mod = importlib.import_module(mod_name)
        src = inspect.getsource(mod)
        assert "get_event_loop()" not in src, (
            f"{mod_name} still calls asyncio.get_event_loop()"
        )
