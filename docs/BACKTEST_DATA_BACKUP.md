# Backtest data backup — 2026-09-02

This private backup captures the datasets and research evidence used by Judas
Crew without copying credentials, logs, order-state files, or other transient
runtime state.

The prepared backup is stored locally at:

`/home/rob/judas-backtest-backup-2026-09-02`

## Contents

| Archive | Contents | SHA-256 |
|---|---|---|
| `judas-crew-market-bars-2026-09-02.tar.gz` | Crew multi-timeframe Parquet bar cache for 6J, DX, MBT, MCL, MET, MGC, MNQ, and ZF | `e7a4005f88e4d7e98cea717f814fac604317d8ee0f0193a303856b717784bba6` |
| `judas-crew-registry-2026-09-02.db` | Consistent SQLite backup of experiments, candidates, strategies, and trades | `a2a96fcff0c5862ce5076870653415127f23c1d93944d872c16996ff6966cfdf` |
| `judas-crew-research-results-2026-09-02.tar.gz` | Generated backtest CSV/JSON evidence and strategy artifacts from `outputs/research` | `6331552bb5cda5f5d0dacf43b806de30aa9a8f4ed487875f1b0582af3430e0db` |
| `judas-crew-research-sources-and-knowledge-2026-09-02.tar.gz` | Research source files, knowledge base, and strategy summary | `065d3ec7498db80354da5db0278d24cc35cb1f16803ac98757e853403b2c1255` |
| `judas-futures-workshop-backtest-baselines-2026-09-02.tar.gz` | Workshop bar caches and baseline/result CSVs used to seed the crew | `e441ed9d4b872d11d3b4ee877bc98afe2c90e3943c81ac7acfbd8eab62c3acec` |

The research-results archive intentionally excludes `.research.lock` and
`_runtime_ledger.json`; those are coordination/runtime files, not backtest
evidence. `.env`, credentials, application logs, and live order-state files are
excluded from every archive.

## Google Drive status

Uploaded to the private Google Drive folder
[`Judas Crew Backtest Data — 2026-09-02`](https://drive.google.com/drive/folders/1FEHpb1cnxKN9l37MzbySLXEI8y1xNTn5).
Post-upload verification reported five matching files and zero differences
from the local checksum-verified backup.
