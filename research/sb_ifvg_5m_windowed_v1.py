"""
ICT Silver Bullet + iFVG midpoint reversion on 5m — windowed to NY AM only.

Distillation from YouTube synthesis (Aug 8 2026):
- 10:00-11:00 AM ET (14:00-15:00 UTC) is the highest-probability ICT killzone
  (Silver Bullet). Confirmed across 7+ recent videos (M FOREX, FXNX, ICT_SilverBullet,
  Baidar Fx, etc.).
- Combine: iFVG midpoint reversion (proven edge across 7 symbols, PF 2-6) +
  strict session filter (NY AM 14:00-15:00 UTC only).
- The window filter reduces signal count but should increase WR by skipping
  off-session FVGs that don't have institutional flow behind them.

Rules (5m bars):
  1) Compute 5m FVG: 3-bar pattern where bar[i-2].high < bar[i].low (bullish FVG)
     or bar[i-2].low > bar[i].high (bearish FVG).
  2) Compute displacement: the 3rd bar of the FVG must have body >= body_ratio
     * (high-low) and range >= min_displacement_atr * ATR(20).
  3) HTF bias: rolling 20-bar close slope must agree with FVG direction.
  4) Time filter: only signal between 14:00-15:00 UTC (NY AM Silver Bullet).
  5) Entry: at midpoint of FVG when price retraces into it.
  6) Stop: 0.1 ATR beyond FVG edge.
  7) Target: target_r * risk (default 2.0R).

Validation targets: MGC, MNQ, MCL, 6J (active symbols only; DX/MBT/MET banned).
"""

import numpy as np
import pandas as pd


def _atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    h = bars["high"].astype(float)
    l = bars["low"].astype(float)
    c = bars["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def evaluate(bars: pd.DataFrame, params: dict) -> dict | None:
    body_ratio_min = float(params.get("body_ratio_min", 0.55))
    min_disp_atr = float(params.get("min_disp_atr", 1.0))
    target_r = float(params.get("target_r", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.1))
    htf_slope_len = int(params.get("htf_slope_len", 20))
    # Time filter (UTC) — Silver Bullet NY AM = 14:00-15:00 UTC
    use_sb_window = bool(params.get("use_sb_window", True))
    sb_start_hour = int(params.get("sb_start_hour", 14))
    sb_end_hour = int(params.get("sb_end_hour", 15))
    # Optional London pre-open (11:00-12:00 UTC) window
    use_london_window = bool(params.get("use_london_window", False))
    london_start_hour = int(params.get("london_start_hour", 11))
    london_end_hour = int(params.get("london_end_hour", 12))

    n = len(bars)
    if n < 80:
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    open_ = bars["open"].astype(float)

    # Need a datetime index for hour filtering; some feeds use 'timestamp' column
    if "timestamp" in bars.columns:
        ts = pd.to_datetime(bars["timestamp"], utc=True)
    elif isinstance(bars.index, pd.DatetimeIndex):
        ts = bars.index
    else:
        return None

    atr = _atr(bars).iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    cur_hour = ts.iloc[-1].hour if hasattr(ts, "hour") else None
    if cur_hour is None:
        return None

    # Session window filter
    in_sb = use_sb_window and (sb_start_hour <= cur_hour < sb_end_hour)
    in_london = use_london_window and (london_start_hour <= cur_hour < london_end_hour)
    if not (in_sb or in_london):
        return None

    # HTF bias: rolling 20-bar slope of close
    htf_slope = float(close.iloc[-htf_slope_len:].iloc[-1] - close.iloc[-htf_slope_len:].iloc[0])
    bias_long = htf_slope > 0
    bias_short = htf_slope < 0

    # Scan last K bars for an unmitigated FVG in HTF bias direction
    scan_n = 60
    start = max(0, n - scan_n)
    direction = None
    fvg_top = None
    fvg_bot = None
    for i in range(n - 1, start + 1, -1):
        # Bullish FVG: bar[i-2].high < bar[i].low
        if float(high.iloc[i - 2]) < float(low.iloc[i]):
            gap_top = float(low.iloc[i])
            gap_bot = float(high.iloc[i - 2])
            # Displacement check on bar i
            bar_range = float(high.iloc[i]) - float(low.iloc[i])
            bar_body = abs(float(close.iloc[i]) - float(open_.iloc[i]))
            if bar_range <= 0:
                continue
            if bar_body / bar_range < body_ratio_min:
                continue
            if bar_range < min_disp_atr * float(atr):
                continue
            # Mitigated? price came back through gap
            mid_retrace = (gap_top + gap_bot) / 2.0
            mitigated = False
            for j in range(i, n):
                if float(low.iloc[j]) <= gap_bot:
                    mitigated = True
                    break
            if mitigated:
                continue
            # Is current price in the gap?
            cur_close = float(close.iloc[-1])
            if gap_bot <= cur_close <= gap_top and bias_long:
                direction = "long"
                fvg_top = gap_top
                fvg_bot = gap_bot
                break
        # Bearish FVG: bar[i-2].low > bar[i].high
        elif float(low.iloc[i - 2]) > float(high.iloc[i]):
            gap_top = float(low.iloc[i - 2])
            gap_bot = float(high.iloc[i])
            bar_range = float(high.iloc[i]) - float(low.iloc[i])
            bar_body = abs(float(close.iloc[i]) - float(open_.iloc[i]))
            if bar_range <= 0:
                continue
            if bar_body / bar_range < body_ratio_min:
                continue
            if bar_range < min_disp_atr * float(atr):
                continue
            mitigated = False
            for j in range(i, n):
                if float(high.iloc[j]) >= gap_top:
                    mitigated = True
                    break
            if mitigated:
                continue
            cur_close = float(close.iloc[-1])
            if gap_bot <= cur_close <= gap_top and bias_short:
                direction = "short"
                fvg_top = gap_top
                fvg_bot = gap_bot
                break

    if direction is None:
        return None

    mid = (fvg_top + fvg_bot) / 2.0
    entry = mid
    if direction == "long":
        stop = fvg_bot - stop_buf_atr * float(atr)
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + target_r * risk
    else:
        stop = fvg_top + stop_buf_atr * float(atr)
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - target_r * risk

    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "target": target,
        "fvg_top": fvg_top,
        "fvg_bot": fvg_bot,
        "risk": risk,
    }