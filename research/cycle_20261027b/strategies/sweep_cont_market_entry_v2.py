"""MARKET-ENTRY SWEEP CONTINUATION v2 -- honest fills + previous-day-high/low sweep.

WHY THIS FILE
-------------
The house backtester (`src/research/custom_strategy_runtime.py`) FILLS AT THE
PRICE THE STRATEGY NAMES.  Findings #9126 / #9133 / #9145 / #9146 proved that
every family in the book that names a STRUCTURAL price (FVG midpoint, OB
midpoint, the sweep extreme) is a RETRO-FILL artefact: the move from that
named price to the decision close is never risked.

The FIX is a MARKET entry: the strategy decides on closed data and the market
order goes out right after the decision bar's CLOSE.  There is no structural
entry price anywhere in this file, so:

    base fill  == decision close   (what the house backtester stamps)
    fair fill  == decision close
    live fill  == next bar's open  (production, brackets re-anchored)

Stops and targets stay structural (that is where the edge information lives);
only the ENTRY is honest.

TWO SETUPS
----------
  sweep_cont      liquidity sweep of a ROLLING extreme (max/min of the last
                  `lookback` bars) -> displacement back through that level ->
                  continuation.  This is the architecture promoted on MGC
                  (csid 439 / candidate 7382).
  pd_sweep_cont   liquidity sweep of the PREVIOUS DAY's HIGH/LOW (PDH/PDL) ->
                  displacement -> continuation.  Same market-entry discipline,
                  but the liquidity pool is the overnight extreme instead of a
                  rolling window extreme.  The book has PDH/PDL families, but
                  every one of them enters at a STRUCTURAL price (the FVG /
                  OB midpoint), which is the retro-fill construction.

DISPLACEMENT MODES
------------------
  through   the decision bar closes back through the swept LEVEL  (default)
  beyond    the decision bar closes beyond the swept EXTREME (a full break)
  either    either of the above

PLACEHOLDER-BAR FILTER
----------------------
The deep archive is padded with PLACEHOLDER bars (open=high=low=close at a
stale price, volume 0) wherever IBKR history was missing.  Those bars create
fake sweeps and fake displacement candles, so the strategy only ever looks at
a CONTIGUOUS RUN OF REAL BARS (range > 0 and volume > 0) ending at the
decision bar.  Inside a real run this is a no-op.

STATELESS; numpy only (sandbox-safe: no imports beyond numpy).
"""
import numpy as np


DEFAULTS = {
    # --- CELL --- (the house grid substitutes this literal)
    "setup": "sweep_cont",
    "lookback": 12,
    "min_sweep_atr": 0.30,
    "disp_k": 1.0,
    "rr": 1.5,
    "body_ratio_min": 0.55,
    "stop_buf_atr": 0.15,
    "atr_period": 14,
    "confirm_bars": 6,
    "ema_period": 0,
    "session_start_utc": -1,
    "session_end_utc": -1,
    "disp_mode": "through",
    "pd_max_age_days": 4,
    "sweep_max_atr": 99.0,
}


def _cfg(params, key):
    try:
        if params is not None and key in params:
            return params[key]
    except TypeError:
        pass
    return DEFAULTS[key]


def _real_run(H, L, V, cur):
    """Index of the first bar of the contiguous run of REAL bars ending at cur.

    Returns -1 when the decision bar itself is not real.
    """
    if not (H[cur] > L[cur]) or V[cur] <= 0:
        return -1
    i = cur
    while i > 0 and H[i - 1] > L[i - 1] and V[i - 1] > 0:
        i -= 1
    return i


def _atr(H, L, C, start, cur, period):
    """Mean true range over the last `period` bars INSIDE the real run."""
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


def _ema(C, period):
    alpha = 2.0 / (float(period) + 1.0)
    out = float(C[0])
    for x in C[1:]:
        out += alpha * (float(x) - out)
    return out


def evaluate(bars, params):
    setup = str(_cfg(params, "setup"))
    lookback = int(_cfg(params, "lookback"))
    min_sweep_atr = float(_cfg(params, "min_sweep_atr"))
    disp_k = float(_cfg(params, "disp_k"))
    rr = float(_cfg(params, "rr"))
    body_ratio_min = float(_cfg(params, "body_ratio_min"))
    stop_buf_atr = float(_cfg(params, "stop_buf_atr"))
    atr_period = int(_cfg(params, "atr_period"))
    confirm_bars = int(_cfg(params, "confirm_bars"))
    ema_period = int(_cfg(params, "ema_period"))
    sess_start = float(_cfg(params, "session_start_utc"))
    sess_end = float(_cfg(params, "session_end_utc"))
    disp_mode = str(_cfg(params, "disp_mode"))
    pd_max_age = int(_cfg(params, "pd_max_age_days"))

    need = lookback + confirm_bars + atr_period + 12
    if setup == "pd_sweep_cont":
        need = max(need, 260)
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
    ts = w["ts"].to_numpy()

    run_start = _real_run(H, L, V, cur)
    if run_start < 0:
        return None
    if (cur - run_start + 1) < lookback + 2:
        return None
    if (cur - run_start + 1) < atr_period + 1:
        return None

    a = _atr(H, L, C, run_start, cur, atr_period)
    if not np.isfinite(a) or a <= 0:
        return None
    ms = min_sweep_atr * a
    buf = stop_buf_atr * a

    ccur = float(C[cur])
    hcur = float(H[cur])
    lcur = float(L[cur])
    rng = max(hcur - lcur, 1e-9)
    br = abs(ccur - float(O[cur])) / rng

    # optional UTC session gate
    if sess_start >= 0:
        hour = float(ts[cur].astype("datetime64[h]").astype("int64") % 24)
        if not (sess_start <= hour < sess_end):
            return None

    direction = None
    stop = None

    if setup == "sweep_cont":
        j = -1
        for k in range(cur - 1, max(run_start, cur - confirm_bars - 1), -1):
            if k - lookback < run_start:
                continue
            phk = float(np.max(H[k - lookback:k]))
            plk = float(np.min(L[k - lookback:k]))
            if L[k] < plk - ms and C[k] > plk:
                j = k
                break
            if H[k] > phk + ms and C[k] < phk:
                j = k
                break
        if j < 0:
            return None
        plj = float(np.min(L[j - lookback:j]))
        phj = float(np.max(H[j - lookback:j]))
        if L[j] < plj - ms and C[j] > plj:
            # swept the lows -> long continuation
            if disp_mode in ("through", "either"):
                ok = ccur > plj
            else:
                ok = ccur < float(L[j])
            if not (ok and br >= body_ratio_min and rng >= disp_k * a):
                return None
            direction, stop = "long", float(L[j]) - buf
        else:
            if disp_mode in ("through", "either"):
                ok = ccur < phj
            else:
                ok = ccur > float(H[j])
            if not (ok and br >= body_ratio_min and rng >= disp_k * a):
                return None
            direction, stop = "short", float(H[j]) + buf

    elif setup == "sess_sweep":
        # SESSION-POOL liquidity sweep (YT rZnjkZ_JV2o, Mulham "Turtle Soup").
        #   * London kill zone 06:00-09:00 UTC  -> pool = the ASIAN session
        #     extreme (00:00-04:00 UTC, same day)
        #   * New York kill zone 11:00-14:00 UTC -> pool = the LONDON session
        #     extreme (06:00-09:00 UTC, same day)
        #   * no trade outside the kill zones
        #   * sweep = wick through the pool level with NO body close beyond it
        #   * trigger = the decision bar closes back beyond the REJECTION
        #     CANDLE'S BODY EDGE (body low for a bullish sweep, body high for a
        #     bearish one) -- the video's "mark the body edge, enter when the
        #     lower timeframe closes beyond it", collapsed onto one timeframe.
        hours = ts.astype("datetime64[h]").astype("int64") % 24
        days = ts.astype("datetime64[D]").astype("int64")
        hcur_h = float(hours[cur])
        d0 = int(days[cur])
        if 6.0 <= hcur_h < 9.0:
            pool_mask = (hours >= 0) & (hours <= 3) & (days == d0)
        elif 11.0 <= hcur_h < 14.0:
            pool_mask = (hours >= 6) & (hours <= 8) & (days == d0)
        else:
            return None
        if int(pool_mask.sum()) < 4:
            return None
        pool_high = float(np.max(H[pool_mask]))
        pool_low = float(np.min(L[pool_mask]))
        max_sweep_atr = float(_cfg(params, "sweep_max_atr"))
        j = -1
        for k in range(cur - 1, max(run_start, cur - confirm_bars - 1), -1):
            if not (run_start <= k):
                continue
            if H[k] > pool_high + ms and C[k] < pool_high:
                if max_sweep_atr < 90 and (H[k] - L[k]) > max_sweep_atr * a:
                    continue
                j = k
                break
            if L[k] < pool_low - ms and C[k] > pool_low:
                if max_sweep_atr < 90 and (H[k] - L[k]) > max_sweep_atr * a:
                    continue
                j = k
                break
        if j < 0:
            return None
        body_low = float(min(O[j], C[j]))
        body_high = float(max(O[j], C[j]))
        if L[j] < pool_low - ms and C[j] > pool_low:
            if not (ccur > body_low):
                return None
            direction, stop = "long", float(L[j]) - buf
        else:
            if not (ccur < body_high):
                return None
            direction, stop = "short", float(H[j]) + buf

    elif setup == "pd_sweep_cont":
        # previous DAY's high/low from the window's own timestamps
        days = ts.astype("datetime64[D]").astype("int64")
        d0 = int(days[cur])
        prev_days = days[days < d0]
        if len(prev_days) == 0:
            return None
        pd_day = int(np.max(prev_days))
        if (d0 - pd_day) > pd_max_age:
            return None
        m = (days == pd_day)
        if int(m.sum()) < 24:
            return None
        pdh = float(np.max(H[m]))
        pdl = float(np.min(L[m]))
        j = -1
        for k in range(cur - 1, max(run_start, cur - confirm_bars - 1), -1):
            if H[k] > pdh + ms and C[k] < pdh:
                j = k
                break
            if L[k] < pdl - ms and C[k] > pdl:
                j = k
                break
        if j < 0:
            return None
        if H[j] > pdh + ms:
            # swept the previous day's high -> continuation short
            if disp_mode in ("through", "either"):
                ok = ccur < pdh
            else:
                ok = ccur > float(H[j])
            if not (ok and br >= body_ratio_min and rng >= disp_k * a):
                return None
            direction, stop = "short", float(H[j]) + buf
        else:
            if disp_mode in ("through", "either"):
                ok = ccur > pdl
            else:
                ok = ccur < float(L[j])
            if not (ok and br >= body_ratio_min and rng >= disp_k * a):
                return None
            direction, stop = "long", float(L[j]) - buf
    else:
        return None

    if direction is None or stop is None:
        return None

    entry = ccur                      # HONEST: decision bar's close
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    target = entry + sign * rr * risk

    # geometry guard: stop on the losing side, target on the winning side
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": float(entry),
            "stop": float(stop), "target": float(target),
            "ts": w["ts"].iloc[cur]}
