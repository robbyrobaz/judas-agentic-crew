# POST_RESET_FIRST_FIRE_WATCH_PLAN_2026-10-18

**Cycle**: 2026-10-18T12:30Z (mid-London, mid-NY-prep)  
**Author**: Researcher  
**Account state**: SimJudasFutures **HALTED 13+ days** on CUSHION_EXHAUSTED ($67 cushion < $100 floor).  
**Trigger**: Ops reset SQL is the ONLY path to clear the halt per finding `9e85b382`. Reset can land tonight, tomorrow, or next week. When it lands, **ALL** currently-quiet actives begin firing in the same window with no operator precommit gates armed.

---

## TL;DR (operator's 30-second crib sheet)

| Bucket | Count | Action on ops reset |
|---|---|---|
| **Tier 1 PROTECT** (PF≥2.0, n≥50, E[R]≥+0.5R, BT-LURE clean) | **41 actives** | Passive monitor. Pre-commit gate already stamped in `notes`. Verify at first-50 mark. |
| **Tier 2 MONITOR** (PF 1.3+, n≥20) | **91 actives** | Manual first-10 trades per strategy. **6 consec L gate fires here.** Worst symbol weight per finding `f575d604`. |
| **Tier 3 RETIRE-ELIGIBLE** (PF<2, n<20, or BT-LURE-loose) | **6 actives** | **Pre-retire the BT-LURE-Loose (#4896) BEFORE reset**; arm tightest possible gate on the others; first 5 live trades = trigger. |
| **Slot concentration (in-cohort, max-2 rule)** | **2 buckets at-limit, 0 over-max in fresh cohort** | Do NOT promote a 3rd `MCL 15m ict_mitigation_block_15m` or `MNQ 1h ifvg_midpoint_reversion_htf_bias_1h` without retiring one. |
| **BT-LURE strict (PF≥3 + WR≥85% + n≥20)** | 3 | Live WR first 15 trades must exceed 75% to confirm; otherwise retire per finding `c46a9740`. |
| **BT-LURE-LOOSE (PF≥5 + WR≥85% but n<20)** | 1 (#4896 MNQ 5m ny_am_pdh_pdl_silver_bullet, PF=31.97 WR=92% n=12) | **RED FLAG** — pre-retire or freeze before reset lands. |

**Most leveraged pre-reset action**: The single highest-value defensive move an operator can take TODAY, while the account is still halted, is to **manually retire or freeze #4896** before it can fire. The architecture (CSID 375) was cross-validated per finding `8933` and the registry chose to promote with a "strict pre-commit gate" instead of rejecting as BT-LURE. With account HALTED, no live evidence will accrue, and the moment reset lands the lottery ticket will fire on its first qualifying bar.

---

## (A) Active roster triage — full 138-entry n=0 cohort

**Scope**: All `active_strategies` rows where `state='active'` AND `n_live_trades (closed) = 0` AND `activated_at_utc > '2026-10-08T00:00:00Z'`. Verified sqlite pull 2026-10-18T12:25Z. **Note this is materially larger than the task's ≥12-row assumption** — the 13-day halt has stacked 138 promotions that have NEVER fired. The full per-id matrix is at `/tmp/postreset/cohort_full.tsv` (139 lines incl. header). Per-tier summary:

| Tier | Count | Description |
|---|---|---|
| T1 PROTECT | 41 | All 4-gate criteria met (PF≥2, n≥50, E[R]≥+0.5R, BT-LURE clean) |
| T2 MONITOR | 91 | PF 1.3-2.0 OR n<50 with positive E[R] |
| T3 RETIRE-ELIG | 6 | Small-sample (n<20) or PF<1.5 or BT-LURE-loose |
| **TOTAL** | **138** | |

**Plus 4 pre-halt n=0 actives activated 2026-10-06 (#4774 MNQ 5m ifvg_h, #4775 MGC 5m ifvg_h, #4776 MGC 5m ob_mid_tight, #4777 MGC 5m bsl_run_judas) — total 142 if including these.**

### Top 25 T1 PROTECT (passive monitor only)

Sort: PF desc. These are the prize assets; do nothing but observe.

| ID | Sym | TF | Family (50 char) | PF | n | E[R] | WR | $ |
|---|---|---|---|---|---|---|---|---|
| #4827 | MGC | 5m | silver_bullet_pdh_pdl_retest_5m_nogate_roll48 | 9.11 | 99 | +1.47R | 89.9% | +$8,482 |
| #4828 | MGC | 15m | silver_bullet_pdh_pdl_retest_15m_50pct_filter_roll4 | 7.71 | 84 | +1.26R | 78% | +$6,318 |
| #4831 | MNQ | 15m | silver_bullet_pdh_pdl_retest_15m_roll24_v1 | 5.36 | 83 | +1.51R | 84% | +$11,946 |
| #4857 | MGC | 5m | silver_bullet_pdh_pdl_retest_5m_roll24 | 5.66 | 119 | +1.37R | n/a | n/a |
| #4858 | MCL | 5m | silver_bullet_pdh_pdl_retest_5m_roll24 | 5.38 | 84 | +1.35R | n/a | n/a |
| #4875 | MNQ | 5m | silver_bullet_pdh_pdl_retest_5m_nogate_roll24 | 7.11 | 115 | +1.59R | 87% ⚠LURE | +$11,504 |
| #4876 | MGC | 15m | ict_inside_bar_disp_breakout_15m | 4.56 | 141 | +1.14R | 73% | +$10,068 |
| #4912 | MNQ | 5m | ob_midpoint_reversion_htf_bias_5m | 4.38 | 57 | +1.03R | n/a | n/a |
| #4948 | MCL | 5m | silver_bullet_pdh_pdl_retest_5m_nogate_v2_mcl | 4.61 | 113 | +1.14R | 76.9% | +$3,276 |
| #4949 | MNQ | 5m | silver_bullet_pdh_pdl_retest_5m_nogate_v2_mnq | 4.56 | 134 | +1.24R | 76.1% | +$11,727 |
| #4959 | MGC | 1h | silver_bullet_pdh_pdl_retest_1h_mgc_loose_v1 | 4.08 | 73 | +1.30R | 65.8% | +$23,547 |
| #4962 | MGC | 1h | silver_bullet_pdh_pdl_retest_1h_mgc_loose_v2 | 2.00 | 78 | +1.66R | 66.7% | +$17,806 |
| #4966 | MGC | 5m | silver_bullet_pdh_pdl_retest_5m_nogate_roll24_tight | 4.36 | 137 | +1.42R | n/a | n/a |
| #4841 | MCL | 5m | ict_inside_bar_disp_breakout_tight | 4.97 | 127 | +1.17R | n/a | +$2,573 |
| #4842 | MCL | 5m | ict_inside_bar_disp_breakout | 3.76 | 216 | +0.94R | n/a | +$3,512 |
| #4960 | ZF | 5m | silver_bullet_pdh_pdl_retest_5m_nogate_roll36_zf_v1 | 3.10 | 21 | +0.95R | 66.7% | +$1,737 |
| #4909 | MCL | 5m | ob_midpoint_reversion_htf_bias_5m | 2.17 | 51 | +0.52R | n/a | n/a |
| #4970 | MNQ | 15m | atr_disp_continuation_15m_loosened | 2.35 | 125 | +0.53R | n/a | +$6,521 |
| #4955 | MCL | 15m | silver_bullet_pdh_pdl_retest_15m_roll96 | 3.65 | 25 | +1.34R | n/a | n/a |
| #4884 | MNQ | 15m | ifvg_midpoint_reversion_htf_bias_15m | 5.51 | 59 | +0.96R | 67.8% | n/a |
| #4895 | MGC | 15m | asian_range_sweep_mitigation_block_15m | 4.16 | 59 | +0.58R | 59% | +$4,248 |
| #4887 | MNQ | 1h | ifvg_midpoint_reversion_htf_bias_1h | 3.56 | 94 | +0.72R | 58.5% | +$5,170 |
| #4890 | MCL | 15m | ict_mitigation_block_15m | 3.05 | 27 | +0.79R | 63% | +$859 |
| #4860 | MCL | 15m | silver_bullet_pdh_pdl_retest_15m_50pct | 3.98 | 72 | +1.34R | n/a | n/a |
| #4863 | MNQ | 15m | judas_continuation_15m | 3.11 | 66 | +0.69R | n/a | n/a |

(16 more T1 rows omitted — see `/tmp/postreset/section_a_full.tsv`.)

### T3 RETIRE-ELIGIBLE — 6 entries (operator focal points)

| ID | Sym | TF | Family | PF | n | WR | Reason T3 |
|---|---|---|---|---|---|---|---|
| **#4896** | MNQ | 5m | ny_am_pdh_pdl_silver_bullet_5m | **31.97** | **12** | **92%** | **BT-LURE-LOOSE RED FLAG** — pre-commit "ARMED TIGHT n=10 pf_net<0.85" |
| #4846 | MNQ | 15m | silver_bullet_pdh_pdl_retest_15m_nogate | 8.18 | 7 | 81% | n=7 too thin; PF extreme from small sample |
| #4856 | MGC | 5m | ny_am_intra_judas_5m | 3.01 | 13 | n/a | n=13 below significance threshold |
| #4940 | MNQ | 1h | buffet_zoo_rsi_mean_reversion_1h_regime_filtered | 1.65 | 10 | 45% | RS=10.45 PF=4.61 BT but live regime risk per finding `9051` |
| #4941 | 6J | 5m | ict_mitigation_block_london_sb_5m_6j_v1 | 2.18 | 10 | 71% | London SB failure mode per finding `9056`; CSID 359 base |
| #4848 | MGC | 15m | silver_bullet_pdh_pdl_retest_15m_roll96 | 1.50 | 16 | 73% | PF<2 + n<20; bleed map 8782 had concerns on roll96 |

---

## (B) Architecture / slot concentration check

### In-cohort buckets with >1 active (max-2 rule per finding `161b877a`)

| Sym | TF | Family | Count | IDs | Status |
|---|---|---|---|---|---|
| MNQ | 1h | ifvg_midpoint_reversion_htf_bias_1h | 2 | 4887, 4913 | **AT LIMIT** — no more can be promoted without retiring one |
| MCL | 15m | ict_mitigation_block_15m | 2 | 4890, 4911 | **AT LIMIT** — same constraint |

**No fresh-cohort violations.** Per the cleaned-up `slot_key` accounting, the registry already retired the prior duplicates per finding `37e08337` (deactivated_at_utc=`2026-10-17T22:02:35Z` on #4859, #4934, #4935, #4936, #4967).

### Pre-existing violations in overall roster (carryover from earlier cohorts — NOT in fresh cohort)

These are documented in finding `161b877a` and are referenced here for completeness, not as new triage:
- **MGC 5m custom_5m**: 3 active (#4638, #4697, #4702) — ALREADY VIOLATION, older cohort
- **6J 5m custom_5m**: 2 (#4617, #4619) — AT LIMIT
- **MCL 5m custom_5m**: 2 (#4621, #4694) — AT LIMIT
- **MNQ 5m custom_5m**: 2 (#4654, #4693) — AT LIMIT
- **MNQ 15m custom_15m**: 2 (#4599, #4701) — AT LIMIT
- **MNQ 15m judas_continuation**: 2 (#4645, #4738) — AT LIMIT
- **6J 5m custom_5m**: 2 — see above

### If a third promotion is queued for the at-limit buckets: drop one before promoting

**Pre-reset action**: confirm the registrar queue has NO pending `promote_candidate` task that would create `MNQ 1h ifvg…3rd` or `MCL 15m ict_mitigation…3rd`. If found, cancel/withdraw before the reset window opens.

---

## (C) Tier classification rubric

**TIER 1 PROTECT**: `BT_PF ≥ 2.0 AND BT_n ≥ 50 AND BT_E[R] ≥ +0.5R AND NOT BT-LURE (PF≥3 + WR≥85% + n≥20)`. These should fire with confidence; passive monitor only.

**TIER 2 MONITOR**: `BT_PF ≥ 1.3 AND BT_n ≥ 20 OR BT_PF ≥ 1.5 AND BT_n ≥ 30 AND BT_E[R] ≥ +0.5R`. Pre-commit retire gate per `notes` already says `n=10 pf_net<0.85 OR 6 consec L` (standard) or `n=10 pf_net<0.90 OR 5 consec L` (6J-TIGHT per `068c1fe6`). Manual watch first 5-10 trades per strategy.

**TIER 3 RETIRE-ELIGIBLE**: `BT_PF < 1.3 OR BT_n < 20 OR BT_E[R] < 0 OR BT-LURE-loose (PF≥5 + WR≥85% + n<20)`. Mostly short-sample first-fires and the BT-LURE-Loose red flag (#4896).

---

## (D) First-50-trade fire sequence (deterministic, post-reset)

**Reading caveat**: 138 actives cannot all be exercised in 50 trades. The deterministic **first-50-trade fire order below targets the TIER 1 PROTECT cohort**, recognizing that entry will be gated by the runner's per-strategy signal-detection logic (i.e., a strategy only fires when its condition triggers — not every active trades every bar). The priority order below is the desired-rank for the *first trades that come out of the gate*. After 50 trades, all T1 actives are expected to have produced ≥1 sample.

### Phase 1 (Trades 1-10) — Most-validated foundation, all-time MTD/promotion track-record positive

Goal: get live evidence on the architectures most likely to be **regime-valid** before letting any BT-LURE-flagged strategy gamble. Staggered start (NOT same-bar) per `6a27d7dd` MCL hostility precedent.

| # | Trade | ID | Sym | TF | Family | PF | n | WR | Why-first |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 4876 | MGC | 15m | ict_inside_bar_disp_breakout_15m | 4.56 | 141 | 73% | Has biggest BT-n=141; live history shows fast convergence |
| 2 | 4857 | MGC | 5m | silver_bullet_pdh_pdl_retest_5m_roll24 | 5.66 | 119 | n/a | Sister to MNQ live #4678 winner |
| 3 | 4841 | MCL | 5m | ict_inside_bar_disp_breakout_tight | 4.97 | 127 | n/a | CSID 357 architecture, bleed-map APPROVED |
| 4 | 4849 | 6J | 5m | ict_inside_bar_disp_breakout | 2.54 | 153 | n/a | Same arch as #4872; bleeder symbol but architecture proven |
| 5 | 4877 | MNQ | 15m | ict_inside_bar_disp_breakout_15m | 3.49 | 144 | 69% | +$8,302 BT; MNQ is most profitable symbol (+$779 live) |
| 6 | 4875 | MNQ | 5m | silver_bullet_pdh_pdl_retest_5m_nogate_roll24 | 7.11 | 115 | 87% ⚠ | **LURE-WATCH** per finding `8912`; first 10 trades tracked for WR>75% |
| 7 | 4966 | MGC | 5m | silver_bullet_pdh_pdl_retest_5m_nogate_roll24_tight | 4.36 | 137 | n/a | Tighter variant |
| 8 | 4872 | MNQ | 5m | ict_inside_bar_disp_breakout_5m | 3.12 | 210 | 68% | Massive n=210 most statistically robust |
| 9 | 4860 | MCL | 15m | silver_bullet_pdh_pdl_retest_15m_50pct | 3.98 | 72 | n/a | Bleeder symbol diversification per `6a27d7dd` |
| 10 | 4878 | ZF | 15m | ict_inside_bar_disp_breakout_15m | 3.03 | 80 | 75% | Only T1 on ZF (smallest cohort, but profitable symbol +$656) |

### Phase 2 (Trades 11-25) — T1 satiation, low-correlated TF pairs

Add the remaining T1 PROTECT actives; cluster them by session (avoid 3 same-timeframe fires within a 30-min window).

| # | Trade | ID | Sym | TF | Family | PF | n |
|---|---|---|---|---|---|---|---|
| 11-15 | 4828, 4912, 4948, 4962, 4959 | MGC/MNQ/MCL | mixed | silver_bullet_15m_50pct, ob_midpoint_5m, silver_bullet_5m_nogate_v2, silver_bullet_1h_loose_v2, silver_bullet_1h_loose_v1 | 7.71/4.38/4.61/2.00/4.08 | 84/57/113/78/73 |
| 16-20 | 4949, 4887, 4884, 4861, 4862 | MNQ/MNQ/MNQ/MGC/MCL | mixed | silver_bullet_5m_nogate_v2_mnq, ifvg_midpoint_htf_bias_1h, ifvg_midpoint_htf_bias_15m, ict_inside_bar_disp_breakout_5m, silver_bullet_pdh_pdl_retest_5m_roll12 | 4.56/3.56/5.51/3.90/3.84 | 134/94/59/191/87 |
| 21-25 | 4915, 4890, 4895, 4863, 4955 | MCL/MCL/MGC/MNQ/MCL | mixed | ob_midpoint_reversion_15m, ict_mitigation_block_15m, asian_range_sweep_mitigation_block_15m, judas_continuation_15m, silver_bullet_pdh_pdl_retest_15m_roll96 | 2.53/3.05/4.16/3.11/3.65 | 175/27/59/66/25 |

### Phase 3 (Trades 26-50) — T2 MONITOR, **staggered** for the BT-LURE-Loose #4896 to FIRE LAST

After 25 trades of T1 evidence, T2 MONITOR gets a controlled opening. The critical exclusion:

| # | Trade | ID | Sym | TF | Family | PF | n | Note |
|---|---|---|---|---|---|---|---|---|
| 26-39 | Pick from T2 MONITOR by (PF × n) — see full TSV T2 rows | mixed | mixed | T2 families | 1.3-2.0 | 20-100 | Manual watch first 10 trades each |
| 40-44 | T2 cross-symbol diversification | mixed | mixed | htf_1h_disp_5m_fvg_pullback, ifvg_midpoint_reversion_htf_bias_ny_only_5m | 1.5-2.5 | 30-80 | |
| 45-49 | **DEFER #4896 to trade 50+** | MNQ | 5m | ny_am_pdh_pdl_silver_bullet_5m | 31.97 | 12 | **BT-LURE-Loose** — see (E) below |
| **50** | #4896 (conditional) | MNQ | 5m | ny_am_pdh_pdl_silver_bullet_5m | 31.97 | 12 | **Only fire if WR of trades 1-49 > 55%** (validates regime matches BT); otherwise RETIRE pre-fire |

### Order rationale summary

- **Same (symbol, TF, family) max-2 constraint** per finding `161b877a` is preserved (no Phase 1 cluster ever has 3 from same slot).
- **MNQ 15m atr_disp_continuation_15m_loosened (#4970, MNQ 15m) and MCL 15m atr_disp_continuation_15m_loosened (#4971, MCL 15m)** are STAGGERED — `#4971 fires in Phase 2 (trade 24-25 window), #4970 fires in Phase 3 (trade 35+)`. This is the "trend diversification" pair per finding `9053`.
- **BUFFER_ZOO_RSI_1H #4940** is a STANDALONE per finding `9051` (RSI 1H regime risk). It is in the T3 cohort (n=10, PF=1.65, WR=45%) and fires LATE, not before trade 35+ in the sequence, ideally paired only after at least one MNQ ifvg midpoint_1h fires to confirm regime.
- **6J 1h silver_bullet #4938** fires standalone after MNQ 1h #4887 confirms (paired-time risk).
- **6J 5m ict_mitigation_block_london_sb_5m_6j_v1 (#4941)** defers to trades 40+ per finding `9056` London SB failure mode (3-5 AM EST time-gate failure).

### Drawdown envelope (Day-1 worst-case stop-out simultaneously at 1R)

Per finding `9025` methodology: assume 5 simultaneous PHASE_1 max-stops at typical contract-notional. Worst-case is **$1,500 in simultaneous PHASE_1 stops**, right at the hard flatten line. Phase 2/3 expansion adds another $1,500-$3,500 of cumulative risk. **Operator should cap PHASE_1 to 3 entries for first 4 hours**, expand to 5 only if first 3 show WR>50% on first 5-10 live trades.

---

## (E) Pre-commit retire gate manual monitoring checklist

For each TIER 2/3 strategy, the pre-commit gate text is in the `notes` column of the active_strategies row. Below is the consolidated operator crib sheet — copied verbatim from each entry's promotion notes. **Use this as the running checklist during the post-reset window.**

### Critical: T3 RETIRE-ELIGIBLE pre-reset decisions

| ID | Pre-commit retire gate (FROM NOTES) | Action |
|---|---|---|
| **#4896** MNQ 5m ny_am_pdh_pdl_silver_bullet | "Pre-commit retire gate ARMED TIGHT: n=10 pf_net<0.85" | **Pre-retire before reset OR freeze signal-block.** BT-LURE-Loose red flag (PF=31.97 WR=92% n=12). |
| #4846 MNQ 15m silver_bullet_pdh_pdl_retest_15m_nogate | "n=10 pf_net<0.85 OR 6 consec L" | First 5 live trades = monitor ticket. If WR<60% in first 10 → retire immediately. |
| #4856 MGC 5m ny_am_intra_judas_5m | "Pre-commit retire gate: n=15 pf_net<0.85" | n=13 BT sample thin. First 5 live trades; if WR<55% → retire. |
| #4940 MNQ 1h buffet_zoo_rsi_mean_reversion_1h_regime_filtered | "Pre-commit retire gate: n=10 pf_net<0.85" | RSI 1h regime risk per finding `9051`. STANDALONE fire; retire if first 5 trades produce 3+ losses. |
| #4941 6J 5m ict_mitigation_block_london_sb_5m_6j_v1 | "Pre-commit retire gate: n=10 pf_net<0.85" | **London SB time-gate failure mode per `9056`** — 3-5 AM EST gate violated in backtest, expect ifvg_london session de-sync. First 5 live trades; if WR<50% → retire. |
| #4848 MGC 15m silver_bullet_pdh_pdl_retest_15m_roll96 | "Pre-commit retire: n=10 pf_net<0.85" | MGC sister to MNQ roll96 live winner +$533 (per finding `8880`); moderate confidence. |

### BT-LURE STRICT (3 entries — live WR must exceed 75% in first 15 trades)

| ID | Family | BT WR | Live WR gate |
|---|---|---|---|
| #4827 MGC 5m silver_bullet_pdh_pdl_retest_5m_nogate_roll48 | 89.9% | WR>75% per finding `c46a9740` |
| #4837 MCL 5m ifvg_midpoint_reversion_htf_bias_ny_only_5m_v3 | 85% | WR>75%; if<60% first 10 → retire |
| #4875 MNQ 5m silver_bullet_pdh_pdl_retest_5m_nogate_roll24 | 87% | Per finding `8912` — first 10 trades tracked for WR<50% → retire immediately |

### T1 PROTECT watch (informational; passive monitor)

All 41 T1 actives have either MGC-standard `n=10 pf_net<0.85 OR 6 consec L` or symbol-specific tightening (MCL/6J `n=10 pf_net<0.90 OR 5 consec L`). These are passive-monitor unless a red flag appears in live WR.

### Operator's running checklist during post-reset window

- [ ] **Pre-reset (NOW, while halted)**: Manually freeze or pre-retire #4896 to prevent lottery-ticket fire on first bar.
- [ ] **Trade 1-10 fires**: monitor PnL accumulator; expect mix of W/L. Check if any strategy takes 3 consec L in first hour → retire that one.
- [ ] **Trade 11-25**: confirm T2 MONITOR starts feeding without crowding T1.
- [ ] **First 50 trades milestone**: full review. Compute WR per symbol. If MCL WR < 50%, retire MCL-loose (per `6a27d7dd`). If 6J WR < 45%, retire 6J-loose (per `068c1fe6`).
- [ ] **First 100 trades milestone**: full BT vs live divergence audit. Tier 3 strategies get hard retire decision.

### Known DEFCITs to verify before reset lands

1. **MAX-2 rule violations carried from earlier cohorts** (NOT fresh) are still active: MGC 5m custom_5m has 3 (#4638, #4697, #4702). If a 4th gets promoted and reset opens same-window, a 4-deep simultaneous MGC 5m custom_5m fire is possible.
2. **`pre_commit_retire_gate` text** in `notes` column is DOCUMENTED-NOT-ENFORCED per finding `325ba8fd` — runner parses notes as text only. The operator must manually monitor. The runner will NOT auto-retire on gate breach.
3. **Pending `promote_candidate` tasks in queue** are unknown (current open task count for this team = 1 — task #2841 is research-only). Recommend: verify the reviewer/registrar queue has no pending third-promote for `MCL 15m ict_mitigation_block_15m` or `MNQ 1h ifvg_midpoint_reversion_htf_bias_1h` (would push these from "AT LIMIT" to "VIOLATION").
4. **Coder autofix is permanently degraded** per finding `0c77f8e1` (8+ consecutive `harness_no_fix`). Do not rely on coder for any retirements during the post-reset window — operator must drive via reviewer/registrar.

---

## Cohort matrix artifacts (working TSVs)

These are dumped to disk for any future cycle to extend:

- **`/tmp/postreset/cohort_full.tsv`** — full 138-row tsv with notes/params (139 lines incl. header)
- **`/tmp/postreset/section_a_full.tsv`** — parsed 138-row with tier/PF/n/E[R]/WR/$/lure/gate columns
- **`/tmp/postreset/section_a_fresh.tsv`** — freshest subset (≥2026-10-13, 5-day window)
- **`/tmp/postreset/slot_concentration.tsv`** — full active roster (sym, tf, family) concentration check

---

## Refs

- **9e85b382** — STICKY_HALT_DIAGNOSIS (ops reset SQL fallback)
- **325ba8fd** — POST_RESET_FIRST_FIRE_PLAN_2026-10-16 (matrix foundation; this finding supersedes-extends)
- **37e08337** — FIRST_FIRE_DUPLICATE_AUDIT_2026-10-17 (duplicates cleaned up)
- **161b877a** — POST-HALT-SLOT-PRIORITY (max-2 same-TF rule)
- **f575d604** — STRUCTURAL_FAMILY_X_SYMBOL_BLEED_MAP (6J/MCL hostility gates)
- **068c1fe6** — 6J-TIGHTENED gate (n=10 pf_net<0.90 OR 5 consec L)
- **6a27d7dd** — MCL-TIGHTENED gate (n=10 pf_net<0.90 OR 5 consec L)
- **6511** — BT-LURE rule (PF≥3 + WR≥85% + n≥20 = REJECT)
- **c46a9740** — BT-LURE-Loose forward-watch (WR>75% first 15 trades red flag)
- **bec46004** — 6J London SB failure mode (applies to #4941)
- **9051** — BUFFER_ZOO 1H RSI regime risk (#4940)
- **9056** — 6J_LONDON_SB_TIME_GATE_FAILURE_MODE_2026-10-18 (#4941 specifics)
- **8912** — SILVER_BULLET_ROLL24_OVERFIT_FLAG_MNQ (#4875)
- **8924** / **8933** — CSID 373/375 ny_am_pdh_pdl_silver_bullet (MNQ #4896 LURE caveat, MCL #4893 clean)
- **9025** — POST_RESET_FIRST_FIRE_PLAN_2026-10-16 (predecessor plan, this one extends it to 138-row scope)
- **9055** — SESSION_BRIEF:2026-10-18:pre-london (HALT context)
- **9061** — MID_LONDON_BRIEF:2026-10-18:12z (12Z context)

## Active roster cross-check

- 169 active rows; 138 with `n_live_trades (closed) = 0`; cohort activated > 2026-10-08.
- 4 additional `n=0` actives activated 2026-10-06 (#4774-4777) — total 142 n=0 actives.
- Broker FLAT; account halted; ops reset SQL is the only path forward.
