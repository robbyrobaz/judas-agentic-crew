"""OTE 79% Reversion family — registered as a standalone strategy module.

Concept (from YT:BP8_5iY9sso — Bull and Bear Forex 28 Sep 2026):
  After a LARGE displacement (one-shot move >= K * ATR over N bars), wait for
  price to retrace to the 62%-79% Fibonacci zone OF THAT displacement, then
  enter a counter-trend reversion IF the entry zone is near a HTF level
  (rolling prior-day high/low or weekly level).

This is COUNTER-TREND by design. Expectation: low winrate (~30-45%), high R targets.

Distinguishing features vs existing families:
  - ifvg_midpoint_reversion: targets FVG midpoint (50%), NOT fib 79%.
  - silver_bullet_pdh_pdl_retest: targets prior-day level, no displacement filter.
  - ict_mitigation_block: re-enters via OB re-touch, no fib level.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def evaluate(bars: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {
        "disp_atr_mult": 2.5,
        "disp_window": 5,
        "retrace_window": 10,
        "fib_low": 0.62,
        "fib_high": 0.79,
        "htf_prox_atr": 1.5,
        "htf_lookback": 48,
        "confirm_bars": 2,
        "target_r": 2.0,
        "stop_buffer_atr": 0.3,
        "atr_period": 14,
        "session_filter": False,
        "max_bars_in_trade": 40,
    }
    p.update(params or {})

    df = bars.copy()
    df.columns = [c.lower() for c in df.columns]
    if "time" not in df.columns and "timestamp" in df.columns:
        df = df.rename(columns={"timestamp": "time"})
    if "time" not in df.columns:
        df = df.reset_index().rename(columns={df.index.name or "index": "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")

    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)
    prev_c = c.shift(1)
    tr = pd.concat(
        [(h - l).abs(), (h - prev_c).abs(), (l - prev_c).abs()], axis=1
    ).max(axis=1)
    atr = tr.rolling(p["atr_period"]).mean()

    pdh = h.rolling(p["htf_lookback"]).max().shift(1)
    pdl = l.rolling(p["htf_lookback"]).min().shift(1)

    trades = []
    n = len(df)
    start_i = p["atr_period"] + p["htf_lookback"] + 2

    i = start_i
    while i < n - p["max_bars_in_trade"] - 1:
        a = atr.iloc[i]
        if not np.isfinite(a) or a <= 0:
            i += 1
            continue

        win_start = max(0, i - p["disp_window"])
        win = df.iloc[win_start:i]
        if len(win) < p["disp_window"]:
            i += 1
            continue
        win_range = win["high"].max() - win["low"].min()
        if win_range < p["disp_atr_mult"] * a:
            i += 1
            continue

        disp_h = float(win["high"].max())
        disp_l = float(win["low"].min())
        first_c = float(win["close"].iloc[0])
        last_c = float(win["close"].iloc[-1])
        bearish = last_c < first_c

        if bearish:
            fib_top = disp_h - p["fib_low"] * (disp_h - disp_l)
            fib_bot = disp_h - p["fib_high"] * (disp_h - disp_l)
            zone_low, zone_high = min(fib_top, fib_bot), max(fib_top, fib_bot)
            side = "long"
            stop_ref = disp_l
        else:
            fib_top = disp_l + p["fib_high"] * (disp_h - disp_l)
            fib_bot = disp_l + p["fib_low"] * (disp_h - disp_l)
            zone_low, zone_high = min(fib_top, fib_bot), max(fib_top, fib_bot)
            side = "short"
            stop_ref = disp_h

        entry_i = None
        for j in range(i, min(i + p["retrace_window"], n)):
            px = float(df["close"].iloc[j])
            if not (zone_low <= px <= zone_high):
                continue

            prox_ok = False
            if side == "long":
                pdl_v = pdl.iloc[j]
                if np.isfinite(pdl_v) and abs(px - float(pdl_v)) <= p["htf_prox_atr"] * a:
                    prox_ok = True
            else:
                pdh_v = pdh.iloc[j]
                if np.isfinite(pdh_v) and abs(px - float(pdh_v)) <= p["htf_prox_atr"] * a:
                    prox_ok = True

            if not prox_ok:
                continue

            if p["session_filter"]:
                hr = pd.Timestamp(df["time"].iloc[j]).hour
                if not (7 <= hr < 10 or 12 <= hr < 15):
                    continue

            if j + 1 >= n:
                continue
            cb = max(0, j - p["confirm_bars"] + 1)
            cs = df["close"].iloc[cb: j + 1]
            os_ = df["open"].iloc[cb: j + 1]
            if side == "long":
                if float(cs.iloc[-1]) <= float(os_.iloc[0]):
                    continue
            else:
                if float(cs.iloc[-1]) >= float(os_.iloc[0]):
                    continue

            entry_i = j
            break

        if entry_i is None:
            i += 1
            continue

        entry_px = float(df["close"].iloc[entry_i])
        if side == "long":
            stop_px = stop_ref - p["stop_buffer_atr"] * a
            risk = entry_px - stop_px
            target_px = entry_px + p["target_r"] * risk
        else:
            stop_px = stop_ref + p["stop_buffer_atr"] * a
            risk = stop_px - entry_px
            target_px = entry_px - p["target_r"] * risk

        if risk <= 0 or not np.isfinite(risk):
            i = entry_i + 1
            continue

        exit_i = entry_i
        exit_px = entry_px
        r_mult = 0.0
        for k in range(entry_i + 1, min(entry_i + p["max_bars_in_trade"] + 1, n)):
            hi = float(df["high"].iloc[k])
            lo = float(df["low"].iloc[k])
            if side == "long":
                if lo <= stop_px:
                    exit_i, exit_px = k, stop_px
                    r_mult = -1.0
                    break
                if hi >= target_px:
                    exit_i, exit_px = k, target_px
                    r_mult = float(p["target_r"])
                    break
            else:
                if hi >= stop_px:
                    exit_i, exit_px = k, stop_px
                    r_mult = -1.0
                    break
                if lo <= target_px:
                    exit_i, exit_px = k, target_px
                    r_mult = float(p["target_r"])
                    break
            exit_i, exit_i = k, k
            exit_px = float(df["close"].iloc[k])
            if side == "long":
                r_mult = (exit_px - entry_px) / risk
            else:
                r_mult = (entry_px - exit_px) / risk

        trades.append(
            {
                "entry_time": df["time"].iloc[entry_i],
                "exit_time": df["time"].iloc[exit_i],
                "side": side,
                "entry": entry_px,
                "exit": exit_px,
                "r_multiple": r_mult,
            }
        )

        i = exit_i + 1

    return pd.DataFrame(trades)
