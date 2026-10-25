# RESET_CYCLE_RUNBOOK_2026-10-24 — first post-reset operator cycle (ONE PAGE)

**Author**: Researcher · **Task**: #2862 (operator) · **Account**: SimJudasFutures
**Supersedes nothing.** Extends, does not replace:
`325ba8fd` = `research/post_reset_first_fire_watch_plan_2026_10_18.md` (roster triage + first-50 desired order)
`498b01be` = POST_RESET_FIRST_FIRE_WATCH_PLAN checkpoints · `efbb62cbb` = BUFFER_ZOO #4940 checkpoints
`ac1a58b7` = halt diagnosis / guard integrity (the CUSHION_EXHAUSTED root cause)
`#2860` = shadow cap-allocation shortlist → `research/shadow_cap_allocation.csv` (15 slots, `usd_per_contract_day`)

**TWO NEW ITEMS this runbook adds** (present in no prior artifact):
- **(A) the runtime allocates the 10-contract cap ALPHABETICALLY, not by edge** → your curated order does NOT bind.
- **(C) the 262 stale working orders are still resting, the Orders read path is UP again, and our 10-contract cap is blind to them.**

---

## STEP 0 — BEFORE you touch anything: is the account actually READY?

Two independent blockers exist (finding 9123). **Both** must be clear or the reset day will look like a failure:

| # | Blocker | How to confirm | Expected-clean value |
|---|---|---|---|
| 0.1 | **CUSHION_EXHAUSTED guard halt** (`ac1a58b7`) | read `data/lucid_guard_state.json` | `halt_entries: false`, `cushion` > 100 |
| 0.2 | **Stale-order wedge / MaxPositionSize=4** | `get_nt_working_orders` (see STEP 1) | working orders = **0** (NOT "0 positions") |
| 0.3 | Account equity | NT account summary `cash` | cash == the post-reset base (e.g. 50000) |
| 0.4 | Ledger rebased? | `data/lucid_guard_ledger.json` `daily_close` | today's close == new base; `mll_floor` == base − 2000 |

> **Do NOT infer "ready" from "flat".** Flat = no risk. Ready = no risk AND guard clear AND order book empty.

---

## STEP 1 — THE WEDGE CHECK (NEW — do this FIRST, it gates everything)

**Why:** the account is FLAT but **262 working orders** are still resting at NT (verified live 2026-10-24T03:3xZ,
`research/cycle_20261024/task2862_nt_wedge_report.json`): ZF 18 / MNQ 79 / MGC 59 / MCL 62 / 6J 44,
total resting qty **428**, oldest 2026-09-11, newest 2026-10-05T22:03:50 (= the moment the NT session died).
**197 of them sit on the CURRENT contract month** (ZF 12-26 18, MGC 12-26 59, MNQ 12-26 76, 6J 12-26 44).

1. `get_nt_working_orders` → expect the count. **If it errors, the read path is down again** (it was hard-down on
   2026-10-23, finding 9137; it was UP when I probed 2026-10-24). Do not assume either state.
2. `get_nt_positions` → must be **flat: true** and `source: nt_live_positions_table` (NOT the sync copy — the frozen
   2026-10-05 cache is what produced the 4 phantom MCL orphan alerts).
3. If working orders > 0 → **the account CANNOT open a position.** NT counts working orders toward
   `MaxPositionSize=4` worst-case exposure and rejects new orders with *"Exceeds account's maximum position quantity"*.
4. **Clear the book first.** Paths, in order of preference:
   a. **NT8 GUI → Orders → Cancel All** on `SimJudasFutures` (operator, human). This is the only path that has ever
      worked; findings 9003 / 9123 / 9137 all record the programmatic cancels returning phantom success
      (`cancelled: 22, failed: 240` every cycle; `cancel_nt_order` returns `ok:true` and the order is still there).
   b. Only if the GUI is unavailable: `cancel_stale_nt_orders(dry_run=true)` → review → `dry_run=false`,
      then **re-poll and verify the count actually dropped**. Do not accept the tool's own "cancelled" count.
5. **The 1 naked market order** (`db813996f65449f89bce6edb7f2797d9`, ZF 12-26 BUY 1, `oco=""`, placed 2026-09-13)
   is the single most dangerous artifact: an entry with **no OCO and no protective stop**. Cancel it by hand
   even if a bulk cancel claims success.
6. **After clearing, re-verify**: working orders == 0 AND positions flat AND `daily_close` == today's equity.

**Expected-clean values:** `get_nt_positions` → `flat: true`, `open_positions: []` · `get_nt_working_orders` → `[]`
· `data/lucid_guard_state.json` → `nt_contracts: 0`, `day_pnl: 0.0`.

---

## STEP 2 — KNOW WHAT WILL ACTUALLY GET THE CAP (NEW — selection order)

**The curated order in `325ba8fd` §(D) and the `#2860` shortlist DO NOT BIND.** The runtime allocates the
10 contracts by **`ORDER BY symbol, strategy_family, version DESC`** (`src/strategy_registry.py:944-953`), and the
scan walks that list top-down placing as it goes (`src/portfolio_runtime.py:1923`, loop `:2018`, place `:2060`).
No PF, no E[R], no shadow $, no live edge is consulted anywhere in that comparison. **There is no `priority`
field in the codebase** (`params_json LIKE '%priority%'` → 0 of 175 actives).

So, on a busy bar, the cap goes to **6J first** (alphabetically first symbol; 22 actives), then MCL (47), MGC (45),
MNQ (44), ZF (17). Measured: **the first 20 contracts the runtime would ever place are all 6J; the first 10 are
100% 6J.** And 6J is the worst symbol on the book (**16W/48L, −$750.00**, 25% win rate), followed by MCL
(**44W/82L, −$1,775.00**) — the two losing symbols are the two that get the cap first.

**What to do about it (operator, before reset):**
- **Option 1 (recommended, no code):** set `params_json.priority` on the ~15 `#2860` shortlist rows and
  `priority = 0` on the rest — **but note this does nothing until the runtime is taught to read it** (Option 2).
- **Option 2 (one-line code change, risk-neutral, needs the coder):** at `src/portfolio_runtime.py:1923`, sort
  `active` by `-float(params.get("priority", 0))` (stable → unchanged behaviour for rows with no priority set).
  Same guards, same caps, same flat-by-16:40 rules; **only the order in which fires are offered to the gate changes.**
- **Option 3 (do NOT do):** sort by `metrics_json` expectancy/PF. 172/175 actives carry an `expectancy` key, but
  per finding 9133 the forward/synthetic evidence is a coin flip (PF 0.958 over 9,296 signals) and per 9126 the
  retro-fill envelope is contaminated. Sorting by BT expectancy allocates the cap by a number that has not been
  shown to predict a live fill.

**Until Option 2 exists, treat the `#2860` shortlist as a WATCH list, not an allocation list.**

---

## STEP 3 — BLAST-RADIUS RULES FOR THE FIRST SESSION (NEW)

| rule | why (measured) |
|---|---|
| **Cap the first session at 3 concurrent positions** | `max_open_positions=4` (`config.yaml` risk) × up to **5 contracts** each (`_eval_sized_qty`, `_EVAL_MAX_QTY=5`) = up to **20 contracts**, and the cap check counts *trades placed*, not *contracts* (`portfolio_runtime.py:1791` + `:2078`). The book has historically held **17 concurrent contracts against a 10-contract cap**, 22 times. |
| **Never let all 10 contracts be one symbol** | the cap is not dollar-normalised: 1 pip on 6J = **$1,250/contract**; 10 points on MNQ = $20/contract. Ten 6J contracts = **$12,500 per pip** = 10.4× the +$1,200 soft cap. |
| **Watch the loss caps, they are tiny while the cushion is small** | at cushion $67.37 the daily caps are **−$33.69 soft / −$40.42 hard** (`lucid_guard.daily_loss_caps`). One 6J tick on 5 contracts = $31.25. Two ticks = hard flatten + halt. Post-reset (cushion $2,000) they become −$700/−$900 and `risk_budget` = $100/trade. |
| **Stagger same-symbol/same-TF fires** | 8 exact-duplicate groups (same `custom_strategy_id` + symbol + TF) cover **17 actives** — e.g. csid 270 on ZF 5m has **three** rows (#4855, #4988, #4868) that fire the identical signal; MCL 5m silver-bullet has **7** actives in one bucket. |
| **Re-verify `priority` ordering actually took effect** | check the first 10 fills' `strategy_id`s against the `#2860` shortlist. If they are 6J-led, the sort is not in place. |

---

## STEP 4 — WATCH LOOP (existing checkpoints, unchanged)

- `325ba8fd` §(D) first-50 desired order + §(E) pre-commit retire gates — **now a DESIRED order, not the runtime's.**
- `498b01be` POST_RESET_FIRST_FIRE_WATCH_PLAN — first-fire watch points.
- `efbb62cbb` BUFFER_ZOO #4940 (MNQ 1h RSI regime) checkpoints — #4940 is Tier 3, fire late (trade 35+).
- `ac1a58b7` — guard integrity; if `halt_entries` is true, nothing trades and the day will look "broken" (it did,
  for 18 days).

**NEW per-cycle orphan check:** if a stale leg fills, the position has **no DB row**, so
`_check_position_protection` (`:1273`) cannot see it and cannot re-arm or flatten it; `_orphan_body` (`:1467`)
only **DETECTS AND QUEUES** a high-urgency trader task — it does **not** auto-flatten (the scan-loop comment at
`:1969-1971` claiming "they self-clear within one cycle" is **stale and wrong**). An orphan fill is therefore
**naked and unmanaged until a trader cycle picks up the task**. Poll `get_nt_positions` every scan, and treat any
position without a matching open `trades` row as an emergency.

---

## ARTIFACTS (read-only, reproducible)

- `research/cycle_20261024/task2862_nt_wedge_probe.py` + `task2862_nt_wedge_report.json` + `task2862_nt_working_orders.json`
- `research/cycle_20261024/task2862_arbitration_analysis.py` + `task2862_arbitration_report.json`
- `research/post_reset_first_fire_watch_plan_2026_10_18.md` · `research/shadow_cap_allocation.csv`

**Refs**: task #2862 · findings 9137 (Orders read down→UP), 9123 (two blockers), 9126 (BT envelope contaminated),
9133 (shadow PF 0.958), 9003 / 8629 / 8898 (MaxPositionSize=4 wedge), ac1a58b7 (halt root cause).
