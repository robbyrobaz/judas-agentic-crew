"""THREE-CANDLE LIQUIDITY GRAB REVERSAL (YT p0rmH1VmNYQ, Com Lucro Trader).

RULES EXTRACTED FROM THE VIDEO (bullish side; bearish is the mirror)
---------------------------------------------------------------------
* HTF structure is trending up and price has retraced into a HTF point of
  interest (FVG / OB / supply-demand / swing level).
* CANDLE 1 -- "the first candle establishes the reference low".
* CANDLE 2 -- sweeps BELOW candle 1's low, REJECTS lower prices (closes back
  above candle 1's low) but FAILS to give the Two-Candle confirmation
  (it does not close above candle 1's range).  "If candle two fails to
  confirm a valid Two-Candle Liquidity Grab Reversal, we do not force the
  setup."
* CANDLE 3 -- "does not take out the low of candle two, but instead closes
  above the top of candle two's body ... effectively engulfing it".  That is
  the delayed confirmation.
* Execution: after the confirmation the trader looks for a lower-timeframe
  confirmation (CISD / CHoCH) and enters; stop goes beyond the most recent
  swing high/low.  For a market-entry implementation the confirmation bar's
  CLOSE is the entry (the live path submits a MARKET order -- there is no
  LIMIT entry path in this repo, finding 9134).

ENTRY HONESTY
-------------
The pattern is only complete when candle 3 closes, so the entry is candle 3's
close.  There is NO structural entry price anywhere in this file: candle 2's
body top is used only to CONFIRM the pattern, never as a price to fill at.
The stop is structural (beyond candle 2's extreme) and the target is `rr` x
risk.  This is the gate-4 discipline (no retro-fill artefact).

PLACEHOLDER BARS
----------------
The deep archive is padded with placeholder rows (open=high=low=close at a
stale price, volume 0).  They manufacture fake sweeps and fake patterns, so
the strategy only looks at a CONTIGUOUS RUN OF REAL BARS.

Stateless; numpy only (sandbox-safe).
"""
import numpy as np

DEFAULTS = {
    "atr_period": 14,
    "rr": 1.5,
    "body_min": 0.35,          # min body ratio of the confirmation candle
    "engulf": 0,               # 0 = close above bar2 body top, 1 = close above bar2 HIGH
    "require_not_break": 0,    # 1 = candle 3 must not take out candle 2's extreme
    "min_risk_atr": 0.15,
    "max_risk_atr": 4.0,
    "stop_buf_atr": 0.15,
    "poi_lookback": 0,         # 0 = no POI filter
    "poi_min_atr": 0.0,
    "direction": 0,            # 0 = both, 1 = long only, -1 = short only
}


def _cfg(params, key):
    try:
        if params is not None and key in params and params[key] is not None:
            return params[key]
    except TypeError:
        pass
    return DEFAULTS[key]


def _real_run(H, L, V, cur):
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
    rr = float(_cfg(params, "rr"))
    body_min = float(_cfg(params, "body_min"))
    engulf = int(_cfg(params, "engulf"))
    not_break = int(_cfg(params, "require_not_break"))
    min_risk_atr = float(_cfg(params, "min_risk_atr"))
    max_risk_atr = float(_cfg(params, "max_risk_atr"))
    buf_mult = float(_cfg(params, "stop_buf_atr"))
    poi_lookback = int(_cfg(params, "poi_lookback"))
    poi_min_atr = float(_cfg(params, "poi_min_atr"))
    direction = int(_cfg(params, "direction"))

    need = 60 + poi_lookback
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
    if cur < 6:
        return None
    run_start = _real_run(H, L, V, cur)
    if run_start < 0 or cur - run_start < 4:
        return None
    a = _atr(H, L, C, run_start, cur, atr_period)
    if not np.isfinite(a) or a <= 0:
        return None

    b1, b2 = cur - 2, cur - 1
    if b1 < run_start:
        return None

    rng3 = max(float(H[cur]) - float(L[cur]), 1e-9)
    br3 = abs(float(C[cur]) - float(O[cur])) / rng3
    if br3 < body_min:
        return None

    sig_dir = None
    stop = None

    # ---------------- BULLISH ----------------
    body2_top = max(float(O[b2]), float(C[b2]))
    if (float(L[b2]) < float(L[b1])                 # candle 2 sweeps below
            and float(C[b2]) > float(L[b1])          # ... and rejects lower prices
            and float(C[b2]) <= float(H[b1])         # ... but FAILS the 2-candle close
            and float(C[cur]) > body2_top            # candle 3 closes above bar2 body
            and (not engulf or float(C[cur]) > float(H[b2]))):
        if not_break and float(L[cur]) <= float(L[b2]):
            pass
        else:
            sig_dir = "long"
            stop = float(L[b2]) - buf_mult * a

    # ---------------- BEARISH ----------------
    body2_bot = min(float(O[b2]), float(C[b2]))
    if (float(H[b2]) > float(H[b1])
            and float(C[b2]) < float(H[b1])
            and float(C[b2]) >= float(L[b1])
            and float(C[cur]) < body2_bot
            and (not engulf or float(C[cur]) < float(L[b2]))):
        if not_break and float(H[cur]) >= float(H[b2]):
            pass
        else:
            sig_dir = "short"
            stop = float(H[b2]) + buf_mult * a

    if sig_dir is None:
        return None
    if direction == 1 and sig_dir != "long":
        return None
    if direction == -1 and sig_dir != "short":
        return None

    # ---------------- optional HTF POI filter ----------------
    # A bullish FVG (3-candle imbalance) formed inside the last `poi_lookback`
    # bars whose zone the current bar is trading into.  This is a FILTER on
    # WHERE the pattern happens -- it never becomes an entry price.
    if poi_lookback > 0:
        min_gap = max(poi_min_atr * a, 0.02 * a)
        ok_poi = False
        for j in range(cur - 1, max(run_start, cur - poi_lookback - 1), -1):
            if j - 2 < run_start:
                break
            if sig_dir == "long":
                if H[j - 2] < L[j] and (L[j] - H[j - 2]) >= min_gap:
                    if float(L[cur]) <= float(L[j]) and float(H[cur]) >= float(H[j - 2]):
                        ok_poi = True
                        break
            else:
                if L[j - 2] > H[j] and (L[j - 2] - H[j]) >= min_gap:
                    if float(H[cur]) >= float(H[j]) and float(L[cur]) <= float(L[j - 2]):
                        ok_poi = True
                        break
        if not ok_poi:
            return None

    entry = float(C[cur])              # HONEST: the confirmation candle's close
    risk = abs(entry - stop)
    if risk <= 0 or risk < min_risk_atr * a or risk > max_risk_atr * a:
        return None
    sign = 1.0 if sig_dir == "long" else -1.0
    target = entry + sign * rr * risk
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": sig_dir, "entry": float(entry),
            "stop": float(stop), "target": float(target),
            "ts": w["ts"].iloc[cur]}
