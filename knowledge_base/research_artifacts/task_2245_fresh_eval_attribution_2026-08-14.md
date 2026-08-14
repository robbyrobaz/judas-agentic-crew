# Task #2245 — FRESH_EVAL_PER_STRATEGY_ATTRIBUTION_AND_MNQ_BOOK_CONSOLIDATION

**Cycle:** 2026-08-14T12:30Z (08:00 EDT, eval LFE05064290360100 LIVE Day 3)
**Window:** 2026-08-12T00Z → 2026-08-14T12:30Z (≈2.5 trading days)
**Trades analyzed:** 27 closed (since cutover); 39 closed (all fresh eval); 13 closed today (2026-08-14)
**Realized P&L fresh-eval cumulative:** +$282.50 (across 39 closed trades)
**Realized P&L today (closed):** -$31.00 (13 trades; 11 on MNQ -$199, 2 on MCL +$168)

---

## WS-A: Per-Strategy Fresh-Eval Attribution (since 2026-08-12T00Z)

### Winners (top 3 — protect, ensure slot priority)

| ID | Symbol | Family | Ver | CSID | Fresh P&L | n | nW | nL | WR% |
|---|---|---|---|---|---|---|---|---|---|
| 4517 | MNQ | custom_15m | 2 | 219 | **+$239.00** | 1 | 1 | 0 | 100% |
| 4532 | MCL | custom_1h | 1 | 228 | **+$168.00** | 2 | 2 | 0 | 100% |
| 4513 | MNQ | custom_15m | 1 | 156 | **+$103.50** | 2 | 2 | 0 | 100% |
| 4528 | MNQ | custom_15m | 5 | 229 | +$39.50 | 7 | 1 | 6 | 14.3% |

The 4th row (4528) is a "winner" only in aggregate — its WR is **catastrophic at 14.3%**. See WS-C diagnosis.

### Losers (top 3 — fresh-eval negative P&L)

| ID | Symbol | Family | Ver | CSID | Fresh P&L | n | nW | nL |
|---|---|---|---|---|---|---|---|---|
| 4360 | MNQ | custom (1h) | 4 | 212 | **-$60.50** | 1 | 0 | 1 |
| 4539 | MNQ | custom_5m | 13 | 250 | -$13.00 | 1 | 0 | 1 |
| 4526 | MCL | custom_15m | 1 | 235 | -$8.00 | 1 | 0 | 1 |

Sample sizes too small for retire on fresh-eval alone; lifetime context (4360 +$447.50 over 11 trades pre-eval) argues against panic-retire.

### Zero-Fires (27 of 30 active strategies have NOT traded since cutover)

This is a **critical finding** — the book is essentially MNQ-only in fresh eval:

| Symbol | Total active | Zero-fire | % silent |
|---|---|---|---|
| 6J | 4 | **4** | 100% silent |
| ZF | 4 | **4** | 100% silent |
| MGC | 9 | **9** | 100% silent |
| MCL | 4 | 3 (4532 only fired) | 75% silent |
| MNQ | 9 (now 8 after today's retirements) | 3 (4536, 4537, 4544) | 33% silent |
| **TOTAL** | **30** | **23** | **77% silent** |

Implication: The fresh-eval window is **MNQ-only** for closed-trade P&L. The 27 silent strategies are waiting for their setups (likely 1h/15m-cycle-based). On a 4-contract cap, the daily book uses 1-2 MNQ + 0-1 MCL slots; the rest are idle by design.

### Over-Concentration Clusters (CSID families with multiple actives)

| CSID | Family | Active count | IDs |
|---|---|---|---|
| 250 | ob_midpoint_reversion_5m_loose | **4** | 4520 MCL, 4538 MGC, 4539 MNQ, 4540 6J |
| 254 | judas_continuation_5m | **4** | 4547 MCL, 4545 MGC, 4544 MNQ, 4546 ZF |
| 235 | ifvg_midpoint_reversion_htf_bias | **4** | 4524 6J, 4526 MCL, 4398 MGC, 4525 ZF |
| 165 | ict_silver_bullet_FIB | 3 | 4327 MGC, 4536 MNQ 15m, 4537 MNQ 5m |
| 229 | silver_bullet_pdh_pdl_retest roll24/1h | 3 | 4518 MGC 15m, 4519 MGC 1h, 4528 MNQ 15m |
| 228 | regime_filtered_rsi_1h (orig) | 3 | 4531 6J, 4532 MCL, 4535 ZF |
| 212 | regime_filtered_rsi_1h_stateless | 2 | 4360 MNQ, 4542 ZF (4543 MNQ retired today) |
| 156 | atr_disp_continuation | 2 | 4527 6J 5m, 4513 MNQ 15m |

**Note on 7532 finding:** 5+ actives carry names that don't match the original CSID symbol/timeframe (e.g., CSID 156 is "atr_disp_continuation_5m_6j_v1" but reused as 15m MNQ). Code is stateless, so it works, but attribution is suspect — see finding 7532 for the audit list.

---

## WS-B: MNQ Book Consolidation (8 actives after today's auto-retirements)

**Today's MNQ retirements (per findings 7535, 7536):** 4477 (5m silver_bullet v8), 4523 (15m silver_bullet v4), 4543 (1h RSI period=7). All three were cross-TF/CSID duplicates of higher-PF sisters. Net: book shrunk from 11 → 8.

| ID | Ver | CSID | Strategy | Fresh P&L | n | Verdict | Reason |
|---|---|---|---|---|---|---|---|
| 4360 | custom 1h (v4) | 212 | regime_filtered_rsi_1h_mnq_v1 | -$60.50 | 1 | **KEEP** | Lifetime +$447.50 (11 trades, 36% WR). 1 fresh loss insufficient to retire. CSID 212 sister 4543 was retired today — 4360 is now sole MNQ RSI 1h representative. |
| 4513 | custom_15m (v1) | 156 | atr_disp_continuation_15m_mnq_atr10_v1 | +$103.50 | 2 | **KEEP** | Fresh-eval 2W/0L. Confirmed winner on this regime. |
| 4517 | custom_15m (v2) | 219 | silver_bullet_pdh_pdl_retest_15m_mnq_v1 | +$239.00 | 1 | **KEEP** | Fresh-eval +$239. Single trade — sample size thin but profitable. Distinct CSID 219 (not the roll24 variant). |
| 4528 | custom_15m (v5) | 229 | silver_bullet_pdh_pdl_retest_15m_mnq_roll24_v1 | +$39.50 | 7 | **CONDITIONAL** | Fresh-eval WR=14.3% (1W/6L). Net positive but **all from a single +$235 win offsetting 6 losses**. Today alone: 0W/4L -$85. **High-priority watch — chronic low-WR bleed.** |
| 4536 | custom_15m_FIB (v1) | 165 | ict_silver_bullet_15m_FIB_mnq_v1 | $0 | 0 | **KEEP** | Fresh-eval silent. Keep gathering 15m FIB data (sister of 4537 5m FIB). |
| 4537 | custom_5m_FIB (v1) | 165 | ict_silver_bullet_5m_FIB_mnq_v1 | $0 | 0 | **KEEP** | Fresh-eval silent. Both 4536 and 4537 are CSID 165 — could consider retiring one if no fires by next week, but keep for now to gather 5m/15m FIB comparison. |
| 4539 | custom_5m (v13) | 250 | ob_midpoint_reversion_5m_loose_mnq_v1 | -$13.00 | 1 | **CONDITIONAL** | Fresh-eval -$13 from 1 trade. Paper-only by design (cross-symbol port from MCL). Lifetime 36 5m-trades BT PF=5.85. **Keep but monitor WR — CSID 250 has 4 actives across the book.** |
| 4544 | custom_5m (v14) | 254 | judas_continuation_5m_mnq_v2 | $0 | 0 | **KEEP** | Fresh-eval silent. Just activated 2026-08-13T07:06Z. Needs more bars to fire. CSID 254 has 4 actives across the book — keep at least one per symbol for diversification. |

### WS-B Summary

- **KEEP: 6** strategies (4360, 4513, 4517, 4536, 4537, 4544)
- **CONDITIONAL: 2** strategies (4528, 4539) — both have negative or low-WR signals on thin fresh-eval data
- **RETIRE recommended now: 0** — none cross retire thresholds on fresh-eval alone

**Net MNQ count:** 8 (down from 11 pre-today). With 4-contract cap, this leaves headroom for 1-2 MNQ positions concurrently. The book is **right-sized post today's convergent retirements** (findings 7535, 7536).

**Watch-list for next-week review:**
1. **4528** (roll24 silver_bullet) — if WR stays <30% on n>=15 cumulative → retire. Today is a strong negative signal.
2. **CSID 165 MNQ pair** (4536/4537) — if both remain zero-fire through 2026-08-21, retire one as redundant.
3. **4539** (ob_midpoint_reversion MNQ) — if n>=5 with WR<35% → retire; CSID 250 family already bleeds on other symbols.

---

## WS-C: Why Is MNQ Bleeding Today? (2026-08-14)

### Time-of-day distribution (UTC)

| Hour UTC | Local EDT | n | nW | nL | P&L | Avg | Comment |
|---|---|---|---|---|---|---|---|
| 02 | 22:00 prev day | 2 | 0 | 2 | -$62.50 | -$31.25 | Asia session — overnight gap |
| 03 | 23:00 prev day | 2 | 1 | 1 | +$7.50 | +$3.75 | Late Asia — single win |
| 04 | 00:00 | 1 | 0 | 1 | -$13.50 | -$13.50 | Asia dead zone |
| 06 | 02:00 | 2 | 0 | 2 | -$27.50 | -$13.75 | Pre-London — bleed |
| 07 | 03:00 | 1 | 0 | 1 | -$8.00 | -$8.00 | London pre-open |
| 08 | 04:00 | 1 | 0 | 1 | -$16.00 | -$16.00 | London — bleed |
| 11 | 07:00 | 4 | 1 | 3 | -$79.00 | -$19.75 | Pre-NY-open — **THE WORST HOUR** |

**No trades in NY-Cash hours (12-16 UTC / 08:00-12:00 EDT) yet today.** Session distribution shows the bleed is concentrated pre-NY-open (11 UTC) and overnight Asia/London.

### Side distribution

| Direction | n | nW | nL | P&L | Avg | WR% |
|---|---|---|---|---|---|---|
| LONG | 7 | 1 | 6 | -$8.50 | -$1.21 | 14.3% |
| SHORT | 6 | 1 | 5 | **-$190.50** | **-$31.75** | 16.7% |

**SHORT side bleeding catastrophically today** — avg short loss is -$31.75 vs long -$1.21. The 11:11-12Z cluster of shorts (4360 -$60.5, 4543 -$65, 4528 -$12.5) drove the bulk of the short-side damage.

### Strategy-level (today)

| ID | Status | n | nW | nL | P&L | Comment |
|---|---|---|---|---|---|---|
| 4528 | active | 4 | 0 | 4 | **-$85.00** | **THE BLEEDER** — 4 trades, 4 stops/manual-closes, zero wins today |
| 4543 | retired today | 1 | 0 | 1 | -$65.00 | Duplicate of 4360, co-fired same bar |
| 4360 | active | 1 | 0 | 1 | -$60.50 | Manual close on the duplicate-fire short |
| 4523 | retired today | 4 | 0 | 4 | -$45.50 | Already retired per finding 7536 |
| 4539 | active | 1 | 0 | 1 | -$13.00 | Single stop |
| 4522 | retired (7536) | 1 | 1 | 0 | +$11.00 | The only MNQ win today is from a RETIRED strategy |
| 4513 | active | 1 | 1 | 0 | +$59.00 | The only ACTIVE-STRATEGY MNQ win today |

### Root-cause verdict

**PRIMARY: specific_strategy_failure on 4528 (CSID 229 roll24).**
- 4 trades, 0 wins today. Lifetime on fresh eval: 1W/6L = 14.3% WR.
- The "win" was a +$235 outlier at 2026-08-13T14:01Z; without it, 4528 is -$200+ lifetime.
- Roll24 variant of silver_bullet_pdh_pdl_retest is **firing too often in chop** — generating 6 small losses for every 1 win. This is a textbook overtrading signature.

**SECONDARY: duplicate-fire pollution at 11:11:42-51Z.**
- 4360 (custom 1h CSID 212) and 4543 (custom_1h CSID 212) co-fired on the same MNQ short bar.
- Both manually closed within minutes for -$60.50 and -$65 — adding ~$125 of unnecessary exposure.
- Finding 7535 caught this and retired 4543. 4360 stands alone now.

**TERTIARY: regime_mismatch.**
- 4528 roll24 silver_bullet was backtested for trending PDH/PDL retests. Current regime is **chop** (per Day 2-3 brief). PDH/PDL retests in chop produce repeated failures at the level.
- 4528 needs a regime filter (e.g., ADX > 20, or ATR ratio > 1.2x) to gate entries — but per STRICT RULES we cannot modify params; only diagnose.

**OVERALL DIAGNOSIS:** The today's MNQ bleed is a **specific_strategy_failure + duplicate-fire pollution** event, NOT a regime-wide architectural breakdown. The two confirmed winners (4513 atr_disp, 4517 silver_bullet roll96) showed they can still win in this regime. The book needs:
1. **Watch 4528 closely** — if next 5 trades don't recover WR to ≥35%, recommend retire at next week's review.
2. **No action needed on 4360, 4513, 4517, 4536, 4537, 4544, 4539** — they're either silent or net positive.
3. **CSID 212 dedup is now complete** (4543 retired). 4360 has the slot alone.

---

## DELIVERABLES SUMMARY

- WS-A: 1 table per active strategy + zero-fire list + CSID cluster list ✓
- WS-B: 1 table per MNQ strategy with verdict + 1-paragraph summary ✓
- WS-C: 3 tables (hour/side/strategy) + 1-paragraph root-cause verdict ✓

## RECOMMENDATIONS TO REGISTRAR (not executed — strict rules)

1. **No immediate retires** — sample sizes too thin for fresh-eval window. Wait for next-week review (≥10 trades per strategy) before any further MNQ consolidation.
2. **Monitor 4528 closely** — flag for review if 5+ more trades without WR recovery.
3. **Consider same-day convergence** if a second sister (CSID 165 MNQ pair 4536/4537) remains zero-fire through 2026-08-21.
4. **Open positions:** 2 MNQ SHORT @ 30243.0/30243.5 from 4360/4543 — already delegated to trader (task #2244).

## REF CONTEXT HONORED

- finding 6511 (BT-LURE gate) — fresh-eval samples too small for promotion, hence watch-not-retire
- finding 9259d980 (CSID 232 catastrophe) — similar iFVG pattern, apply caution if 4529 (MGC CSID 232) starts firing
- finding f2a1a7ad (eval pass math) — fresh eval P&L = +$282.50 / Day 3 P&L = -$31 closed + 2 open
- finding 7253 (#4360 KEEP) — confirmed; 4360 lifetime +$447.50 supports retention
- finding 2e024aa6 (fire-readiness) — 23/30 silent on fresh eval; consistent with fire-readiness gating

## DOLLAR IMPACT (projected)

- WS-A identification: surfaces +$282.50 captured (worth protecting) and isolates which actives are silent (don't churn them).
- WS-B net: today's convergent retirements already saved 2-4 MNQ slots; if 4528 retired next week, save ~$20-50/day in bleed.
- WS-C diagnosis: confirms regime_mismatch + specific-strategy failure; no architectural change needed.
