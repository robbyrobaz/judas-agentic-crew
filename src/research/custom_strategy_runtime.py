"""Sandboxed execution of agent-authored strategy code.

Phase 9 — Option A "full peer trust" path. The PM agent writes a Python
function ``evaluate(bars, params) -> dict | None`` and we run it on real
historical bars. Hard safety:

  - Restricted ``__builtins__`` (no ``__import__``, ``open``, ``eval``,
    ``exec``, ``getattr``, ``setattr``, ``compile``, ``input`` ...).
  - Only ``np``, ``pd``, ``datetime`` modules pre-injected.
  - 5s wall-clock kill via ``signal.alarm`` on POSIX main thread; thread
    fallback elsewhere.
  - Errors caught and returned as structured failures.

NOTE on remaining sandbox surface (operator is aware): ``pd.read_csv`` /
``pd.DataFrame.to_csv`` / ``np.load`` etc. CAN still touch the
filesystem. The brief explicitly accepts this for M3 peer trust; the
hard rails (paper-only mode, sleeve cap, kill flag) remain in place.
"""
from __future__ import annotations

import json
import logging
import signal
import sqlite3
import threading
import time
from typing import Any

log = logging.getLogger(__name__)


ALLOWED_BUILTINS = {
    "True": True,
    "False": False,
    "None": None,
    "abs": abs,
    "all": all,
    "any": any,
    "len": len,
    "max": max,
    "min": min,
    "sum": sum,
    "round": round,
    "range": range,
    "enumerate": enumerate,
    "zip": zip,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "frozenset": frozenset,
    "float": float,
    "int": int,
    "str": str,
    "bool": bool,
    "isinstance": isinstance,
    "sorted": sorted,
    "reversed": reversed,
    "map": map,
    "filter": filter,
    "next": next,
    "iter": iter,
    "print": (lambda *a, **kw: None),  # silenced
    # Required for `class Foo:` declarations; harmless on its own.
    "__build_class__": __build_class__,
    "__name__": "<custom_strategy>",
    # NOTE: __import__ is NOT in the bare dict — agent code can't issue raw
    # imports. Instead, _safe_import is injected at runtime via the
    # _build_namespace() helper so it can close over the live `sys` module.
}


_DEFAULT_TIMEOUT_S = 5


def _build_namespace() -> dict[str, Any]:
    """Build a fresh restricted namespace for evaluating agent code.

    Only ``pandas``, ``numpy``, ``datetime`` modules and the ``ALLOWED_BUILTINS``
    subset are exposed. Notably absent from builtins: ``os``, ``sys``,
    ``subprocess``, ``socket``, ``open``, ``eval``, ``exec``.

    ``__import__`` IS injected (as ``_safe_import``) so that numpy/pandas
    internals can lazy-import their own already-loaded submodules
    (e.g. ``ndarray.max`` -> ``numpy._methods``). Without this, any code
    that uses ``arr.max()`` / ``arr.min()`` style method calls dies with
    ``KeyError: '__import__'``. The guard only returns modules already in
    ``sys.modules`` so agent code can't pull in fresh modules.
    """
    import numpy as np
    import pandas as pd
    import datetime as _dt

    bi = dict(ALLOWED_BUILTINS)
    bi["__import__"] = _safe_import
    return {
        "__builtins__": bi,
        "np": np,
        "pd": pd,
        "datetime": _dt,
    }


class _StrategyTimeout(Exception):
    """Raised when the agent code exceeds its wall-clock budget."""


def _run_with_timeout(callable_fn, timeout_s: int) -> Any:
    """Run ``callable_fn()`` with a wall-clock timeout.

    Uses ``signal.alarm`` on POSIX main thread (sub-second precision not
    required); falls back to thread-with-join elsewhere. Both raise
    ``_StrategyTimeout`` on overrun.
    """
    is_main = threading.current_thread() is threading.main_thread()
    use_signal = is_main and hasattr(signal, "SIGALRM")

    if use_signal:
        def _handler(signum, frame):  # noqa: ARG001
            raise _StrategyTimeout(f"timed out after {timeout_s}s")

        old_handler = signal.signal(signal.SIGALRM, _handler)
        try:
            signal.alarm(int(max(1, timeout_s)))
            try:
                return callable_fn()
            finally:
                signal.alarm(0)
        finally:
            signal.signal(signal.SIGALRM, old_handler)

    # Thread fallback: best-effort. We can't kill a runaway thread cleanly
    # in CPython, but we can stop waiting and surface the timeout.
    holder: dict[str, Any] = {}

    def _runner() -> None:
        try:
            holder["result"] = callable_fn()
        except BaseException as exc:  # noqa: BLE001 — propagate to caller
            holder["error"] = exc

    th = threading.Thread(target=_runner, daemon=True)
    th.start()
    th.join(timeout_s)
    if th.is_alive():
        raise _StrategyTimeout(f"timed out after {timeout_s}s (thread fallback)")
    if "error" in holder:
        raise holder["error"]  # type: ignore[misc]
    return holder.get("result")


def _compile_and_load(code: str) -> tuple[Any, dict] | tuple[None, dict]:
    """Compile ``code`` and exec into a fresh namespace.

    Returns ``(evaluate_fn, ns)`` on success, ``(None, {"error": ...})`` on
    failure to compile / find ``evaluate``.
    """
    try:
        compiled = compile(code, "<custom_strategy>", "exec")
    except SyntaxError as exc:
        return None, {"error": f"SyntaxError: {exc}"}
    ns = _build_namespace()
    try:
        exec(compiled, ns)  # noqa: S102 — sandboxed namespace
    except _StrategyTimeout as exc:
        return None, {"error": f"timeout during module load: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return None, {"error": f"{type(exc).__name__} during load: {exc}"}
    fn = ns.get("evaluate")
    if not callable(fn):
        return None, {"error": "code must define a top-level callable named 'evaluate'"}
    return fn, ns


def evaluate_custom_strategy(
    *,
    code: str,
    bars,
    params: dict | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> dict | None:
    """Compile + execute the agent's code in a restricted namespace.

    The code MUST define ``def evaluate(bars, params): -> dict | None``.
    Return value is the signal dict (e.g.
    ``{"direction": "long", "entry": ..., "stop": ..., "target": ...}``)
    or ``None``. Any exception, including timeout, is caught and logged;
    we return ``None`` so the runtime treats it as a no-op.
    """
    fn, meta = _compile_and_load(code)
    if fn is None:
        log.warning("custom_strategy.load_failed: %s", meta.get("error"))
        return None

    p = dict(params or {})

    def _call() -> Any:
        return fn(bars, p)

    try:
        result = _run_with_timeout(_call, timeout_s)
    except _StrategyTimeout as exc:
        log.warning("custom_strategy.timeout: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("custom_strategy.eval_failed csid=%s: %s: %s", params.get("__csid__", "?") if isinstance(params, dict) else "?", type(exc).__name__, exc)
        return None
    if result is None:
        return None
    if not isinstance(result, dict):
        log.warning("custom_strategy.bad_return_type: %s", type(result).__name__)
        return None
    return result


# Modules agent code must NEVER be able to import, even via __import__.
# (numpy/pandas internals are NOT in this set — see _safe_import.)
_BLOCKED_IMPORTS = frozenset({
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "pathspec",
    "importlib", "ctypes", "cffi", "multiprocessing", "threading",
    "http", "urllib", "urllib3", "requests", "ftplib", "smtplib",
    "ssl", "asyncio", "select", "fcntl", "pwd", "grp", "resource",
    "tempfile", "glob", "fnmatch", "fileinput", "zipfile", "tarfile",
    "pickle", "marshal", "shelve", "dbm", "sqlite3",
    "builtins", "_io", "io",
    "antigravity", "this", "__future__",
    # Anything starting with "src." or our own packages — never let an agent
    # reach into the runtime internals.
    # (We don't list each submodule here; the prefix check in _safe_import
    # catches them.)
})


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """Restricted ``__import__`` for the agent-strategy sandbox.

    Strategy code in the sandbox never directly issues ``import`` statements,
    but **numpy/pandas internals** do: e.g. ``ndarray.max()`` lazily imports
    ``numpy._methods``. With the bare ``ALLOWED_BUILTINS`` that lookup hits
    ``KeyError: '__import__'`` and the strategy silently dies. We:

      1. Always reject top-level imports from ``_BLOCKED_IMPORTS`` and any
         submodule of an internal package (``src.*``, ``crewai.*``).
      2. Allow ``__import__`` for modules already loaded in ``sys.modules``
         so numpy/pandas's own lazy imports succeed.
      3. Reject new (not-yet-loaded) module imports outright — agent code
         can't pull in fresh modules via this path.

    Net effect: an agent that writes ``__import__('os')`` still gets
    ``ImportError``; an agent that writes ``arr.max()`` works because numpy
    can lazily import its own already-registered submodules.
    """
    import sys as _sys

    # 1. Block the dangerous top-levels entirely.
    top = name.split(".", 1)[0]
    if top in _BLOCKED_IMPORTS or top.startswith("src") or top.startswith("crewai"):
        raise ImportError(f"import of {name!r} is not permitted in the strategy sandbox")
    # 2. Only return modules already loaded.
    if name in _sys.modules:
        return _sys.modules[name]
    # 3. Fresh modules are forbidden.
    raise ImportError(f"import of {name!r} is not permitted in the strategy sandbox")


_BARS_PER_DAY = {"5m": 288, "15m": 96, "1h": 24}


def _fetch_synthetic_or_live_bars(symbol: str, days: int, timeframe: str = "1h"):
    """Return a DataFrame of OHLCV bars for ``symbol`` at ``timeframe``.

    Lazy-imports ``portfolio_runtime.fetch_bars`` to avoid forcing
    ib_async at import time (tests pass synthetic bars directly via
    ``run_custom_backtest_on_bars``).
    """
    from src.config import load_config
    from src import bar_cache

    tf = bar_cache.normalize_tf(timeframe)
    cfg = load_config()
    ibkr = cfg.ibkr
    # Use a DEDICATED clientId (not the live scanner's data_client_id) so a
    # researcher backtest doesn't collide with the every-5-min scan on the same
    # IBKR connection. bar_cache's connect-retry covers the rare same-id race.
    research_client_id = int(ibkr.data_client_id) + 9
    # Use bar_cache (tf-aware history duration + caching) rather than the fixed
    # 60-day fetch_bars path — 60 days of 5m exceeds IBKR's limit and times out.
    try:
        df = bar_cache.get_bars(
            symbol.upper(), host=ibkr.host, port=ibkr.port,
            client_id=research_client_id, timeframe=tf,
        )
    except Exception:  # noqa: BLE001
        return None
    if df is None or len(df) == 0:
        return None
    # Trim to ``days`` using the timeframe's bars-per-day (the old code assumed
    # 24/day = hourly, which would keep only ~2 days of 5m data).
    per_day = _BARS_PER_DAY.get(tf, 24)
    if days and len(df) > per_day * days:
        df = df.tail(per_day * days).reset_index(drop=True)
    return df


def run_custom_backtest_on_bars(
    *,
    code: str,
    bars,
    params: dict | None = None,
    symbol: str = "MGC",
    qty: int = 1,
    timeout_s: int = 60,
) -> dict:
    """Replay ``bars`` through ``evaluate(bars, params)`` and aggregate.

    The convention: at each bar ``i``, we call ``evaluate(bars[:i+1], params)``.
    If a non-None signal dict is returned with direction/entry/stop/target,
    we simulate a one-shot trade — first subsequent bar that hits stop or
    target settles the trade. No re-entries until the trade closes.
    """
    started = time.time()
    try:
        import pandas as pd
    except Exception as exc:  # noqa: BLE001
        return {"error": f"pandas import failed: {exc}", "n_signals": 0,
                "n_wins": 0, "n_losses": 0, "total_pnl": 0.0,
                "pf": 0.0, "expectancy_r": 0.0, "max_drawdown": 0.0}

    if bars is None or len(bars) < 5:
        return {"error": "insufficient bars", "n_signals": 0, "n_wins": 0,
                "n_losses": 0, "total_pnl": 0.0, "pf": 0.0,
                "expectancy_r": 0.0, "max_drawdown": 0.0}

    fn, meta = _compile_and_load(code)
    if fn is None:
        return {"error": meta.get("error"), "n_signals": 0, "n_wins": 0,
                "n_losses": 0, "total_pnl": 0.0, "pf": 0.0,
                "expectancy_r": 0.0, "max_drawdown": 0.0}

    p = dict(params or {})
    sym = str(symbol).upper()
    trade_qty = max(1, int(p.get("qty", qty) or 1))
    from src.portfolio_runtime import _CONTRACT_SPECS
    from src.tools.cost_model import COST_MODEL_VERSION, apply_costs_to_trade

    spec = _CONTRACT_SPECS.get(sym)
    if not spec or not spec.get("tick") or not spec.get("tick_value"):
        return {"error": f"unknown contract economics for {sym}", "n_signals": 0,
                "n_wins": 0, "n_losses": 0, "total_pnl": 0.0,
                "pf": 0.0, "expectancy_r": 0.0, "max_drawdown": 0.0}
    dollars_per_point = float(spec["tick_value"]) / float(spec["tick"])

    n_signals = 0
    wins = 0
    losses = 0
    pnls: list[float] = []
    rs: list[float] = []
    open_trade: dict | None = None
    error: str | None = None

    try:
        def _run_loop():
            nonlocal n_signals, wins, losses, open_trade
            n = len(bars)
            # cap iterations to bound runtime
            for i in range(2, n):
                if time.time() - started > timeout_s:
                    raise _StrategyTimeout(f"backtest exceeded {timeout_s}s")
                slice_df = bars.iloc[: i + 1]
                # Resolve open trade first
                if open_trade is not None:
                    bar = bars.iloc[i]
                    hit_stop = (
                        (open_trade["direction"] == "long" and float(bar["low"]) <= open_trade["stop"]) or
                        (open_trade["direction"] == "short" and float(bar["high"]) >= open_trade["stop"])
                    )
                    hit_target = (
                        (open_trade["direction"] == "long" and float(bar["high"]) >= open_trade["target"]) or
                        (open_trade["direction"] == "short" and float(bar["low"]) <= open_trade["target"])
                    )
                    if hit_stop or hit_target:
                        entry = open_trade["entry"]
                        risk = abs(entry - open_trade["stop"]) or 1.0
                        target_won = hit_target and not hit_stop
                        points = (abs(open_trade["target"] - entry) if target_won
                                  else -abs(entry - open_trade["stop"]))
                        gross_pnl = points * dollars_per_point * trade_qty
                        pnl = apply_costs_to_trade(
                            symbol=sym,
                            gross_pnl=gross_pnl,
                            exit_reason="target" if target_won else "stop",
                            qty=trade_qty,
                        )
                        if pnl > 0:
                            wins += 1
                        else:
                            losses += 1
                        pnls.append(float(pnl))
                        risk_dollars = risk * dollars_per_point * trade_qty
                        rs.append(float(pnl / risk_dollars) if risk_dollars else 0.0)
                        open_trade = None
                        continue

                if open_trade is not None:
                    continue

                try:
                    sig = fn(slice_df, p)
                except Exception:
                    sig = None
                if sig is None or not isinstance(sig, dict):
                    continue
                direction = str(sig.get("direction", "")).lower()
                if direction not in ("long", "short"):
                    continue
                try:
                    entry = float(sig["entry"])
                    stop = float(sig["stop"])
                    target = float(sig["target"])
                except (KeyError, TypeError, ValueError):
                    continue
                n_signals += 1
                open_trade = {
                    "direction": direction,
                    "entry": entry,
                    "stop": stop,
                    "target": target,
                }

        _run_with_timeout(_run_loop, timeout_s)
    except _StrategyTimeout as exc:
        error = f"timeout: {exc}"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    total_pnl = float(sum(pnls))
    gross_win = sum(p_ for p_ in pnls if p_ > 0)
    gross_loss = -sum(p_ for p_ in pnls if p_ < 0)
    pf = float(gross_win / gross_loss) if gross_loss > 0 else (0.0 if gross_win == 0 else float("inf"))
    expectancy_r = float(sum(rs) / len(rs)) if rs else 0.0

    # Max drawdown on cumulative equity curve.
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in pnls:
        running += x
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    return {
        "n_signals": int(n_signals),
        "n_wins": int(wins),
        "n_losses": int(losses),
        "total_pnl": total_pnl,
        "pf": pf if pf != float("inf") else 999.0,
        "profit_factor": pf if pf != float("inf") else 999.0,
        "expectancy_r": expectancy_r,
        "max_drawdown": float(max_dd),
        "n_trades": len(pnls),
        "symbol": sym,
        "qty": trade_qty,
        "dollars_per_point": dollars_per_point,
        "cost_model": COST_MODEL_VERSION,
        "error": error,
    }


def run_custom_backtest(
    *,
    code: str,
    symbol: str,
    days: int = 90,
    db_path: str | None = None,
    bars=None,
    timeout_s: int = 60,
    timeframe: str = "1h",
    params: dict | None = None,
    qty: int = 1,
) -> dict:
    """High-level entry point used by the PM agent tool.

    If ``bars`` is supplied (tests), uses it directly. Otherwise fetches
    native bars at ``timeframe`` (5m/15m/1h) — the detectors are
    timeframe-agnostic, so this lets the researcher backtest faster
    timeframes natively instead of resampling 1h.
    """
    if bars is None:
        bars = _fetch_synthetic_or_live_bars(symbol, days, timeframe)
    if bars is None or len(bars) < 5:
        return {
            "n_signals": 0, "n_wins": 0, "n_losses": 0, "total_pnl": 0.0,
            "pf": 0.0, "expectancy_r": 0.0, "max_drawdown": 0.0,
            "error": "no bars available",
        }
    return run_custom_backtest_on_bars(
        code=code, bars=bars, params=params, symbol=symbol, qty=qty,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------
# DB helpers consumed by portfolio_runtime + pm_agent tools
# ---------------------------------------------------------------------------


def load_custom_strategy(custom_id: int, *, db_path: str) -> tuple[str, dict] | None:
    """Return ``(code, params)`` for an active custom strategy, or None."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT code, backtest_metrics_json FROM custom_strategies "
            "WHERE id = ? AND active = 1 AND retired_at_utc IS NULL",
            (int(custom_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        metrics = json.loads(row["backtest_metrics_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        metrics = {}
    # Custom params are not stored separately in v1; pass empty.
    return str(row["code"]), {"backtest_metrics": metrics}
