# ICT Judas Swing — Concept Reference

## What is a Judas Swing?

A Judas Swing is a deliberate false move engineered by large participants to sweep retail
liquidity (stop orders clustered beyond obvious swing highs/lows) before the real directional
move unfolds in the opposite direction. The name comes from the biblical betrayal — the move
appears to break out, but it is a trap.

The structure is always the same:
1. Price approaches a well-defined liquidity pool (prior session high/low, equal highs/lows)
2. Price sweeps through the level, triggering resting stop orders
3. Price reverses back through a structural pivot (CHoCH), signalling the true direction
4. The real move develops with displacement and momentum

## Session Liquidity Windows

ICT framework divides the trading day into three sessions, each with a specific role:

**Asia Session (20:00–02:00 ET)**
- Builds the liquidity pool through a relatively tight, low-volume range
- Equal highs and equal lows form as price oscillates without committing to direction
- Smart money uses this session to identify WHERE the stops are
- Do NOT trade sweeps during Asia — too low volume, too likely to whipsaw

**London Session (03:00–08:00 ET / 08:00–13:00 GMT)**
- Sweeps the Asia liquidity pool
- This is where the Judas Swing most commonly occurs
- High volume, strong displacement after the sweep
- Ideal setup: London sweeps Asia high/low, then reverses with a CHoCH

**New York Session (09:30–16:00 ET)**
- Confirms or extends the London move
- Second Judas opportunity: NY open can sweep the London range
- NY 09:30–10:30 ET is the PRIME Judas window — the sweep often occurs in the first
  30 minutes of the NY open. Do NOT apply a lockout here.
- After 11:00 ET, the setup quality degrades significantly

## Equal Highs and Equal Lows as Liquidity Magnets

Equal highs (price tags the same level twice) = stops resting just above those highs.
Equal lows (price touches the same level twice) = stops resting just below those lows.

These are the highest-probability sweep targets because retail traders place stops at
obvious structural levels. The more touches, the larger the pool.

Prior session high/low is the primary level used in this system (prior trading day H/L).
The system uses IBKR 1H bars to identify this level programmatically.

## Sweep Anatomy

A sweep consists of three components:

**1. Approach**
Price must approach the level methodically, not gap into it. A clean approach means
the market is orderly building towards the liquidity.

**2. The Wick/Sweep**
The actual penetration of the level. Two types:
- **Wick sweep (preferred)**: The candle's wick pokes through the level but the body
  closes back inside. This is the cleaner trap — it hunts the stops and immediately
  shows rejection. Highest probability setup.
- **Body sweep (lower quality)**: The candle closes beyond the level. More aggressive
  institutions needed to push through. Less precise reversal timing. This system
  downgrades body sweeps in the quality score.

The sweep must penetrate the level by at least 3 ticks (MGC: $0.30, MNQ: $0.75)
to count as a valid sweep. Minor wicks that barely tag the level are noise.

**3. Rejection and Return**
After the sweep, price must return back through the swept level. This is the first
signal that the move was a trap. We do NOT enter here — we wait for CHoCH.

## Displacement

Displacement is the impulsive reversal move that follows the sweep and CHoCH. It is
characterized by:
- Large candles relative to recent average (≥1.5× average candle size is the minimum)
- Strong directional bias (minimal overlap between candle bodies)
- Often accompanied by an FVG (Fair Value Gap)
- Volume spike (not always visible on IBKR data, but implied by price action)

**Displacement strength scoring:**
- 1.5–2.0×: Minimum acceptable (marginal setup)
- 2.0–3.0×: Good displacement (score upgrade)
- >3.0×: Exceptional — highest conviction setups

If the "reversal" after the sweep is slow and choppy, it is NOT displacement — it is
likely a continuation trap. SKIP these setups.

## Fair Value Gap (FVG)

A Fair Value Gap is a price imbalance created when a candle moves so fast that it
leaves a gap between the close of one bar and the open of the next.

On 1H bars, FVG detection is simplified: if there is a gap between the sweep bar's
close and the next bar's open, an FVG is present.

FVG significance:
- **Present**: Confirms displacement; smart money will likely return to fill this gap,
  but first the directional move develops
- **Absent**: Setup is still valid, but lower conviction

The FVG zone (gap_low to gap_high) is also a secondary entry refinement point, but
in this system we use the CHoCH bar close as the primary entry.

## CHoCH vs BOS (Change of Character vs Break of Structure)

**BOS (Break of Structure)**: Price makes a new swing high or low, extending the existing
trend. This is the trend CONFIRMING. Do NOT mistake a BOS for a CHoCH.

**CHoCH (Change of Character)**: After a sweep, price closes through a PRIOR swing pivot
in the OPPOSITE direction of the sweep. This signals that the sweep was indeed a trap
and the structure has shifted.

Example:
- Market has been trending up: higher highs, higher lows
- Sweep: Wick above a swing high (sweeps buy-side liquidity)
- CHoCH: Price then closes BELOW the most recent swing low → structure has shifted to bearish
- Entry: On the close of the CHoCH bar

The CHoCH must occur within 4 bars of the sweep (1H timeframe). Beyond 4 bars, the
setup is stale and the probability degrades.

## Entry Rules

**Entry signal**: Close of the CHoCH bar
**Entry type**: Market order placed at bar close (or limit at that close price)
**Bar timeframe**: 1H only — 5m timeframe produces -0.21R expectancy (negative edge)

Only enter on the MOST RECENT CHoCH. Never re-enter a stale setup from a prior hour.

## Stop Placement

Stop goes 2 ticks beyond the sweep extreme (the most extreme point the wick reached).

- Long setup: Stop = sweep_low_extreme - (2 × tick_size)
- Short setup: Stop = sweep_high_extreme + (2 × tick_size)

For MGC (tick = $0.10): 2 ticks = $0.20
For MNQ (tick = $0.25): 2 ticks = $0.50

The stop is placed beyond the WICK extreme, not the level that was swept. This ensures
the stop is beyond ALL the price action of the trap.

## Target

Primary target: 2R minimum (2× the dollar risk)
Secondary target: Next HTF (higher timeframe) liquidity pool (prior day's opposite H/L)

Example:
- Short setup, entry 3220.50, stop 3225.80, risk = $52.80 for 1 MGC contract
- 2R target = 3220.50 - (2 × 5.30 points) = 3209.90

Always set target ≥ 2R. Partial exit at 2R with trail to 1R is acceptable in Phase 2
(not implemented in Phase 1).

## "Best Setups Only" Criteria

ALL of the following must be met for a high-quality Judas setup (score ≥ 7/10):

1. **Wick sweep** (not body) — close must be back inside the swept level
2. **Displacement ≥ 1.5×** — CHoCH bar range must be ≥ 1.5× the 20-bar average
3. **Clear structural CHoCH** — a swing pivot must have been visible before the sweep
4. **ATR not contracted** — current 14-bar ATR must be ≥ 0.5× 20-bar average ATR
5. **Session validity** — London (03:00–08:00 ET) or NY (09:30–16:00 ET) session
6. **CHoCH within 4 bars** — stale setups (CHoCH > 4 bars after sweep) are invalid

Missing any ONE of these criteria → maximum score drops below 7 → RiskGuardian skips.

## Common Mistakes to Avoid

- **Entering on the sweep, not the CHoCH**: The sweep is not an entry signal. Wait for
  structural confirmation.
- **Trading during Asia session**: Volume too low, sweeps whipsaw.
- **Body sweep without displacement**: May be a real breakout, not a Judas trap.
- **Stale CHoCH**: If the CHoCH bar is not the most recent bar, the setup is over.
- **Ignoring ATR contraction**: Compressed ranges mean there is no energy for the reversal.
- **Taking marginal setups**: This system targets quality over quantity. One clean setup
  per week is better than five mediocre setups.
