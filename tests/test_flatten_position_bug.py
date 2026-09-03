"""Regression test for the flatten_position NameError bug.

Finding 922ef65e: trader reported
    "Broker returned: {"ok": false, "error": "sleeve check failed:
     name 'db_path' is not defined"}"
when trying to flatten an MCL orphan position. The bare ``flatten_position``
function referenced ``db_path`` without it being in scope. Fixed by
converting to the ``make_flatten_position(db_path=...)`` factory pattern
(consistent with sibling tools like ``make_get_fills``).

The bug only manifested at the sleeve-check step. Once the trader reaches
the IBKR broker connection, the bug would have been caught — but the
sleeve guard came FIRST, so the trader never got there.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.research import agent_tools


def test_make_flatten_position_is_callable():
    """The factory should return a callable with the documented signature."""
    fn = agent_tools.make_flatten_position(db_path="/tmp/whatever.db")
    assert callable(fn)
    # signature must remain (symbol, side, qty) — agents call it that way
    import inspect
    sig = inspect.signature(fn)
    params = set(sig.parameters.keys())
    assert params == {"symbol", "side", "qty"}, f"unexpected params: {params}"


def test_flatten_position_sleeve_check_uses_db_path(tmp_path: Path, monkeypatch):
    """End-to-end: with no open MCL trade, sleeve guard fires (NOT NameError).

    Pre-fix this would raise NameError: name 'db_path' is not defined.
    Post-fix it cleanly returns the 'no open agentic-crew trade' guard.
    """
    db_path = str(tmp_path / "sleeve.db")
    # Initialize an empty schema with just the trades table the sleeve
    # check inspects. No need to boot init_db here — _connect opens read-write
    # and the SQL just returns 0 rows.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE trades ("
            "  id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, status TEXT)"
        )
        conn.commit()

    # This regression covers the legacy IBKR sleeve guard. The live repo route
    # is NinjaTrader, whose truth-first orphan handling is intentionally different.
    monkeypatch.setattr("src.config.load_config", lambda: type(
        "Cfg", (), {"route": "ibkr"}
    )())
    fn = agent_tools.make_flatten_position(db_path=db_path)
    result = fn(symbol="MCL", side="close_long", qty=1)
    assert result["ok"] is False
    assert "no open agentic-crew trade" in result["error"], (
        f"sleeve guard did not engage — got: {result!r}"
    )
    # The key regression assertion: NO NameError leaked out.
    assert "db_path" not in result["error"], (
        f"db_path NameError leaked through result: {result!r}"
    )


def test_flatten_position_rejects_bad_symbol(tmp_path: Path):
    """Pre-sleeve guard: bad symbol rejected without DB access."""
    fn = agent_tools.make_flatten_position(db_path=str(tmp_path / "x.db"))
    result = fn(symbol="BANANA", side="close_long", qty=1)
    assert result["ok"] is False
    assert "unknown symbol" in result["error"]


def test_flatten_position_rejects_bad_qty(tmp_path: Path):
    """Pre-sleeve guard: bad qty rejected without DB access."""
    db_path = str(tmp_path / "x.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE trades ("
            "  id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, status TEXT)"
        )
        conn.commit()

    fn = agent_tools.make_flatten_position(db_path=db_path)
    # qty=0 → reject
    result = fn(symbol="MCL", side="close_long", qty=0)
    assert result["ok"] is False
    assert "qty must be > 0" in result["error"]
    # qty=-1 → reject
    result = fn(symbol="MCL", side="close_long", qty=-1)
    assert result["ok"] is False
    assert "qty must be > 0" in result["error"]


def test_nt_flatten_uses_atomic_closeposition(tmp_path: Path, monkeypatch):
    """NT flatten must cancel OCO children atomically, not race a MARKET order."""
    monkeypatch.setattr("src.config.load_config", lambda: type("Cfg", (), {
        "route": "ninjatrader",
        "ninjatrader": type("NT", (), {
            "account": "SimJudasFutures", "instrument_map": {"MGC": "MGC 12-26"},
            "host": "x", "user": "u", "python_exe": "python",
            "outgoing_dir": "out",
        })(),
    })())
    monkeypatch.setattr(agent_tools, "get_nt_positions", lambda: {
        "ok": True, "open_positions": [{
            "instrument": "MGC", "contract": "MGC 12-26",
            "side": "LONG", "qty": 1,
        }],
    })

    calls = []

    class FakeBroker:
        def __init__(self, **kwargs):
            pass
        def close_position_cmd(self, contract):
            calls.append(contract)
            return True
        def positions(self):
            return []
        def flatten(self, *args, **kwargs):
            raise AssertionError("MARKET flatten must not be used")

    monkeypatch.setattr("src.broker.ninjatrader.NTBroker", FakeBroker)
    monkeypatch.setattr("src.portfolio_runtime._resolve_nt_instrument_map",
                        lambda mapping: mapping)

    result = agent_tools.make_flatten_position(
        db_path=str(tmp_path / "unused.db")
    )(symbol="MGC", side="close_long", qty=1)
    assert result["ok"] is True
    assert result["action"] == "CLOSEPOSITION"
    assert result["verified_flat"] is True
    assert calls == ["MGC 12-26"]
