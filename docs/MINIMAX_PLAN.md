# MiniMax plan & model wiring — what the crew runs on

_Last verified 2026-06-22. This is the LLM budget/model reference for the
judas-agentic-crew (and any sibling that uses the same `coding_plan` key)._

## The plan — MiniMax "Plus", ~$20/mo (API key name: `coding_plan`)

Tier specs as advertised:
- Full access to the MiniMax model family — **M3 / M2.7 / image / speech / music**.
- **Run 3–4 concurrent agents** (hard concurrency ceiling — keep overlapping
  timers at/under this; see Operational notes).
- 1M context window advertised "for long documents and large codebases".
- Native multimodal: **image and video input** (not wired into the crew today).
- **Text, image, speech, and music share ONE quota.**
- **~1.7B tokens / month of M3 usage** — the headline allotment.
- Billing is quota-based: every row in the usage export shows
  `Amount After Voucher 0.0000` (covered by the plan, not per-token billed).

## Verified API facts (tested against the live endpoint 2026-06-22)

| Thing | Value |
|---|---|
| Base URL | `https://api.minimax.io/v1` (config `crew.llm_base_url`) |
| Provider (litellm) | `openai` (config `crew.llm_provider`) |
| API key | env `MINIMAX_API_KEY` (in `.env`, world-readable — chmod 600 if you care) |
| **Callable M3 string** | **`minimax/MiniMax-M3`** ✅ (litellm) |
| Callable M2.7 string | `minimax/MiniMax-M2.7` ✅ |
| `minimax/MiniMax-M3-512k` | not callable (API rejects it) — `MiniMax-M3-512k` is just the billing-export label |
| Reasoning toggle | `extra_body={"reasoning_split": True}` (config `crew.llm_reasoning_split`) |

`minimax/MiniMax-M3` bills as `MiniMax-M3-512k` in the usage export — same model,
512k-context variant. (Plan advertises 1M context; the served M3 appears to be 512k
per the billing name — verify before relying on >512k.)

## How the crew is wired (as of 2026-06-22)

- **Model:** `minimax/MiniMax-M3` everywhere. The string is hardcoded as the
  `minimax_model` default in ~11 agent files (`pm_agent`, `operator_agent`,
  `researcher_agent`, `reviewer_agent`, `registrar_agent`, `trader_agent`,
  `agent_runner`, `autofix_harness`, `explore`, `live_review`, `memory_backend`)
  + `config.yaml: crew.llm_model`. **Not yet single-source** — a model swap means
  editing all of them (or `grep -rl 'minimax/MiniMax-' src/ | xargs sed -i`).
- **Live call seam:** every standard agent funnels through
  `pm_agent._call_llm` → `litellm.completion(...)`. That's where `max_tokens=8192`
  and `reasoning_split` (from config) are applied. `build_llm()` in
  `judas_agents.py` is only the doctor/health-check path — NOT the agents.
- **History:** M2.7 (lower cost) → M3 (smarter, 2026-06 swap) → back to M2.7 for
  token cost → **back to M3 once ReAct loops were bounded (2026-06-22).**

## Usage patterns & budget math (from the 2026-06-20/21 export)

Two cost components per model, per hour:
- **`cache-read (Text API)`** — re-reading the conversation history each turn.
  **This dominates** — e.g. one hour (2026-06-21 21:00–22:00 UTC) burned
  **9.23M** cache-read tokens on M2.7 vs ~1.16M completion. Output is tiny
  (5k–45k tokens/hr). The cost is *re-sent context*, not generation.
- **`chatcompletion-v2 (Text API)`** — actual input+output of each call.

**Sustainable rate:** 1.7B M3 tokens/mo ÷ 30 ÷ 24 ≈ **~2.36M tokens/hour average**.
Observed peak hour was **~11M tokens** (M2.7+M3 combined) — ~4–5× the sustainable
average in a single busy hour. So the budget is real and the crew _can_ blow it
during heavy cycles. **The lever is cache-read: bound the ReAct loops and keep
prompts/history small** (the 2026-06-18 "Cut token burn" work; turn/time budgets in
the `run_*.py` runners). Cumulative usage 2026-03-01→06-22 was ~6.3B tokens.

## Operational notes for future sessions

- **Concurrency ≤ 3–4 agents.** The crew runs specialists on separate systemd
  timers; avoid stacking many at the same minute (the crew + reviewer both fired at
  `*:00:00` — that's 2, fine; don't add more to that slot). Add `RandomizedDelaySec`
  if you grow the fleet.
- **Watch cache-read, not output**, when chasing token burn. A single unbounded
  loop or a huge tool result re-sent each turn (e.g. a 200K-char web_fetch) is the
  usual culprit.
- **To switch models:** edit `config.yaml: crew.llm_model` AND the ~11 hardcoded
  defaults (until someone centralizes it).
- **To dial reasoning:** `crew.llm_reasoning_split: true|false` (one flag, reverts
  instantly if M3 reasoning ever destabilizes tool-call formatting), and
  `max_tokens` in `pm_agent._call_llm`.
- **Kill switch:** `autofix.disable` file or `JUDAS_AUTOFIX_INHIBIT=1` for the
  self-modify loop; stop the systemd timers for the whole crew.
