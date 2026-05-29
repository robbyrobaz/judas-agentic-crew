"""Per-symbol execution cost model for backtests and live review.

Until this module existed, every PF / expectancy / robustness_score in the
system was computed *gross*: no commission, no slippage. That overstates
profitability for any micro-futures strategy with reasonable trade frequency.
This file makes those metrics honest.

Sources & assumptions (v1, 2026-05-25):

- COMMISSION_PER_SIDE: IBKR cost-plus pricing for CME/CBOT/COMEX/NYMEX/NYBOT
  micros, 2026 published rates, exchange fees + regulatory + clearing rolled
  in. Round numbers, slightly pessimistic. CME micros (MNQ/MBT/MET/6J) ~$0.62.
  COMEX MGC ~$0.65. NYMEX MCL ~$0.65. CBOT ZF ~$0.50. NYBOT DX ~$0.85.
  Source: docs Rob keeps in judas-futures-workshop pricing notes; cross-checked
  against IBKR's published rate cards. Anything not listed defaults to $0.85
  to stay pessimistic.

- SLIPPAGE_TICKS_ENTRY: market-order entry slippage in ticks. Based on Rob's
  observed live fills on the IBKR paper + funded accounts (Apr-May 2026
  notebooks). 0.5 tick is realistic for highly liquid micros (MNQ); 1.0 tick
  is the safer default for less-liquid ones. PLACEHOLDER pending more data.

- SLIPPAGE_TICKS_STOP: stop-loss slippage. Stops gap. 1-2 ticks. PLACEHOLDER.

- SLIPPAGE_TICKS_TARGET: limit-target slippage. Limits either fill at the
  level or do not fill at all; if they do fill there is no adverse drift.
  Defaults to 0.0 across the board. We accept the (small) selection bias of
  pretending every "target hit" backtest result actually filled.

Update path: when Rob has clean live-fill telemetry, edit the dicts below.
No other file should hard-code per-symbol cost numbers.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Constants — update from observed live fills as data accumulates
# ---------------------------------------------------------------------------

COMMISSION_PER_SIDE: dict[str, float] = {
    "MGC": 0.65,
    "MNQ": 0.62,
    "MCL": 0.65,
    "MBT": 0.62,
    "MET": 0.62,
    "DX":  0.85,
    "ZF":  0.50,
    "6J":  0.62,
}

SLIPPAGE_TICKS_ENTRY: dict[str, float] = {
    "MGC": 1.0,
    "MNQ": 0.5,
    "MCL": 1.0,
    "MBT": 1.0,
    "MET": 1.0,
    "DX":  1.0,
    "ZF":  1.0,
    "6J":  1.0,
}

SLIPPAGE_TICKS_STOP: dict[str, float] = {
    "MGC": 2.0,
    "MNQ": 1.0,
    "MCL": 2.0,
    "MBT": 2.0,
    "MET": 2.0,
    "DX":  2.0,
    "ZF":  2.0,
    "6J":  2.0,
}

SLIPPAGE_TICKS_TARGET: dict[str, float] = {
    "MGC": 0.0,
    "MNQ": 0.0,
    "MCL": 0.0,
    "MBT": 0.0,
    "MET": 0.0,
    "DX":  0.0,
    "ZF":  0.0,
    "6J":  0.0,
}

_DEFAULT_COMMISSION = 0.85
_DEFAULT_SLIP_ENTRY = 1.0
_DEFAULT_SLIP_STOP = 2.0
_DEFAULT_SLIP_TARGET = 0.0

COST_MODEL_VERSION = "v1_realistic_micros"


# ---------------------------------------------------------------------------
# Tick-size lookup — single source of truth lives in portfolio_runtime.
# We import lazily to avoid a hard dependency at module import time
# (e.g. when running tests that don't need ib_async).
# ---------------------------------------------------------------------------


def _contract_specs() -> dict[str, dict]:
    from src.portfolio_runtime import _CONTRACT_SPECS  # type: ignore[attr-defined]
    return _CONTRACT_SPECS


def _tick_meta(symbol: str) -> tuple[float, float]:
    """Return (tick_size, tick_value) for symbol. Falls back to (0.0, 0.0)
    for unknown symbols so callers see zero slippage rather than crashing."""
    sym = symbol.upper()
    spec = _contract_specs().get(sym)
    if not spec:
        return 0.0, 0.0
    return float(spec.get("tick", 0.0)), float(spec.get("tick_value", 0.0))


# ---------------------------------------------------------------------------
# Public pure functions
# ---------------------------------------------------------------------------


def commission_per_side(symbol: str) -> float:
    """Commission per single execution (entry OR exit), in dollars."""
    return float(COMMISSION_PER_SIDE.get(symbol.upper(), _DEFAULT_COMMISSION))


def commission_per_round_trip(symbol: str) -> float:
    """Total commission for entry + exit (the realistic round-trip)."""
    return 2.0 * commission_per_side(symbol)


def slippage_dollars(symbol: str, exit_reason: str | None = "stop") -> float:
    """Total entry+exit slippage in dollars for one contract.

    Math: ticks * tick_value, summed over entry and exit.
      entry_slip = SLIPPAGE_TICKS_ENTRY[symbol] * tick_value
      exit_slip  = SLIPPAGE_TICKS_STOP[symbol]   * tick_value   (if stop)
                 = SLIPPAGE_TICKS_TARGET[symbol] * tick_value   (if target)
                 = 0                                            (otherwise)

    Long vs short does not matter — slippage is symmetric in dollars.
    """
    sym = symbol.upper()
    _tick, tick_value = _tick_meta(sym)
    entry_ticks = float(SLIPPAGE_TICKS_ENTRY.get(sym, _DEFAULT_SLIP_ENTRY))
    if exit_reason == "stop":
        exit_ticks = float(SLIPPAGE_TICKS_STOP.get(sym, _DEFAULT_SLIP_STOP))
    elif exit_reason == "target":
        exit_ticks = float(SLIPPAGE_TICKS_TARGET.get(sym, _DEFAULT_SLIP_TARGET))
    else:
        # Unknown / open / manual close → assume neutral exit, entry only.
        exit_ticks = 0.0
    return (entry_ticks + exit_ticks) * tick_value


def round_trip_cost(symbol: str, exit_reason: str | None = "stop") -> float:
    """Commission + slippage for a single-contract round trip.

    Defaults to the pessimistic 'stop' case so callers that don't know the
    exit reason can't accidentally under-cost.
    """
    return commission_per_round_trip(symbol) + slippage_dollars(symbol, exit_reason)


def apply_costs_to_trade(
    *,
    symbol: str,
    gross_pnl: float,
    exit_reason: str | None = "stop",
    qty: int = 1,
) -> float:
    """Return net pnl after subtracting per-contract round-trip cost * qty."""
    qty = max(int(qty), 1)
    return float(gross_pnl) - round_trip_cost(symbol, exit_reason) * qty
