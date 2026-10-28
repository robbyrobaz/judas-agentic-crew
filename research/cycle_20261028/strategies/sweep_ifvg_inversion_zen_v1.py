"""SWEEP -> MSS -> iFVG-INVERSION ENTRY (Zen liquidity sweep model), 5m.

SOURCE (2026-10-28 YouTube ingest)
-----------------------------------
YT:pmexdsocN9c  "Zen Trades - The Easiest ICT Liquidity Sweep Strategy in 2026"
    1. LIQUIDITY  : a prominent swing wick on 15m and above (session highs/lows
       count too).  "The market always moves from liquidity to liquidity."
    2. THE SWEEP  : price pierces that liquidity.
    3. HTF FVG TAP: after the sweep price taps inside a higher-timeframe FVG.
    4. ENTRY TRIGGER: an INVERSE FAIR VALUE GAP on a 30s-5m execution chart --
       the gap left by the leg away from the sweep gets FILLED, and the fill
       close is the entry.  Filled bullish gap -> short; filled bearish gap ->
       long ("we got a candle closure above this gap.  That's a go to sign" on
       the long, "we see that price inverses this fair value gap... we can take
       a short position" on the short).
    GEOMETRY      : entry on the inversion close; STOP = beyond the swing
       extreme that took the liquidity; TARGET = the OPPOSING liquidity.

YT:NRwsJ9NVH5c  "New TRADERS - Stop Fading the Trend! Real vs False Liquidity
    Sweeps" -- RULE 1 (EXPANSION FILTER): if a candle BODY closes beyond the
    level, the level is being broken (expansion), not swept (absorption), and
    the fade must be skipped.  RULE 2 (MICROSTRUCTURE SHIFT): after the sweep
    the execution chart must show a forceful displacement that BREAKS the last
    swing (MSS) -- if the staircase of higher highs/lows does not break, the
    "sweep" is a continuation and there is no trade.  RULE 3 (HTF MAGNET):
    only fade where the higher-timeframe draw-on-liquidity objective has been
    reached.

WHAT IS NEW HERE RELATIVE TO THE BOOK
-------------------------------------
The book HAS iFVG-midpoint reversion (csid=282/436/438), FVG pullback
(csid=424/425/429) and sweep families (csid=390/439/441) -- but NO
sweep-triggered iFVG-INVERSION entry with this geometry (entry = the gap-fill
close, stop = beyond the SWEEP extreme, target = the opposing liquidity pool).

Single timeframe, so the video's "HTF FVG tap" (step 3) is approximated by an
OPTIONAL filter (require_fvg_tap): the retracement after the sweep must
re-enter an FVG left by the leg INTO the sweep.  It is a switch, so the grid
can price it.

Stateless; numpy only.
"""
import numpy as np

TAIL = 400
DEFAULTS = {
    "liq_lb": 20,          # bars that define the swing liquidity level
    "sweep_lookback": 40,  # how far back a sweep may be
    "pierce_atr": 0.0,     # min pierce of the level, in ATR
    "mss_win": 8,          # bars allowed for the displacement after the sweep
    "mss_lb": 10,          # bars that define the swing level the MSS must break
    "fvg_min_atr": 0.10,   # min FVG gap width, in ATR
    "fvg_expiry": 12,      # bars an FVG stays valid
    "require_fvg_tap": 1,  # 1 = the retracement must re-enter a pre-sweep FVG
    "max_r": 2.0,          # target cap in R
    "min_r": 0.8,          # target floor in R (opposing liquidity too close)
    "buf_atr": 0.15,       # stop buffer beyond the sweep extreme
    "atr_period": 14,
}


def _atr(H, L, C, period):
    n = len(C)
    if n < 3:
        return float("nan")
    prev_c = C[:-1]
    tr = np.maximum(H[1:] - L[1:], np.maximum(np.abs(H[1:] - prev_c), np.abs(L[1:] - prev_c)))
    p = int(min(period, len(tr)))
    return float(np.mean(tr[-p:]))


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items() if k in DEFAULTS and v is not None})
    except Exception:
        pass

    liq_lb = int(p["liq_lb"]); sw_lb = int(p["sweep_lookback"]); pierce = float(p["pierce_atr"])
    mss_win = int(p["mss_win"]); mss_lb = int(p["mss_lb"])
    fvg_min = float(p["fvg_min_atr"]); fvg_exp = int(p["fvg_expiry"])
    need_tap = bool(int(p["require_fvg_tap"]))
    max_r = float(p["max_r"]); min_r = float(p["min_r"])
    buf = float(p["stop_buf_atr"]); atr_period = int(p["atr_period"])

    # ---------------- DATA HYGIENE (placeholder rows) ----------------
    try:
        valid = ((bars["high"] - bars["low"]) > 0) & (bars["volume"] > 0)
        if not bool(valid.iloc[-1]):
            return None
        b = bars[valid]
    except Exception:
        b = bars

    need = max(liq_lb, mss_lb) + mss_win + fvg_exp + sw_lb + 10
    b = b.tail(TAIL) if len(b) > TAIL else b
    n = len(b)
    if n < need or n < 80:
        return None
    cur = n - 1

    H = b["high"].to_numpy(float); L = b["low"].to_numpy(float)
    C = b["close"].to_numpy(float)

    a = _atr(H, L, C, atr_period)
    if not np.isfinite(a) or a <= 0:
        return None

    best = None  # (r_used, direction, entry, stop, target)

    for direction in ("long", "short"):
        # ---------------- 1. SWEEP of swing liquidity ----------------
        # long : bar pierces the rolling swing LOW and closes back above it
        # short: bar pierces the rolling swing HIGH and closes back below it
        # (a BODY close beyond the level = expansion, not a sweep -> rule 1)
        j_lo = max(1, cur - sw_lb)
        for j in range(cur - mss_win - 1, j_lo - 1, -1):
            if j - liq_lb < 1:
                continue
            if direction == "long":
                sell_side = float(np.min(L[j - liq_lb:j]))     # swept pool
                buy_side = float(np.max(H[j - liq_lb:j]))      # opposing pool
                if not (L[j] < sell_side - pierce * a and C[j] > sell_side):
                    continue
                sweep_ext = float(np.min(L[j:j + 1]))
            else:
                buy_side = float(np.max(H[j - liq_lb:j]))      # swept pool
                sell_side = float(np.min(L[j - liq_lb:j]))     # opposing pool
                if not (H[j] > buy_side + pierce * a and C[j] < buy_side):
                    continue
                sweep_ext = float(np.max(H[j:j + 1]))

            # ------------- 2. MSS: displacement breaks the last swing -----
            mss_bar = -1
            for k in range(j + 1, min(cur, j + mss_win) + 1):
                if k - mss_lb < 1:
                    continue
                rng_k = H[k] - L[k]
                if rng_k < 0.8 * a:
                    continue
                if direction == "long":
                    # bullish MSS: the displacement breaks the last swing HIGH
                    swing = float(np.max(H[k - mss_lb:k]))
                    if C[k] > swing:
                        mss_bar = k; break
                else:
                    # bearish MSS: the displacement breaks the last swing LOW
                    swing = float(np.min(L[k - mss_lb:k]))
                    if C[k] < swing:
                        mss_bar = k; break
            if mss_bar < 0:
                continue

            # ------- 3. FVG left by the leg AWAY from the sweep (to invert) --
            # long : the rally leg leaves a BULLISH gap (H[i-1] < L[i+1]); the
            #        entry is the close back ABOVE that gap (gap filled ->
            #        inverted -> bullish).
            # short: the drop leg leaves a BEARISH gap (L[i-1] > H[i+1]); the
            #        entry is the close back BELOW that gap.
            gap_edge = None
            gap_idx = -1
            for i in range(max(1, j + 1), mss_bar + 1):
                if direction == "long":
                    if H[i - 1] < L[i + 1] and (L[i + 1] - H[i - 1]) >= fvg_min * a:
                        gap_edge = float(L[i + 1]); gap_idx = i; break
                else:
                    if L[i - 1] > H[i + 1] and (L[i - 1] - H[i + 1]) >= fvg_min * a:
                        gap_edge = float(H[i + 1]); gap_idx = i; break
            if gap_idx < 0:
                continue

            # -------- 4. iFVG INVERSION: the gap gets FILLED, close = entry --
            entry = None
            for k in range(gap_idx + 1, cur + 1):
                if direction == "long":
                    if L[k] <= gap_edge and C[k] > gap_edge:
                        entry = float(C[k]); break
                else:
                    if H[k] >= gap_edge and C[k] < gap_edge:
                        entry = float(C[k]); break
            if entry is None:
                continue

            # ------- 5. optional HTF-FVG-TAP filter (approximation) ---------
            if need_tap:
                tapped = False
                post_hi = float(np.max(H[j + 1:cur + 1]))
                post_lo = float(np.min(L[j + 1:cur + 1]))
                for i in range(max(1, j - fvg_exp), j):
                    if direction == "long":
                        # bearish FVG formed before the sweep -> zone to tap
                        if L[i - 1] > H[i + 1] and (L[i - 1] - H[i + 1]) >= fvg_min * a:
                            zlo = float(H[i + 1]); zhi = float(L[i - 1])
                            if post_hi >= zlo and post_lo <= zhi:
                                tapped = True; break
                    else:
                        if H[i - 1] < L[i + 1] and (L[i + 1] - H[i - 1]) >= fvg_min * a:
                            zlo = float(H[i - 1]); zhi = float(L[i + 1])
                            if post_lo <= zhi and post_hi >= zlo:
                                tapped = True; break
                if not tapped:
                    continue

            # ------------ 6. stop beyond the SWEEP extreme, target opposing --
            if direction == "long":
                stop = sweep_ext - buf * a
                tgt_liq = buy_side
            else:
                stop = sweep_ext + buf * a
                tgt_liq = sell_side
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            sign = 1.0 if direction == "long" else -1.0
            if sign * (stop - entry) >= 0:
                continue
            r_liq = sign * (tgt_liq - entry) / risk
            if r_liq < min_r:
                continue
            r_use = min(r_liq, max_r)
            target = entry + sign * r_use * risk
            if sign * (target - entry) <= 0:
                continue
            cand = (r_use, direction, entry, stop, target)
            if best is None or cand[0] > best[0]:
                best = cand

    if best is None:
        return None
    _r, direction, entry, stop, target = best
    return {"direction": direction, "entry": entry,
            "stop": float(stop), "target": float(target),
            "ts": b["ts"].iloc[cur]}
