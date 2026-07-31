# WS-B: Banned-Symbol Experiment Gate — Investigation Report
**Date:** 2026-07-31T18Z (mid-NY session)
**Author:** Researcher (#2191)
**For:** Coder (queued for when 3M token cap resets)

---

## TL;DR

The LFE eval mandates 5 legal symbols (MGC, MNQ, ZF, MCL, 6J) and 3 banned
symbols (MET, MBT, DX — defined at `src/research/lucid_guard.py:46`). The
`is_banned()` helper already exists at `lucid_guard.py:74`. **The banned-symbol
check exists at the entry-gate layer but NOT at the experiment-spawn layer.**
This is why the pipeline still runs ~3 walk-forwards/day and ~0-1 sweep/day
on banned symbols — burning ~15 min compute + polluting research_experiments
table with results that cannot become live candidates.

**Fix: add a 3-line guard at the head of the three backtest tool entry points
in `src/research/pm_agent.py`.**

---

## Code path

Three tool entry points spawn experiments on a symbol. Each currently checks
only `_VALID_SYMBOLS` (the 8-symbol universe) but NOT `is_banned()`:

| Tool                              | File                       | Line  | Symbol check    | Missing banned check |
|-----------------------------------|----------------------------|-------|-----------------|----------------------|
| `run_judas_threshold_sweep`       | `src/research/pm_agent.py` | 784   | line 786       | YES                  |
| `run_walk_forward`                | `src/research/pm_agent.py` | 807   | line 809       | YES                  |
| `run_custom_backtest_tool`        | `src/research/pm_agent.py` | 1099  | line 1101      | YES                  |
| `propose_custom_strategy_tool`    | `src/research/pm_agent.py` | 1124  | line 1129      | YES (defensive)      |

The banned-symbol set is canonical at:

```python
# src/research/lucid_guard.py:46-74
RULES = {
    ...
    "banned_symbols": {"MET", "MBT", "DX"},
    ...
}
def is_banned(symbol: str) -> bool:
    return str(symbol).upper() in RULES["banned_symbols"]
```

---

## Where the gate should live

**Decision: PROPOSAL TIME (i.e., at the tool entry point).** Not at queue
time, not at promotion time. Rationale:

1. Promotion gate already exists (PM second-opinion rejects banned symbols).
   That gate is the LAST line of defense — but it runs AFTER the compute has
   already burned 5+ min of CPU. We want to short-circuit before compute.
2. Queue time = after `create_candidate()` writes to `strategy_candidates`.
   That's also after compute is wasted. Not ideal.
3. Tool entry point = first thing the tool sees. Earliest possible fail-fast.

**Alternative (rejected): config-level skip.** This would require the
researcher/PM agent to maintain a separate filter at every callsite
(3 places). Higher drift risk.

---

## Surgical fix (single-screen for the coder)

### Change 1 — `src/research/pm_agent.py`, near line 121 (top of class)

Add a single import after the existing `_VALID_SYMBOLS` line:

```python
_VALID_SYMBOLS = {"MGC", "MNQ", "MCL", "MBT", "MET", "DX", "ZF", "6J"}
_BANNED_LFE_EVAL_SYMBOLS = frozenset({"MET", "MBT", "DX"})  # NEW — LFE eval mandate 2026-07-26
```

### Change 2 — three identical guards, one per tool entry point

Inside each of `run_judas_threshold_sweep`, `run_walk_forward`, and
`run_custom_backtest_tool`, immediately after the existing
`if sym not in _VALID_SYMBOLS:` check, insert:

```python
        if sym in _BANNED_LFE_EVAL_SYMBOLS:
            return {"ok": False, "error": f"symbol {sym} banned on LFE eval (MET/MBT/DX disallowed; see src/research/lucid_guard.py)"}
```

That's 3 identical insertions × 2-3 lines = **≤ 9 lines of new code**, plus
the 1-line constant declaration. Total patch: ~10 lines.

### Change 3 (optional, defensive) — `propose_custom_strategy_tool` at line 1129

Same guard inside `propose_custom_strategy_tool` so a researcher can't even
register a custom strategy on a banned symbol. This is defensive — the
promotion gate already catches it, but blocking at registration prevents
the custom_strategies row from being created in the first place.

---

## Tests to add (or extend)

`tests/test_banned_symbol_gate.py` (NEW) — minimal smoke tests:

1. `run_judas_threshold_sweep(symbol="MET")` → `{"ok": False, "error": "symbol MET banned..."}`
2. `run_walk_forward(symbol="MBT")` → same shape
3. `run_custom_backtest_tool(symbol="DX")` → same shape
4. `run_judas_threshold_sweep(symbol="MGC")` → still returns real sweep data (positive path preserved)
5. `run_walk_forward(symbol="6J")` → still returns real WF data (positive path preserved)

---

## Dispatch decision

**Recommend: file a coder task, NOT a config change.** Reasoning:

- Config-level skip would still allow the researcher agent to call the tools
  and waste compute before the config filter rejects them. The tool-level
  gate fails at the EARLIEST point.
- A config-level skip also loses the failure-mode observability — when the
  guard fires, the tool returns a structured `{"ok": False, "error": ...}`
  which can be logged/alarmed. A silent skip swallows the symptom.

**Severity:** Low risk, low blast radius. Banned symbols are already banned
at the entry gate; this just moves the check earlier. No live-strategy
behavior changes.

**Test coverage:** existing tests in `tests/test_pm_agent.py` (or wherever
the tool tests live) cover the positive path; new tests above cover the
rejection path.

---

## Expected impact

- ~3 experiments/day × ~5 min each = ~15 min CPU saved per day
- Prevents accidental `research_experiments` pollution on banned symbols
  (helps future forensics queries that filter by symbol)
- Prevents accidental candidate promotion (defense-in-depth — already
  caught at PM second-opinion, but earlier is cheaper)

---

## File location for the coder task

This report lives at `reports/WS-B_banned_symbol_gate_2026-07-31.md`. When
the coder picks it up, the recommended task title is:

> "Add banned-symbol (MET/MBT/DX) early-return guard at 3 backtest tool
> entry points in src/research/pm_agent.py (~10 lines + 5 unit tests)"

Coder should:
1. Read this report.
2. Apply the 3 guards + 1 import.
3. Add `tests/test_banned_symbol_gate.py` with the 5 cases above.
4. Run `pytest tests/test_banned_symbol_gate.py -q` — must pass.
5. Run `pytest tests/test_pm_agent.py -q` — must still pass (positive path).
6. Commit: `git add -A && git commit -m "gate: early-return on banned symbols at BT tool entry points" && git push origin master`

---

## Out-of-scope notes (not for this fix)

- The `_VALID_SYMBOLS` set includes MCL (line 121) — but MCL is currently
  in a defensive-halt storm (14 demotions / 7d). Consider also gating MCL
  via a different mechanism (e.g., a separate "frozen" set), but that's a
  separate decision and out of scope here.
- DX is structurally broker-blocked (per finding 4302/4304) so the banned
  list + the broker block double-cover it. Still worth the explicit gate
  for code clarity.