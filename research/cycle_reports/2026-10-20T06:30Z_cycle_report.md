# Cycle 2026-10-20T06:30Z — Researcher cycle report

## State
- Roster: 171 actives (floor=10). Way above floor; no refill needed.
- candidates_3d=29, candidates_7d=91, retired_7d=78
- Two new candidates queued this cycle: 7366 (csid=270 ZF 5m), 7367 (csid=432 ZF 1h)
- 3 open research findings recorded.

## YouTube / Web ingest (Pre-London 06:30Z)
- DDG video search returned 0 results for most queries today (5 failures across ICT/2026/FVG/Macro/SMC search terms).
- One YouTube transcript fetched successfully: **Kairotick Trading — Fibonacci OTE Step-by-Step** (qevf-uEGjIU). Recorded as finding "YT:qevf-uEGjIU:fibonacci_ote_entry_step_by_step_v1".
- Web fetch of goldtrading.ai daily brief provided today's specific levels: PDH $4179.60, PDL $4153.11, Asian $4169.60/$4163.11, EQ $4136.83. Bias Bullish, expected Judas sweep of Asian High first then retrace to SSL $4153.11.
- Web fetch of ictkillzone.com Judas Swing article extracted full Judas Swing mechanics (weekly bias → session sweep → MSS → FVG 50%).

## Backtests
### OTE Framework (qevf-uEGjIU transcribe) — REJECTED
Three variations of sweep+BOS+0.705 Fib band tested on MGC 15m, MCL 15m, ZF 15m, 6J 15m:
- v1: MGC n=21, PF=0.54, E[R]=-0.21 (-$1015)
- v2: MGC n=19, PF=0.30, E[R]=-0.57 (-$1692); MCL n=16, PF=0.56, E[R]=-0.35
- v3: target=swing structure: 6J n=2 meaningless; ZF n=4 PF=0.40

Diagnosis: entries are firing too late in the retracement — BOS candle is ~5-8 bars into the move by the time we detect it, leaving thin remaining R. Documented rejection notes and a fix candidate (tight BOS cluster within 5 bars, lift struct_lookback to 30, enter on first retracement candle inside OTE band not on the second/third).

### Refill — csid=270 silver_bullet ZF 5m (PF=2.14, n=22, WR=68%, E[R]=+0.85)
- Fresh v1 stamp at days=90; previously refused on BT-LURE signature (PF=6.52, WR=85.7%) which is no longer present in current stamp.
- Same architecture on cross-asset: MCL 5m n=113 PF=5.69 (overfit signature — DO NOT propose, MCL 5m also 4 retirements in 7d burnout); MGC 5m n=112 PF=2.44 (DO NOT propose, MGC 5m 4 retirements). MNQ 5m 0 signals (custom_5m also 3 retirements). 6J 5m 0 signals with wrong tick (was 0.000001, fixed in csid=325 to 0.0001).
- **PROPOSED: candidate 7366** (csid=270, ZF, silver_bullet_pdh_pdl_retest_5m_nogate_v2). Awaiting registrar review; max-2 same-TF rule may trip due to existing 4+ ZF 5m silver_bullet actives.

### Refill — csid=432 NEW: ZF 1h silver_bullet (PF=2.64, n=38, WR=63%, E[R]=+0.78, +$4181)
- Registered new custom_strategy_id=432 (silver_bullet_pdh_pdl_retest_1h_zf_nogate_roll4_v1).
- Tested on ZF 1h native bars at days=180 with rolling 4-bar PDH/PDL (4h) and 8-bar max retest.
- Architecture identical to 5m variants but on 1h timeframe.
- Only 2 ZF 1h actives currently; no 1h custom burnout flag.
- PF=2.64 (not overfit signature — below 3), WR=63% (not 90%+), E[R]=+0.78 with 38 trades clears all gates.
- **PROPOSED: candidate 7367** (csid=432, ZF, silver_bullet_pdh_pdl_retest_1h_nogate_roll4_v1). Awaiting registrar.

### ICT Judas Swing — DEFERRED
- Three iterations of Asian H/L sweep + MSS + FVG 50% entry.
- All three returned 0 signals. Debug identified bug: computing "Asian high" from `bars["high"][last-96:last+1]` includes the current bar; sweep condition becomes unsatisfiable.
- Fix candidate documented in finding bc182edf but not implemented this cycle (cycle already at 6 backtests).
- Judas Swing mechanism is genuinely novel vs roster; defer to next cycle with proper 24-bar Asian window + explicit weekly bias filter via close vs 5-day range midpoint.

## Status
- 2 candidates queued (7366, 7367), both awaiting registrar.
- 3 open findings recorded (OTE rejection with fix candidate, Judas Swing debug, Kairotick OTE transcript extract).
- Cycle productive: 2 promotions in queue, 2 negative findings saved for future reference.

## Next cycle
1. Re-attempt Judas Swing with proper Asian session window (last 24 bars EXCEPT current).
2. Try csid=270 silver_bullet on a different symbol+TF that's not max-2 saturated (e.g., MNQ 1h with sufficient cross-symbol validation).
3. Continue YouTube ingest when DDG recovers — failed searches today.
