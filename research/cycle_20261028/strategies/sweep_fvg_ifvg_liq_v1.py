"""SWEEP -> DISPLACEMENT -> FVG -> IFVG-INVERSION entry, with a STRUCTURAL
(opposing-liquidity) target.  (2026-10-28 cycle, YT ingest)

SOURCE (two independent videos ingested 2026-10-28)
---------------------------------------------------
1. Zen Trades, "The Easiest ICT Liquidity Sweep Strategy in 2026" (Zen Trades,
   pmexdsocN9c, 10:23).  Four steps, quoted: (1) "wait for a liquidity sweep"
   of a prominent swing/session level; (2) after the sweep "we want to see
   price tap inside of a higher time frame fair value gap"; (3) "we need to
   see price inverse a fair value gap to actually enter this trade" -- the
   entry trigger is the close back THROUGH an FVG (an iFVG), taken on the
   30s-5m chart; (4) "we want to be targeting opposing liquidity ... if
   there's no good liquidity, there's no good trade."
2. TradeIQ, "The Liquidity Sweep: How Banks Steal Your Stop Loss" (l27wF-EvDkw,
   10:24).  Same model with the geometry spelled out: "A sweep is a wick
   through the level with a close back inside"; then "displacement that breaks
   the last small high and leaves a fair value gap"; "enter on the retest of
   the fair value gap ... your stop goes below the sweep low ... your target?
   the next pool of liquidity on the other side"; filters: "no displacement,
   no trade", "a bullish sweep inside a strong daily downtrend is far more
   likely to fail".

WHAT IS NEW RELATIVE TO THE BOOK
--------------------------------
The promoted family (csid=439, MGC 15m, candidate #7382) is
sweep -> displacement CONTINUATION: it enters on the displacement bar's close
and targets a FIXED R multiple.  Two things here are different:
  * ENTRY TRIGGER = the FVG left by the displacement gets INVERTED (price
    trades back into the gap and closes back out of it).  That is a CLOSE-based
    trigger, so the fill is the decision bar's close -- the only price the
    broker can pay (finding #9134).  A LIMIT entry at the gap itself is NOT
    admissible here: it is the retro-fill artefact the registrar rejects.
  * TARGET = the next opposing LIQUIDITY (a structural swing level), gated by
    a minimum R.  This is Zen step 4 and the TradeIQ target rule, and it is
    the one filter the book has never implemented.

Entries are therefore always at the decision bar's CLOSE (`entry = close`),
so the stamped envelope is an executable fill, not a named level.

Stateless; numpy only.
"""
import numpy as np

TAIL = 500
DEFAULTS = {
    "lookback": 12,          # bars of the rolling swing level the sweep breaks
    "min_sweep_atr": 0.15,   # sweep must exceed the level by this many ATR
    "max_sweep_age": 12,     # bars back the sweep may be
    "disp_k": 1.0,           # displacement bar range in ATR
    "body_ratio_min": 0.55,  # displacement body / range
    "rr": 1.5,               # target R when target_mode == "rr"
    "min_rr": 1.5,           # minimum R for a structural liquidity target
    "max_rr": 4.0,           # cap on the structural target distance
    "liq_lookback": 60,      # bars searched for the next opposing liquidity
    "stop_buf_atr": 0.15,
    "atr_period": 14,
    "entry_mode": "ifvg",    # "ifvg" (FVG inversion) | "disp" (displacement)
    "target_mode": "liq",    # "liq" (structural liquidity) | "rr"
}


def _atr(H, L, C, period):
    n = len(C)
    if n < 3:
        return float("nan")
    pc = C[:-1]
    tr = np.maximum(H[1:] - L[1:], np.maximum(np.abs(H[1:] - pc), np.abs(L[1:] - pc)))
    p = int(min(period, len(tr)))
    return float(np.mean(tr[-p:]))


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items() if k in DEFAULTS and v is not None})
    except Exception:
        pass

    lb = int(p["lookback"]); ms = float(p["min_sweep_atr"])
    max_age = int(p["max_sweep_age"]); disp_k = float(p["disp_k"])
    min_body = float(p["body_ratio_min"]); rr = float(p["rr"])
    min_rr = float(p["min_rr"]); max_rr = float(p["max_rr"])
    liq_lb = int(p["liq_lookback"]); buf = float(p["stop_buf_atr"])
    atr_period = int(p["atr_period"])
    entry_mode = str(p["entry_mode"]); target_mode = str(p["target_mode"])

    # ---------------- DATA HYGIENE (placeholder rows dropped) ----------------
    try:
        valid = ((bars["high"] - bars["low"]) > 0) & (bars["volume"] > 0)
        if not bool(valid.iloc[-1]):
            return None
        b = bars[valid]
    except Exception:
        b = bars
    b = b.tail(TAIL) if len(b) > TAIL else b
    n = len(b)
    if n < max(120, lb + 30):
        return None
    cur = n - 1

    H = b["high"].to_numpy(float); L = b["low"].to_numpy(float)
    C = b["close"].to_numpy(float); O = b["open"].to_numpy(float)

    a = _atr(H, L, C, atr_period)
    if not np.isfinite(a) or a <= 0:
        return None

    ccur = C[cur]

    # ---------------- 1. THE SWEEP: wick through a level, close back inside --
    sweep = None
    for j in range(cur - 1, max(0, cur - max_age), -1):
        if j < lb + 2:
            break
        plk = float(np.min(L[j - lb:j])); phk = float(np.max(H[j - lb:j]))
        if L[j] < plk - ms * a and C[j] > plk:
            sweep = ("long", j, plk, phk); break
        if H[j] > phk + ms * a and C[j] < phk:
            sweep = ("short", j, plk, phk); break
    if sweep is None:
        return None
    dirn, j, plk, phk = sweep

    # ---------------- 2. THE SHIFT: displacement breaking the last swing ----
    disp = None
    for d in range(j + 1, cur + 1):
        rng = max(H[d] - L[d], 1e-12)
        br = abs(C[d] - O[d]) / rng
        if rng < disp_k * a or br < min_body:
            continue
        if dirn == "long":
            if C[d] <= phk:          # must break the last swing high (MSS)
                continue
        else:
            if C[d] >= plk:
                continue
        disp = d
        break
    if disp is None:
        return None

    # stop sits beyond the sweep extreme
    if dirn == "long":
        stop = float(L[j]) - buf * a
    else:
        stop = float(H[j]) + buf * a

    # ---------------- 3. ENTRY TRIGGER ---------------------------------------
    if entry_mode == "ifvg":
        if disp + 1 > cur:
            return None
        d = disp
        if dirn == "long":
            gap_lo = float(L[d - 1]); gap_hi = float(H[d + 1])
            if not (gap_lo < gap_hi):
                return None
            touched = bool(np.any(L[d + 1:cur + 1] <= gap_hi))
            if not (touched and ccur > gap_hi):
                return None
        else:
            gap_hi = float(H[d - 1]); gap_lo = float(L[d + 1])
            if not (gap_lo < gap_hi):
                return None
            touched = bool(np.any(H[d + 1:cur + 1] >= gap_lo))
            if not (touched and ccur < gap_lo):
                return None
    else:
        # displacement close (the csid=439 trigger) -- reference mode
        if dirn == "long":
            if C[disp] <= phk:
                return None
        else:
            if C[disp] >= plk:
                return None

    entry = float(ccur)
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    # ---------------- 4. TARGET: opposing liquidity (or fixed R) -----------
    target = None
    if target_mode == "liq":
        need = min_rr * risk
        if dirn == "long":
            lv = [float(H[k]) for k in range(max(0, cur - liq_lb), cur)
                  if H[k] > entry + need]
            if not lv:
                return None
            target = min(lv)
            if target > entry + max_rr * risk:
                target = entry + max_rr * risk
        else:
            lv = [float(L[k]) for k in range(max(0, cur - liq_lb), cur)
                  if L[k] < entry - need]
            if not lv:
                return None
            target = max(lv)
            if target < entry - max_rr * risk:
                target = entry - max_rr * risk
    else:
        target = entry + (rr * risk if dirn == "long" else -rr * risk)

    sign = 1.0 if dirn == "long" else -1.0
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": dirn, "entry": entry, "stop": float(stop),
            "target": float(target), "ts": b["ts"].iloc[cur]}
