"""RUBBER-BAND CONTINUATION, MARKET ENTRY (YT dtxPQFHJl9U, LuxAlgo).

RULES EXTRACTED FROM THE VIDEO
------------------------------
The video describes a 4-phase cycle, all of it measured RELATIVE TO ATR:

  1. TREND     -- a strong directional move of at least `trend_atr_mult` x ATR
                  over `trend_lookback` bars.  (Video: "the trend move ATR
                  multiplier controls how large that move needs to be relative
                  to ATR before it qualifies as a strong trend.")
  2. CONSOLIDATION -- after that move price goes sideways and forms a RANGE
                  BOX.  (Video: "consolidation length", "range max ATR
                  multiplier" -- the box must not be too wide, and it goes
                  stale after `max_range_age` bars.)
  3. SWEEP     -- price pierces ONE side of the box and CLOSES BACK INSIDE it.
                  This is the trap that separates a fake breakout from a real
                  one.  (Video: "price to pierce the range high or low, but
                  then close back inside the range.  That close back inside is
                  very important.")
  4. CONTINUATION -- price then breaks out of the box IN THE ORIGINAL TREND
                  DIRECTION.  (Video: "if the original trend was bullish and
                  price sweeps below the range before breaking above the
                  range, the indicator can mark a bullish continuation
                  signal.")

ENTRY HONESTY
-------------
The signal is only complete on the breakout bar, so the entry is that bar's
CLOSE (a market-on-close order).  There is NO structural entry price anywhere
in this file -- the box boundary is used only to DETECT the breakout, never as
a price to fill at.  The stop is structural (beyond the sweep extreme), the
target is `rr` x risk.  This is the same discipline as csid 439 (the only
family in the book with a genuine non-directional edge, finding 9153).

PLACEHOLDER BARS
----------------
The deep archive is padded with placeholder rows (open=high=low=close at a
stale price, volume 0).  They manufacture fake sweeps and fake breakouts, so
the strategy only ever looks at a CONTIGUOUS RUN OF REAL BARS.

Stateless; numpy only (sandbox-safe).
"""
import numpy as np

DEFAULTS = {
    "atr_period": 14,
    "trend_lookback": 20,
    "trend_atr_mult": 1.5,
    "consol_bars": 8,
    "range_max_atr": 2.5,
    "sweep_lookback": 8,
    "sweep_min_atr": 0.05,
    "break_body_min": 0.55,
    "rr": 1.5,
    "stop_buf_atr": 0.15,
    "min_risk_atr": 0.20,
    "max_risk_atr": 4.00,
    "direction": 0,          # 0 = both, 1 = long only, -1 = short only
    "trend_mode": 0,         # 0 = close-to-close, 1 = extremes (wicks)
}


def _cfg(params, key):
    try:
        if params is not None and key in params and params[key] is not None:
            return params[key]
    except TypeError:
        pass
    return DEFAULTS[key]


def _real_run(H, L, V, cur):
    """Index of the first bar of the contiguous run of REAL bars ending at cur."""
    if not (H[cur] > L[cur]) or V[cur] <= 0:
        return -1
    i = cur
    while i > 0 and H[i - 1] > L[i - 1] and V[i - 1] > 0:
        i -= 1
    return i


def _atr(H, L, C, start, cur, period):
    s = max(int(start), int(cur) - int(period), 1)
    if s > cur:
        return float("nan")
    hh = H[s:cur + 1]
    ll = L[s:cur + 1]
    pc = C[s - 1:cur]
    tr = np.maximum(hh - ll, np.maximum(np.abs(hh - pc), np.abs(ll - pc)))
    if len(tr) == 0:
        return float("nan")
    return float(np.mean(tr[-int(period):]))


def evaluate(bars, params):
    atr_period = int(_cfg(params, "atr_period"))
    trend_lookback = int(_cfg(params, "trend_lookback"))
    trend_atr_mult = float(_cfg(params, "trend_atr_mult"))
    consol_bars = int(_cfg(params, "consol_bars"))
    range_max_atr = float(_cfg(params, "range_max_atr"))
    sweep_lookback = int(_cfg(params, "sweep_lookback"))
    sweep_min_atr = float(_cfg(params, "sweep_min_atr"))
    break_body_min = float(_cfg(params, "break_body_min"))
    rr = float(_cfg(params, "rr"))
    buf_mult = float(_cfg(params, "stop_buf_atr"))
    min_risk_atr = float(_cfg(params, "min_risk_atr"))
    max_risk_atr = float(_cfg(params, "max_risk_atr"))
    direction = int(_cfg(params, "direction"))
    trend_mode = int(_cfg(params, "trend_mode"))

    need = trend_lookback + consol_bars + sweep_lookback + atr_period + 12
    if len(bars) < need:
        return None
    w = bars.iloc[-need:]
    H = w["high"].to_numpy(float)
    L = w["low"].to_numpy(float)
    O = w["open"].to_numpy(float)
    C = w["close"].to_numpy(float)
    if "volume" in w.columns:
        V = w["volume"].to_numpy(float)
    else:
        V = np.ones(len(w), dtype=float)
    cur = len(w) - 1

    run_start = _real_run(H, L, V, cur)
    if run_start < 0:
        return None
    a = _atr(H, L, C, run_start, cur, atr_period)
    if not np.isfinite(a) or a <= 0:
        return None

    ccur = float(C[cur])
    hcur = float(H[cur])
    lcur = float(L[cur])
    rng = max(hcur - lcur, 1e-9)
    br = abs(ccur - float(O[cur])) / rng

    best = None                      # (k, box_low, box_high, side)
    k_lo = max(run_start + consol_bars + trend_lookback + 2,
               cur - sweep_lookback)
    for k in range(cur - 1, k_lo - 1, -1):
        box_hi = float(np.max(H[k - consol_bars:k]))
        box_lo = float(np.min(L[k - consol_bars:k]))
        width = box_hi - box_lo
        if width <= 0.10 * a or width > range_max_atr * a:
            continue
        # --- 3. SWEEP: pierce one side, close back inside -------------
        if L[k] < box_lo - sweep_min_atr * a and C[k] > box_lo:
            best = (k, box_lo, box_hi, "bull")
            break
        if H[k] > box_hi + sweep_min_atr * a and C[k] < box_hi:
            best = (k, box_lo, box_hi, "bear")
            break
    if best is None:
        return None
    k, box_lo, box_hi, side = best

    # --- 1. TREND: strong move BEFORE the consolidation box ----------
    t_hi = k - consol_bars                       # last trend bar (exclusive)
    t_lo = t_hi - trend_lookback
    if t_lo < run_start:
        return None
    if trend_mode == 1:
        up_move = float(np.max(H[t_lo:t_hi + 1]) - np.min(L[t_lo:t_hi + 1]))
        dn_move = up_move
        up_move = float(C[t_hi] - C[t_lo])
        dn_move = float(C[t_lo] - C[t_hi])
    else:
        up_move = float(C[t_hi] - C[t_lo])
        dn_move = float(C[t_lo] - C[t_hi])

    trend_up = up_move >= trend_atr_mult * a
    trend_dn = dn_move >= trend_atr_mult * a

    # --- 4. CONTINUATION: breakout of the box WITH the trend --------
    sig_dir = None
    stop = None
    if side == "bear" and trend_up and ccur > box_hi and br >= break_body_min:
        sig_dir = "long"
        stop = min(float(L[k]), box_lo) - buf_mult * a
    elif side == "bull" and trend_dn and ccur < box_lo and br >= break_body_min:
        sig_dir = "short"
        stop = max(float(H[k]), box_hi) + buf_mult * a
    if sig_dir is None:
        return None
    if direction == 1 and sig_dir != "long":
        return None
    if direction == -1 and sig_dir != "short":
        return None

    entry = ccur                      # HONEST: the decision bar's close
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    if risk < min_risk_atr * a or risk > max_risk_atr * a:
        return None
    sign = 1.0 if sig_dir == "long" else -1.0
    target = entry + sign * rr * risk
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": sig_dir, "entry": float(entry),
            "stop": float(stop), "target": float(target),
            "ts": w["ts"].iloc[cur]}
