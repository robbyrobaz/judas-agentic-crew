"""2026-09-09: _place must not round prices to 4 decimals (6J/ZF need 7)."""
from src.broker.ninjatrader import NTBroker


def test_fmt_price_keeps_6j_and_zf_precision():
    f = NTBroker._fmt_price
    assert f(0.006518) == "0.006518"
    assert f(0.0065205) == "0.0065205"
    assert f(108.1328125) == "108.1328125"
    assert f(25000.25) == "25000.25"
    assert f(4518.8) == "4518.8"
    assert f(0.0) == "0"
    assert f(70.01) == "70.01"
