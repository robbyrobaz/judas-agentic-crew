# Research Findings — Judas Swing on Futures

## Executive Summary

5-minute Judas Swing on MGC is a documented loser with negative expectancy.
1-hour Judas Swing shows real structural edge. This system runs on 1H bars exclusively.

---

## 5m Judas Results (MGC) — Why We Abandoned It

| Metric | Value |
|--------|-------|
| Net P&L | -$93.00 |
| Total trades | 19 |
| Win rate | 26% |
| Expectancy | -0.21R |
| Timeframe | 5-minute bars |

**Root cause of failure**: 5-minute bars are too noisy for reliable sweep+CHoCH detection
on liquid micro futures. The stop placement (2 ticks beyond sweep) is constantly eaten
before the move develops. By the time the real reversal happens, we are already stopped
out. The signal-to-noise ratio at 5m is insufficient for this strategy.

**Key observation**: Even when the Judas setup was correct directionally, 5m stops
were triggered 74% of the time before the move developed. At 1H granularity, the
sweep is more definitive (represents a full hour of price action, not a 5-minute spike).

**Conclusion**: Never use 5m bars for Judas detection. The strategy is conceptually
sound but requires a timeframe where the sweep represents genuine session-level liquidity
engineering, not 5-minute noise.

---

## 1H Timeframe Research (NQ Futures)

Research on 81 strategy combinations tested on NQ 1-hour bars:

| Metric | 5m Bars | 1H Bars |
|--------|---------|---------|
| Profitable combinations | 2 / 81 (2.5%) | 40 / 81 (49%) |
| Median expectancy | -0.18R | +0.12R |
| Best expectancy | +0.08R | +0.30R |

The 1H timeframe shows dramatically better signal quality. Nearly half of all parameter
combinations are profitable, versus virtually none at 5m.

**Best 1H combination (NQ)**:
- Signal filter: RSI 25/75 mean-reversion gate (only take sweeps when RSI is oversold/overbought)
- Risk-reward: 2.0R target
- Stop sizing: 1.5× ATR
- Result: +$10,677 P&L, 43% win rate, +0.30R expectancy

---

## Working Hypothesis for This System

The superior 1H edge on NQ is expected to transfer to MGC (Micro Gold) for the following
structural reasons:

1. **MGC is a trend-following instrument**: Gold's long-duration trends mean the "sweep
   and reverse" structure is pronounced — the prior session level is more meaningful than
   on equity index futures.

2. **Fewer market participants at 1H**: The 1H bar represents genuine session-level
   decisions by institutional participants, not HFT noise at 5m.

3. **Prior day H/L as liquidity magnets**: MGC's daily levels are well-respected by
   algorithmic participants. The sweep of prior-day levels is a documented liquidity
   engineering pattern on gold.

4. **Validation in progress**: This system will validate the 1H Judas hypothesis on MGC
   paper trading (IBKR DUH860616). Target: ≥30 closed trades before drawing conclusions.

---

## Risk Parameters Derived from Research

The following parameters are derived from the research findings and encoded into the system:

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Min displacement strength | 1.5× | Below 1.5× = noise, not reversal |
| ATR contraction threshold | 0.5× | Compressed ranges → no energy for reversal |
| Min quality score to trade | 6/10 | Marginal setups destroyed 5m results |
| Confirmation bars | 4 | 5m used 6; tighter at 1H due to lower noise |
| Min sweep ticks | 3 | Below 3 ticks = micro-sweep, likely noise |
| Daily loss limit | -$300 | ~3 max losses per day before stopping |
| Target R | 2.0R | 2R is minimum to produce positive expectancy at 43% WR |

---

## What This System Is NOT

- Not a high-frequency system (1-2 setups per week is healthy)
- Not a 5-minute scalper (5m has negative expectancy — see above)
- Not a "take every signal" system (quality over quantity is the core discipline)
- Not proven live yet — Phase 1 is validation on IBKR paper account

---

## Phase 1 Success Criteria

The system will be considered validated when:
1. ≥ 30 closed paper trades are recorded
2. Win rate ≥ 35% (consistent with 43% research benchmark)
3. Expectancy ≥ 0.0R (breakeven or better)
4. Daily loss limit never breached (-$300 hard limit)

If these criteria are met, Phase 2 begins: IterationAgent tunes parameters,
MNQ support added, live account promotion evaluation.

---

## Data Sources

- Workshop backtester: `/home/rob/judas-futures-workshop/` (not imported here)
- MGC 5m results: paper trading log, sessions 2026-04-01 through 2026-04-19
- NQ 1H research: systematic backtest across 81 parameter combinations
- Research conducted prior to building this system; findings are pre-validated
