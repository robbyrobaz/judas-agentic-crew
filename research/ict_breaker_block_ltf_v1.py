"""
ICT Breaker Block (Failed OB Flip) - 5m/15m/1h.

Source distillation (2026-08-16):
  - YT:L9EbBZ7_znI (FXNX) "ICT Breaker Blocks: How to Profit from 'Failed' Order Blocks"
  - YT:OWnK0f2D8gQ (DK Futures) "Why Your Order Block Failed (And What To Trade Instead)"
  - findings 7574, 7305, 6384 (theoretical backing)

CANONICAL SETUP (per FXNX, confirmed by DK Futures):
  Phase 1 - LIQUIDITY SWEEP: a sharp move pierces a previous low (or high),
            triggering retail stops in a stop hunt. The sweep is REQUIRED;
            without it, this is a mitigation block (lower probability).
  Phase 2 - MARKET STRUCTURE SHIFT (MSS): violent displacement in the
            OPPOSITE direction that breaks the prior swing point and CLOSES
            well beyond the initial high (body close, not just wick).
  Phase 3 - BREAKER IDENTIFICATION: per DK Futures, this is the FIRST
            candle of the manipulation leg (the candle that INITIATED the
            sweep). If consecutive candles participate, the entire range
            is the breaker zone.
  Phase 4 - WAIT FOR CLOSE OUTSIDE: "Once price CLOSES outside of the
            breaker, we can then look to take an entry" (DK Futures).
            Combined with FXNX's "wait for retrace into the breaker zone".
  Phase 5 - ENTRY DIRECTION IS WITH THE MSS (critical): after sweep-down +
            MSS-up, ENTER LONG on the retrace down into the breaker (now
            flipped to support). After sweep-up + MSS-down, ENTER SHORT on
            the retrace up into the breaker (now flipped to resistance).
  Phase 6 - ENTRY PRICE: 50% of the REAL BODY (mean threshold rule).
            Wicks ignored.
  Phase 7 - STOP: beyond the sweep extreme (long: lowest low of sweep -
            buffer; short: highest high of sweep + buffer).

HTF BIAS FILTER (per task #2254): only fire in direction of HTF trend.
Implemented as rolling N-bar slope of close over htf_len bars.

WHY THIS DIFFERS FROM EXISTING STRATEGIES:
  - ict_mitigation_block_5m_v1: continuation pattern. ENTRY at the candle
    BEFORE the break (retrace). NO sweep required. Breaker is the REVERSE:
    TREND-SHIFT pattern. Requires sweep, MSS body-close, ENTRY at the FIRST
    candle of the sweep leg (flip polarity of that candle).
  - ob_midpoint_reversion_5m_loose_*: reversion to ANY OB midpoint. No
    sweep or MSS requirement. We require sweep + MSS body-close + close
    outside breaker.
  - ifvg_midpoint_reversion_htf_bias: FVG-based, not OB-based.
  - sweep_fade_5m: COUNTER-direction fade. We trade WITH the MSS.

EDGE HYPOTHESIS: The breaker block is the cleanest "failed OB" pattern
in ICT. The MSS body-close + retrace is the institutional footprint.
By waiting for close-OUTSIDE the breaker before entry, we confirm
institutions have flipped and aren't just whipsawing.

PREVIOUS ATTEMPT BUG (per task #2254 brief): "first implementation used
the wrong custom-runtime return convention; it was not a valid
architectural rejection" - fixed here. evaluate() returns
{"direction", "entry", "stop", "target"} per portfolio_runtime spec.

SECOND ATTEMPT BUG (cycle 2026-08-16, this version): entry direction
was INVERTED (short after sweep-down + MSS-up). The MSS is the
DIRECTIONAL move; entry goes WITH the MSS, not against it. Fixed.
"""
import numpy as np
import pandas as pd


def _atr(bars, period=14):
    h = bars["high"].astype(float)
    l = bars["low"].astype(float)
    c = bars["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def evaluate(bars, params):
    pivot_len = int(params.get("pivot_len", 5))
    atr_period = int(params.get("atr_period", 14))
    htf_len = int(params.get("htf_len", 50))
    sweep_min_atr = float(params.get("sweep_min_atr", 0.10))
    mss_body_close_min = float(params.get("mss_body_close_min", 0.40))
    mss_range_atr = float(params.get("mss_range_atr", 0.8))
    sweep_lookback = int(params.get("sweep_lookback", 20))
    breaker_max_age = int(params.get("breaker_max_age", 12))
    target_r = float(params.get("target_r", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.20))
    require_htf_bias = bool(params.get("require_htf_bias", True))

    n = len(bars)
    min_bars = max(pivot_len * 4, htf_len + 5, atr_period + 5, 60)
    if n < min_bars:
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    open_ = bars["open"].astype(float)
    atr = _atr(bars, period=atr_period)

    i = n - 1
    if pd.isna(atr.iloc[i]) or atr.iloc[i] <= 0:
        return None
    cur_atr = float(atr.iloc[i])

    # HTF bias (rolling slope of close over htf_len bars)
    htf_window = close.iloc[-htf_len:]
    htf_slope = float(htf_window.iloc[-1] - htf_window.iloc[0])
    htf_bias_long = htf_slope > 0
    htf_bias_short = htf_slope < 0
    if require_htf_bias and not (htf_bias_long or htf_bias_short):
        return None

    cur_close = float(close.iloc[i])
    cur_high = float(high.iloc[i])
    cur_low = float(low.iloc[i])

    direction = None
    entry = None
    stop = None

    # ---- LONG setup (HTF bullish + sweep DOWN + MSS UP + retrace) ----
    # Per FXNX/DK Futures: bullish OB at swing low -> sweep down -> MSS up
    # closes above OB top -> wait for retrace down into breaker zone ->
    # ENTER LONG (with MSS) -> stop BELOW sweep extreme.
    if not require_htf_bias or htf_bias_long:
        for j in range(i - 1, max(0, i - sweep_lookback) - 1, -1):
            prior = low.iloc[max(0, j - pivot_len):j]
            if len(prior) < pivot_len:
                continue
            swing_low = float(prior.min())
            sweep_idx = None
            for k in range(j + 1, min(n, i + 1)):
                if float(low.iloc[k]) < swing_low - sweep_min_atr * cur_atr:
                    sweep_idx = k
                    break
            if sweep_idx is None:
                continue
            br_lo = float(low.iloc[sweep_idx])
            br_hi = float(high.iloc[sweep_idx])
            br_op = float(open_.iloc[sweep_idx])
            br_cl = float(close.iloc[sweep_idx])
            # widen breaker to consecutive same-direction (bearish) candles
            k = sweep_idx + 1
            while k < min(n, i + 1) and float(close.iloc[k]) < float(open_.iloc[k]):
                br_lo = min(br_lo, float(low.iloc[k]))
                br_hi = max(br_hi, float(high.iloc[k]))
                k += 1
            sweep_extreme_low = br_lo
            # MSS UP: bullish displacement candle closes ABOVE breaker high
            mss_idx = None
            for m in range(k, min(n, i + 1)):
                rng_m = float(high.iloc[m]) - float(low.iloc[m])
                if rng_m <= 0:
                    continue
                body_m = abs(float(close.iloc[m]) - float(open_.iloc[m]))
                if body_m / rng_m < mss_body_close_min:
                    continue
                if rng_m < mss_range_atr * cur_atr:
                    continue
                if float(close.iloc[m]) > br_hi:
                    mss_idx = m
                    break
            if mss_idx is None:
                continue
            breaker_age = i - mss_idx
            if breaker_age > breaker_max_age:
                continue
            # retrace check: current bar has wicked into / touched the breaker
            # zone from above (cur_low reached down into the breaker range)
            in_zone = cur_low <= br_hi and cur_close >= br_lo
            if not in_zone:
                continue
            body_mid = 0.5 * (br_op + br_cl)
            entry_long = max(body_mid, br_lo)
            stop_long = sweep_extreme_low - stop_buf_atr * cur_atr
            risk = stop_long - entry_long  # risk > 0 since stop is below entry
            if risk <= 0 or risk > 4.0 * cur_atr:
                continue
            direction = "long"
            entry = entry_long
            stop = stop_long
            break

    # ---- SHORT setup (HTF bearish + sweep UP + MSS DOWN + retrace) ----
    # Mirror: bearish OB at swing high -> sweep up -> MSS down closes
    # below OB low -> wait for retrace up into breaker zone -> ENTER SHORT
    # -> stop ABOVE sweep extreme.
    if direction is None and (not require_htf_bias or htf_bias_short):
        for j in range(i - 1, max(0, i - sweep_lookback) - 1, -1):
            prior = high.iloc[max(0, j - pivot_len):j]
            if len(prior) < pivot_len:
                continue
            swing_high = float(prior.max())
            sweep_idx = None
            for k in range(j + 1, min(n, i + 1)):
                if float(high.iloc[k]) > swing_high + sweep_min_atr * cur_atr:
                    sweep_idx = k
                    break
            if sweep_idx is None:
                continue
            br_hi = float(high.iloc[sweep_idx])
            br_lo = float(low.iloc[sweep_idx])
            br_op = float(open_.iloc[sweep_idx])
            br_cl = float(close.iloc[sweep_idx])
            k = sweep_idx + 1
            while k < min(n, i + 1) and float(close.iloc[k]) > float(open_.iloc[k]):
                br_hi = max(br_hi, float(high.iloc[k]))
                br_lo = min(br_lo, float(low.iloc[k]))
                k += 1
            sweep_extreme_high = br_hi
            # MSS DOWN: bearish displacement candle closes BELOW breaker low
            mss_idx = None
            for m in range(k, min(n, i + 1)):
                rng_m = float(high.iloc[m]) - float(low.iloc[m])
                if rng_m <= 0:
                    continue
                body_m = abs(float(close.iloc[m]) - float(open_.iloc[m]))
                if body_m / rng_m < mss_body_close_min:
                    continue
                if rng_m < mss_range_atr * cur_atr:
                    continue
                if float(close.iloc[m]) < br_lo:
                    mss_idx = m
                    break
            if mss_idx is None:
                continue
            breaker_age = i - mss_idx
            if breaker_age > breaker_max_age:
                continue
            in_zone = cur_high >= br_lo and cur_close <= br_hi
            if not in_zone:
                continue
            body_mid = 0.5 * (br_op + br_cl)
            entry_short = min(body_mid, br_hi)
            stop_short = sweep_extreme_high + stop_buf_atr * cur_atr
            risk = entry_short - stop_short  # risk > 0 since stop is above entry
            if risk <= 0 or risk > 4.0 * cur_atr:
                continue
            direction = "short"
            entry = entry_short
            stop = stop_short
            break

    if direction is None or entry is None or stop is None:
        return None
    risk = abs(entry - stop)
    target = entry + target_r * risk if direction == "long" else entry - target_r * risk
    return {"direction": direction, "entry": entry, "stop": stop, "target": target}