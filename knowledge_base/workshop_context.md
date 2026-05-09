# Workshop Context

This repo should learn from the proven pieces in the sibling
`judas-futures-workshop` repo without importing code from it directly.

## What already proved useful there

- The strongest research result was not "more ICT detail" on 5m. It was that
  **1H bars carry the edge** while 5m is mostly noise.
- On NQ 1H, many plain strategies were profitable. On MGC 1H, the same effect
  appeared, though with a smaller sample.
- The original 5m Judas logic was a loser in sample. The current hypothesis is
  that Judas logic may still work if slowed down to 1H bars.

## What implementation patterns already worked

- For **market structure analysis**, the workshop used `ContFuture` data so the
  front month rolls cleanly for historical analysis.
- For **order placement**, the workshop resolved an explicit front-month
  `Future`. Continuous contracts are for analysis, not for transmitting orders.
- Session logic was handled with timezone-aware windows using local exchange
  timezones and UTC conversion. This avoids DST mistakes.
- Fill reconciliation mattered because short-lived one-shot processes do not
  stay alive long enough to observe all IBKR fill events in real time.

## What this crew should remember

- Treat 1H as the default operating timeframe unless new evidence says
  otherwise.
- Prefer deterministic tools for data, contract resolution, and execution.
- Use the workshop as a source of validated ideas: contract handling, session
  gating, and post-trade reconciliation patterns are worth copying.
- Do not re-argue whether 5m Judas is good. The knowledge base already says the
  prior evidence was poor.
