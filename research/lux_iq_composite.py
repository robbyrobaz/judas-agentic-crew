"""
Lux IQ Composite: Sweep + MSS + FVG + OB sniper entry.

5-step model: sweep on HTF, MSS push + FVG, entry on LTF retrace into zone.
"""
from typing import List, Dict, Any

def _atr(window: List[dict], n: int) -> float:
    if len(window) < n + 1:
        return 0.0
    trs = []
    for i in range(-n, 0):
        h = window[i]["high"]
        l = window[i]["low"]
        pc = window[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n

def _is_swing_high(bars: List[dict], i: int, look: int) -> bool:
    if i - look < 0 or i + look >= len(bars):
        return False
    h = bars[i]["high"]
    for j in range(i - look, i + look + 1):
        if j == i:
            continue
        if bars[j]["high"] >= h:
            return False
    return True

def _is_swing_low(bars: List[dict], i: int, look: int) -> bool:
    if i - look < 0 or i + look >= len(bars):
        return False
    l = bars[i]["low"]
    for j in range(i - look, i + look + 1):
        if j == i:
            continue
        if bars[j]["low"] <= l:
            return False
    return True

def evaluate(bars: List[dict], params: Dict[str, Any]) -> Dict[str, Any]:
    p = {
        "swing_look": 3,
        "swing_search_bars": 24,
        "fvg_min_atr_mult": 0.10,
        "sweep_min_atr_mult": 0.05,
        "entry_zone": "top",
        "stop_buffer_atr_mult": 0.20,
        "target_rr": 2.0,
        "max_sweep_age_bars": 6,
        "max_hold_bars": 80,
        "warmup": 60,
    }
    p.update({k: v for k, v in params.items() if k in p})

    n = len(bars)
    if n < p["warmup"] + 30:
        return {"trades": [], "metrics": {"n": 0, "reason": "insufficient_bars"}}

    trades = []
    last_i = p["warmup"]
    while last_i < n - 1:
        window = bars[max(0, last_i - 50):last_i + 1]
        atr_val = _atr(window, 14) if len(window) >= 15 else 0.0
        if atr_val <= 0:
            last_i += 1
            continue

        # Step 1: nearest swing H/L
        sh = -1.0
        sl = 1e18
        for k in range(max(0, last_i - p["swing_search_bars"]), last_i - 3):
            if _is_swing_high(bars, k, p["swing_look"]):
                if bars[k]["high"] > sh:
                    sh = bars[k]["high"]
            if _is_swing_low(bars, k, p["swing_look"]):
                if bars[k]["low"] < sl:
                    sl = bars[k]["low"]

        if sh <= 0 or sl >= 1e17:
            last_i += 1
            continue

        # Step 2: sweep detection on recent bars
        direction = 0
        sweep_i = -1
        sweep_wick = 0.0
        for j in range(last_i, max(p["warmup"], last_i - p["max_sweep_age_bars"]), -1):
            bar = bars[j]
            if bar["low"] < sl and bar["close"] > sl and (sl - bar["low"]) >= p["sweep_min_atr_mult"] * atr_val:
                direction = 1
                sweep_i = j
                sweep_wick = bar["low"]
                break
            if bar["high"] > sh and bar["close"] < sh and (bar["high"] - sh) >= p["sweep_min_atr_mult"] * atr_val:
                direction = -1
                sweep_i = j
                sweep_wick = bar["high"]
                break

        if direction == 0:
            last_i += 1
            continue

        # Step 3: MSS push bar (next 1-3 after sweep)
        push_i = -1
        fvg_top = 0.0
        fvg_bot = 0.0
        if direction == 1:
            ref_sh = -1.0
            for k in range(max(0, sweep_i - p["swing_search_bars"]), sweep_i - 2):
                if _is_swing_high(bars, k, p["swing_look"]):
                    if bars[k]["high"] > ref_sh:
                        ref_sh = bars[k]["high"]
            if ref_sh <= 0:
                last_i += 1
                continue
            for k in range(sweep_i + 1, min(n, sweep_i + 4)):
                if bars[k]["close"] > ref_sh:
                    if k - 2 >= 0 and bars[k - 2]["high"] < bars[k]["low"]:
                        gap_size = bars[k]["low"] - bars[k - 2]["high"]
                        if gap_size >= p["fvg_min_atr_mult"] * atr_val:
                            push_i = k
                            fvg_top = bars[k]["low"]
                            fvg_bot = bars[k - 2]["high"]
                            break
        else:
            ref_sl = 1e18
            for k in range(max(0, sweep_i - p["swing_search_bars"]), sweep_i - 2):
                if _is_swing_low(bars, k, p["swing_look"]):
                    if bars[k]["low"] < ref_sl:
                        ref_sl = bars[k]["low"]
            if ref_sl >= 1e17:
                last_i += 1
                continue
            for k in range(sweep_i + 1, min(n, sweep_i + 4)):
                if bars[k]["close"] < ref_sl:
                    if k - 2 >= 0 and bars[k - 2]["low"] > bars[k]["high"]:
                        gap_size = bars[k - 2]["low"] - bars[k]["high"]
                        if gap_size >= p["fvg_min_atr_mult"] * atr_val:
                            push_i = k
                            fvg_top = bars[k - 2]["low"]
                            fvg_bot = bars[k]["high"]
                            break

        if push_i < 0:
            last_i += 1
            continue

        # Step 4: sniper zone
        ob_low = min(bars[sweep_i]["open"], bars[sweep_i]["close"])
        ob_high = max(bars[sweep_i]["open"], bars[sweep_i]["close"])
        zone_low = min(ob_low, fvg_bot)
        zone_high = max(ob_high, fvg_top)
        if direction == 1:
            if p["entry_zone"] == "top":
                entry = fvg_top
            elif p["entry_zone"] == "mid":
                entry = (zone_low + zone_high) / 2
            else:
                entry = zone_low
            stop = sweep_wick - p["stop_buffer_atr_mult"] * atr_val
        else:
            if p["entry_zone"] == "top":
                entry = zone_high
            elif p["entry_zone"] == "mid":
                entry = (zone_low + zone_high) / 2
            else:
                entry = fvg_bot
            stop = sweep_wick + p["stop_buffer_atr_mult"] * atr_val

        # Wait for retrace into zone
        entry_hit_i = -1
        end_search = min(n - 1, push_i + p["max_sweep_age_bars"])
        for k in range(push_i + 1, end_search + 1):
            bar = bars[k]
            if direction == 1 and bar["low"] <= entry and bar["close"] >= zone_low:
                entry_hit_i = k
                break
            if direction == -1 and bar["high"] >= entry and bar["close"] <= zone_high:
                entry_hit_i = k
                break

        if entry_hit_i < 0:
            last_i += 1
            continue

        # Step 5: target = 2R
        risk = abs(entry - stop)
        if risk <= 0:
            last_i = entry_hit_i + 1
            continue
        target = entry + direction * p["target_rr"] * risk

        # walk forward
        exit_price = None
        exit_reason = "hold"
        for k in range(entry_hit_i + 1, min(n, entry_hit_i + p["max_hold_bars"] + 1)):
            bar = bars[k]
            if direction == 1:
                if bar["low"] <= stop:
                    exit_price = stop
                    exit_reason = "sl"
                    break
                if bar["high"] >= target:
                    exit_price = target
                    exit_reason = "tp"
                    break
            else:
                if bar["high"] >= stop:
                    exit_price = stop
                    exit_reason = "sl"
                    break
                if bar["low"] <= target:
                    exit_price = target
                    exit_reason = "tp"
                    break
        if exit_price is None:
            exit_price = bars[min(n - 1, entry_hit_i + p["max_hold_bars"])]["close"]

        pnl_ticks = (exit_price - entry) * direction
        trades.append({
            "entry_i": entry_hit_i,
            "direction": direction,
            "entry": entry,
            "stop": stop,
            "target": target,
            "exit": exit_price,
            "pnl_ticks": pnl_ticks,
            "exit_reason": exit_reason,
        })
        last_i = entry_hit_i + 1

    if not trades:
        return {"trades": [], "metrics": {"n": 0, "reason": "no_trades"}}

    wins = [t for t in trades if t["pnl_ticks"] > 0]
    losses = [t for t in trades if t["pnl_ticks"] <= 0]
    pf = (sum(t["pnl_ticks"] for t in wins) / abs(sum(t["pnl_ticks"] for t in losses))) if losses else 99.0
    return {
        "trades": trades,
        "metrics": {
            "n": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "winrate": len(wins) / len(trades),
            "profit_factor": pf,
            "avg_win_ticks": sum(t["pnl_ticks"] for t in wins) / max(1, len(wins)),
            "avg_loss_ticks": sum(t["pnl_ticks"] for t in losses) / max(1, len(losses)),
            "total_ticks": sum(t["pnl_ticks"] for t in trades),
        },
    }