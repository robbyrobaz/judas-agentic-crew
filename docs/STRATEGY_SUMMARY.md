# Strategy evidence summary

Snapshot date: **2026-09-02**. Source: local `judas_crew.db` registry and trade
ledger. This is a dated Git snapshot; the database remains the runtime source
of truth.

## Executive summary

The system has recorded **8,281 experiments**, evaluated **6,908 candidates**,
and retained **25 active configurations** from **4,445 registry versions**. Of
the candidates, 1,901 were promoted or promoted through a modification and
5,007 were rejected.

The best repeated result is the broader **ATR/displacement-continuation
architecture**, not one magic parameter set. It produced positive expectancy
across 6J, MGC, MNQ, MCL, and ZF and survived several walk-forward checks. The
next most credible families are HTF-biased iFVG reversion and moderate-PF
rolling PDH/PDL retests.

## Evidence tiers

### Tier A — strongest research evidence

| Configuration | Recorded evidence | Why it stands out |
|---|---:|---|
| 6J 5m ATR displacement, 1.3× | n=130, PF=2.78, E[R]=+0.60 | Largest 6J sample; sister variants and cross-symbol checks agree |
| 6J 5m strict ATR displacement, 1.8× | n=48, PF=2.47, E[R]=+0.51 | Five walk-forward windows all PF≥1.56 |
| MGC 5m ATR displacement, ATR-10 | n=79, PF=1.93, E[R]=+0.48 | Three positive walk-forward windows: 2.17/2.05/1.78 |
| MGC 15m strict ATR displacement | n=61, PF=2.92, E[R]=+0.56 | Five positive walk-forward windows; cross-symbol confirmation |
| ZF 15m ATR displacement, 1.3× | n=87, PF=2.14, E[R]=+0.44 | Three profitable walk-forward windows |
| ZF 15m strict ATR displacement | n=61, PF=2.31, E[R]=+0.54 | Five walk-forward windows PF 1.39–5.08 |

### Tier B — promising, needs more sim evidence

- MGC 15m iFVG/HTF-100: n=68, recorded capped PF=2.50, E[R]=+1.03.
- MGC 15m ATR displacement: n=52, PF=2.28, E[R]=+0.62.
- MGC 15m long-only ATR displacement: n=64, PF=2.30, E[R]=+0.41.
- MGC 15m range-cycle displacement: n=27, PF=1.52; walk-forward evidence is
  positive but only six test trades, so confidence remains limited.
- MGC 5m ATR displacement at 1.5R: n=99, PF=2.37, E[R]=+0.46.
- MGC/MNQ 5m short-only ATR displacement: n=57/66, PF=2.35/2.39 on a short
  ten-day regime window. Treat as regime-specific until it persists.
- MNQ 15m iFVG/HTF-100: n=100, PF=2.08, E[R]=+0.53.
- MNQ 15m Silver Bullet roll-96: n=37, PF=1.82, E[R]=+0.95.
- MNQ 5m Silver Bullet roll-12: n=71, PF=1.90, E[R]=+0.79; also positive at
  the alternate 1.0R target.
- MNQ 5m CISD/FVG: n=30, PF=1.84, E[R]=+0.42.

### Overfit-watch / incomplete evidence

- Recorded PF above 5 or win rate above 80% appears in several Silver Bullet
  variants. These are active experiments, not established edges.
- Active rows 4577, 4578, and 4579 have empty `metrics_json` after parameter
  normalization. Their underlying custom-strategy records exist, but the
  active-row evidence is incomplete.
- Several old results lack the current realistic-cost stamp. They should be
  rerun under `v1_realistic_micros` before real-money consideration.
- 6J and ZF legacy dollar fields used incorrect or normalized denominations in
  some experiments. PF and expectancy are more useful than those dollar totals.

## Current active registry — all 25 configurations

Metrics are copied from each active row. `—` means the row did not retain
comparable metrics.

| ID | Symbol | TF | Strategy | n | PF | E[R] | Evidence note |
|---:|---|---|---|---:|---:|---:|---|
| 4583 | 6J | 5m | ATR displacement 1.5×, 1.5R | 89 | 2.55 | +0.54 | Cross-symbol validated |
| 4597 | 6J | 5m | ATR displacement 1.3×, 1.5R | 130 | 2.78 | +0.60 | Broader-signal sibling |
| 4606 | 6J | 5m | Strict ATR displacement 1.8× | 48 | 2.47 | +0.51 | Five positive WF windows |
| 4566 | MGC | 15m | iFVG midpoint reversion, HTF-100 | 68 | 2.50 capped | +1.03 | Raw PF 5.11; monitor |
| 4567 | MGC | 15m | ATR displacement, 2R | 52 | 2.28 | +0.62 | 252-day sample |
| 4593 | MGC | 15m | Silver Bullet PDH/PDL roll-72 | 38 | 10.43 | +1.61 | Overfit-watch |
| 4601 | MGC | 15m | Long-only ATR displacement, 1.5R | 64 | 2.30 | +0.41 | Cross-symbol envelope |
| 4602 | MGC | 15m | Range-cycle displacement | 27 | 1.52 | +0.30 | Thin WF test sample |
| 4605 | MGC | 15m | Strict ATR displacement 1.8× | 61 | 2.92 | +0.56 | Five positive WF windows |
| 4569 | MGC | 5m | ATR displacement ATR-10, 2R | 79 | 1.93 | +0.48 | Three positive WF windows |
| 4579 | MGC | 5m | Silver Bullet PDH/PDL roll-24 | — | — | — | Active metrics missing |
| 4587 | MGC | 5m | ATR displacement, 1.5R | 99 | 2.37 | +0.46 | 180-day sample |
| 4590 | MGC | 5m | Silver Bullet PDH/PDL roll-48 | 39 | 9.78 | +1.68 | Overfit-watch |
| 4604 | MGC | 5m | Short-only ATR displacement | 57 | 2.35 | +0.54 | Ten-day regime sample |
| 4577 | MNQ | 15m | Silver Bullet PDH/PDL roll-24 | — | — | — | Active metrics missing |
| 4595 | MNQ | 15m | Silver Bullet PDH/PDL roll-72 | 37 | 5.33 | +1.58 | Overfit-watch |
| 4596 | MNQ | 15m | iFVG midpoint reversion, HTF-100 | 100 | 2.08 | +0.53 | Largest MNQ 15m sample |
| 4599 | MNQ | 15m | Silver Bullet PDH/PDL roll-96 | 37 | 1.82 | +0.95 | Safer PF band |
| 4565 | MNQ | 5m | HTF-biased Silver Bullet | 25 | 2.50 capped | +1.64 | 88% WR; overfit-watch |
| 4578 | MNQ | 5m | Silver Bullet PDH/PDL roll-48 | — | — | — | Active metrics missing |
| 4582 | MNQ | 5m | Silver Bullet PDH/PDL roll-12 | 71 | 1.90 | +0.79 | Alternate-target validation |
| 4586 | MNQ | 5m | Three-candle CISD/FVG | 30 | 1.84 | +0.42 | Distinct architecture |
| 4603 | MNQ | 5m | Short-only ATR displacement | 66 | 2.39 | +0.48 | Ten-day regime sample |
| 4591 | ZF | 15m | ATR displacement 1.3× | 87 | 2.14 | +0.44 | Three positive WF windows |
| 4607 | ZF | 15m | Strict ATR displacement 1.8× | 61 | 2.31 | +0.54 | Five positive WF windows |

## SimJudasFutures evidence so far

At this snapshot, the new sim era has **15 closed ledger rows for +$472.50**.
Six strategies contributed attributed trades; three additional rows were
`nt_ledger_gap` reconciliation records.

| Active strategy | Closed | Sim P&L |
|---|---:|---:|
| MGC Silver Bullet roll-24 | 2 | +$202.00 |
| MGC ATR displacement ATR-10 | 1 | +$150.00 |
| MNQ Silver Bullet roll-12 | 1 | +$129.50 |
| MGC ATR displacement 1.5R | 1 | +$113.00 |
| MNQ short-only ATR displacement | 1 | +$23.50 |
| MNQ 15m iFVG/HTF-100 | 6 | −$2.00 |
| Unattributed `nt_ledger_gap` rows | 3 | −$143.50 |

This sample is far too small to validate a strategy. It is included to keep the
era boundary honest and to prevent historical Lucid or older sim trades from
being blended into current results.

## Interpretation rules

1. Prefer walk-forward stability and cross-symbol agreement over headline PF.
2. Treat PF > 5, win rate > 80%, tiny samples, or zero-trade windows as warning
   signs rather than proof.
3. Require current cost-model output before promotion evidence is considered
   portable to real-money trading.
4. Use the active strategy ID when attributing trades; names and families are
   reused across versions.
5. Retire weak sim arms according to their recorded gates. An empty slot is
   preferable to a persistent net loser.

## Reproduce the current inventory

```bash
sqlite3 judas_crew.db <<'SQL'
SELECT id, symbol,
       json_extract(params_json, '$.timeframe') AS timeframe,
       json_extract(params_json, '$.strategy_name') AS strategy,
       metrics_json
FROM active_strategies
WHERE state = 'active'
ORDER BY symbol, timeframe, id;
SQL
```

The local dashboard exposes the same registry and current-era trade attribution
without requiring direct database access.
