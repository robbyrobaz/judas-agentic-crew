"""FVG TAP + REJECTION CLOSE -> MARKET ENTRY (production-fill safe).

WHY THIS EXISTS
---------------
The book already contains two families that mean-revert to the midpoint of a
Fair Value Gap / Order Block ("ifvg_midpoint_reversion", "ob_midpoint_reversion",
"ict_mitigation_block").  Every one of them RESTS A LIMIT ORDER at the
midpoint and reports that price as the fill.  The live runtime cannot do that:
`_place_fire_with_record` -> `place_bracket_nt` sends a MARKET entry order and
then RE-ANCHORS the stop/target distances to the achieved fill.  Under the only
admissible fill model (fill_mode="production": decision on the close of bar T,
fill at the OPEN of bar T+1) a named limit entry is therefore priced ~55-350
ticks away from the achievable fill, which is exactly the defect the book has
been fighting (finding 07f3a4c2 / 9164: csid401 PF 4.56 -> 0.187).

This family removes the limit order from the architecture:

    1. A Fair Value Gap forms (bullish: L[j] > H[j-2]).
    2. Price TAPS into that gap.
    3. Price REJECTS it -- a bar CLOSES back OUT of the gap, on the far side.
    4. Entry = MARKET at the close of the decision bar (what the runtime
       actually does).  No resting order, no named fill price.
    5. Stop  = beneath the gap (and beneath the rejection low) + ATR buffer.
    6. Target = target_r * risk from the decision close.

The rejection CLOSE is the confirmation that the gap is being defended; the
entry no longer depends on the fill price at all, so the envelope is honest
under an executable fill.

RULES (mechanical)
------------------
Bullish FVG at bar j:            L[j] > H[j-2]                (gap zone: [H[j-2], L[j]])
Bearish FVG at bar j:            H[j] < L[j-2]                (gap zone: [L[j-2], H[j]])
Gap height filter:               (zone_top - zone_bot) >= min_fvg_atr * ATR[j]
Tap:                             a bar t with j < t <= cur that trades INTO the zone
Rejection:                       a tap bar t whose CLOSE is back OUTSIDE the zone
                                 (bullish: C[t] > zone_top; bearish: C[t] < zone_bot)
Hold:                            no bar after t closes back INSIDE the zone
Entry:                           MARKET at C[cur]
Stop:                            bullish: min(zone_bot, min low in [t..cur]) - buf*ATR
                                 bearish: max(zone_top, max high in [t..cur]) + buf*ATR
Target:                          entry +/- target_r * risk

Optional filters (off by default unless stated): trend bias, session gate.
"""

import numpy as np
import pandas as pd

TAIL = 400

DEFAULTS = {
    "atr_period": 14,
    "min_fvg_atr": 0.15,      # gap height filter, in ATR
    "fvg_lookback": 40,       # bars back to search for the gap formation
    "tap_max_age": 12,        # rejection must be within N bars of the decision
    "target_r": 2.0,
    "stop_buf_atr": 0.20,
    "min_risk_atr": 0.25,     # reject absurdly tight risk (noise trades)
    "bias_bars": 0,           # >0 -> long only if close[cur] > close[cur-bias]
    "bias_dir": 1,            # +1 trend up for longs, -1 trend down for shorts
    "allow_long": 1,
    "allow_short": 1,
}


def _atr(high, low, close, period):
    pc = close[:-1]
    tr = np.empty(len(high), dtype=float)
    tr[0] = high[0] - low[0]
    tr[1:] = np.maximum.reduce([
        (high[1:] - low[1:]),
        np.abs(high[1:] - pc),
        np.abs(low[1:] - pc),
    ])
    alpha = 1.0 / float(period)
    out = np.empty(len(tr), dtype=float)
    acc = tr[0]
    out[0] = acc
    for i in range(1, len(tr)):
        acc = alpha * tr[i] + (1.0 - alpha) * acc
        out[i] = acc
    return out


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items() if k in DEFAULTS and v is not None})
    except Exception:
        pass

    try:
        t = bars.tail(TAIL)
        if len(t) < 60:
            return None
        H = t["high"].to_numpy(float)
        L = t["low"].to_numpy(float)
        C = t["close"].to_numpy(float)
        ts = t["ts"].to_numpy()
    except Exception:
        return None

    n = len(C)
    cur = n - 1
    if cur < 40:
        return None

    atr = _atr(H, L, C, int(p["atr_period"]))
    a = atr[cur]
    if not np.isfinite(a) or a <= 0:
        return None

    min_fvg_atr = float(p["min_fvg_atr"])
    lookback = int(p["fvg_lookback"])
    tap_age = int(p["tap_max_age"])
    target_r = float(p["target_r"])
    buf = float(p["stop_buf_atr"])
    min_risk = float(p["min_risk_atr"]) * a
    bias_bars = int(p["bias_bars"])
    bias_dir = int(p["bias_dir"])
    allow_long = bool(p["allow_long"])
    allow_short = bool(p["allow_short"])

    lo_j = max(2, cur - lookback)
    lo_t = max(0, cur - tap_age)

    best_long = None
    best_short = None

    # ---- bullish FVG (gap up): long on a rejection close above the gap -----
    if allow_long:
        for j in range(cur - 1, lo_j - 1, -1):
            if L[j] <= H[j - 2]:
                continue
            zone_bot = float(H[j - 2])
            zone_top = float(L[j])
            if (zone_top - zone_bot) < min_fvg_atr * atr[j]:
                continue
            # find the most recent tap+rejection bar t in (j, cur]
            rej = None
            for t in range(j + 1, cur + 1):
                if L[t] <= zone_top and C[t] > zone_top:
                    rej = t
            if rej is None or rej < lo_t:
                continue
            # the gap must have held: no close back inside the zone after rej
            if np.any(C[rej + 1:cur + 1] <= zone_top):
                continue
            # entry at the decision close
            entry = float(C[cur])
            ext_low = float(np.min(L[rej:cur + 1]))
            stop = min(zone_bot, ext_low) - buf * a
            risk = entry - stop
            if risk < min_risk:
                continue
            if bias_bars > 0 and cur - bias_bars >= 0:
                if C[cur] <= C[cur - bias_bars]:
                    continue
            best_long = {
                "direction": "long",
                "entry": entry,
                "stop": float(stop),
                "target": float(entry + target_r * risk),
                "ts": ts[cur],
                "features": {"fvg_kind": "bullish", "fvg_age": cur - j,
                             "rej_age": cur - rej, "risk_atr": risk / a},
            }
            break

    # ---- bearish FVG (gap down): short on a rejection close below the gap ---
    if allow_short:
        for j in range(cur - 1, lo_j - 1, -1):
            if H[j] >= L[j - 2]:
                continue
            zone_bot = float(L[j - 2])
            zone_top = float(H[j])
            if (zone_top - zone_bot) < min_fvg_atr * atr[j]:
                continue
            rej = None
            for t in range(j + 1, cur + 1):
                if H[t] >= zone_bot and C[t] < zone_bot:
                    rej = t
            if rej is None or rej < lo_t:
                continue
            if np.any(C[rej + 1:cur + 1] >= zone_bot):
                continue
            entry = float(C[cur])
            ext_high = float(np.max(H[rej:cur + 1]))
            stop = max(zone_top, ext_high) + buf * a
            risk = stop - entry
            if risk < min_risk:
                continue
            if bias_bars > 0 and cur - bias_bars >= 0:
                if C[cur] >= C[cur - bias_bars]:
                    continue
            best_short = {
                "direction": "short",
                "entry": entry,
                "stop": float(stop),
                "target": float(entry - target_r * risk),
                "ts": ts[cur],
                "features": {"fvg_kind": "bearish", "fvg_age": cur - j,
                             "rej_age": cur - rej, "risk_atr": risk / a},
            }
            break

    if best_long is not None and best_short is not None:
        # prefer the fresher rejection
        if best_long["features"]["rej_age"] >= best_short["features"]["rej_age"]:
            return best_short
        return best_long
    return best_long if best_long is not None else best_short
