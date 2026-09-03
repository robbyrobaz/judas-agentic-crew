# Runtime reliability invariants — 2026-09-02

Current execution venue is NinjaTrader SIM account `SimJudasFutures`. Do not
label its trades, fills, equity, or P&L as live.

- Custom backtests pass the proposed `params` and `qty`, convert price movement
  through the symbol's contract economics, subtract the centralized execution
  cost model, and stamp results with `cost_model=v1_realistic_micros`.
  Unstamped legacy custom results are research-only, not promotion evidence.
- NinjaTrader account truth fails closed. Missing account summary, missing
  positions, a zero-cash/flat snapshot, or a guard exception halts new entries.
- IBKR legacy reconciliation prefers actual child-order executions and labels
  them `target_real`/`stop_real`; bar-touch fallback is explicitly labeled
  `target_synthetic`/`stop_synthetic`. Review can filter to real fills.
- NinjaTrader position flattening uses `CLOSEPOSITION`, which cancels working
  OCO children atomically. A naked MARKET close can race those children and
  increase exposure, so agents must not emulate flattening with one.
- Specialist LLM cycles have bounded turns, wall time, per-run tokens, and a
  shared daily budget. Registrar/reviewer/coder skip cycles without new work.
  Repeated identical autofix failures remain parked for human review.
- Pytest discovery is limited to `tests/test_*.py`; research artifacts are not
  test modules.
