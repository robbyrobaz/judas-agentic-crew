"""NT sqlite MarketPosition: 0=Long, 1=Short. positions() must map it that way
and must not filter LONG rows out (regression: 2026-09-07 6J short read as LONG)."""
import inspect
from src.broker import ninjatrader


def test_positions_query_uses_nt_enum_correctly():
    src = inspect.getsource(ninjatrader.NTBroker.positions)
    assert "p.MarketPosition IN (0,1)" in src
    assert "'LONG' if r['mp']==0 else 'SHORT'" in src
    assert "p.MarketPosition!=0" not in src
