"""ICT Silver Bullet -- 10:00-11:00 NY window + MIDNIGHT-NY-OPEN DAILY BIAS FILTER.

Source: YT X7kvlngAjY4 -- "ICT Silver Bullet Strategy: The Complete Guide
(Tested on 1 Year of Gold & Nasdaq)", Edge or Myth, Oct 2026.

Concrete rules extracted from the video (transcript-verified):
  * THREE windows: 03:00-04:00, 10:00-11:00 and 14:00-15:00 New York time.
    The creator's own 1-year test: the 14:00-15:00 window is "basically flat"
    (skip it), 03:00-04:00 works on gold but not Nasdaq, and 10:00-11:00 is the
    one that carries the edge (half the trades win, +$3,060 on its own).
  * LEVEL: mark the high AND the low of the HOUR IMMEDIATELY BEFORE the window
    (for the 10:00 window that is 09:00-10:00 NY). Stops sit above that high /
    below that low.
  * SWEEP: price pokes above (or below) that level, grabs the stops, and SNAPS
    BACK INSIDE. If price never returns, it was not a sweep -> no trade.
  * GAP: after the sweep, three candles in a row where the middle one moves so
    fast that candle 1 and candle 3 do not touch -> a Fair Value Gap.
  * ENTRY: at the EDGE of the gap (price touches the gap -> fade it).
  * STOP: just beyond the sweep extreme. TARGET: 2R.
  * DAILY BIAS FILTER -- "the one line drawn at midnight", the rule the video
    says "nobody puts in the thumbnail": mark the OPENING PRICE AT 00:00 NEW
    YORK. If price is BELOW that line when the setup arrives, the day is
    falling -> ONLY SELLS. If price is ABOVE it -> ONLY BUYS.
    Creator's 1-year numbers: every setup in all three windows, both markets
    = 1,128 trades, 38% WR, +$1,890 (< $2/trade). Adding the midnight line:
    362 trades, 42% WR, +$4,090 (> $11/trade) -- ~6x the expectancy per trade.

Why this is new to the book: no strategy in custom_strategies (437 rows) uses a
midnight/00:00 New York opening-price daily-bias line (grep for midnight /
day_open / daily_bias / 00:00 returns only unrelated hits). The existing
silver-bullet family ("*_pdh_pdl_retest_*") uses rolling prior-day high/low
levels and no daily-direction filter.

Engineering notes:
  * New York time is computed with pandas' own tz database
    (tz_convert("America/New_York")) so DST is handled exactly; no import of
    zoneinfo is needed (the sandbox only exposes np/pd/datetime).
  * evaluate() is called once per bar by the backtest engine, so the cheap
    pre-filter (is the current bar inside the NY window?) runs on every call
    and the expensive work only runs on the ~4-12 bars/day inside the window.
"""
import numpy as np
import pandas as pd


def _ny_fields(ts_series):
    """(hour, minute, ny_date) arrays for a pandas Series of UTC timestamps."""
    s = pd.to_datetime(ts_series, utc=True)
    ny = s.dt.tz_convert("America/New_York")
    return (ny.dt.hour.to_numpy(), ny.dt.minute.to_numpy(), ny.dt.date.to_numpy())


def evaluate(bars, params):
    if bars is None or len(bars) < 20:
        return None

    win_start = int(params.get("win_start_hour_ny", 10))
    win_end = int(params.get("win_end_hour_ny", 11))
    target_r = float(params.get("target_r", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.05))
    atr_period = int(params.get("atr_period", 14))
    use_bias = bool(params.get("use_bias", 1))
    entry_mode = str(params.get("entry_mode", "edge"))
    max_bars_after_sweep = int(params.get("max_bars_after_sweep", 12))
    min_gap_atr = float(params.get("min_gap_atr", 0.0))
    lookback = int(params.get("lookback_bars", 900))

    n = len(bars)
    cur_i = n - 1

    # ---- cheap pre-filter: current bar must be inside the NY window -------
    ts = bars["ts"]
    cur_ts = pd.Timestamp(ts.iloc[cur_i])
    if cur_ts.tzinfo is None:
        cur_ts = cur_ts.tz_localize("UTC")
    cur_ny = cur_ts.tz_convert("America/New_York")
    if not (win_start <= int(cur_ny.hour) < win_end):
        return None

    o = bars["open"].values.astype(float)
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)

    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr_series = pd.Series(tr).rolling(atr_period, min_periods=1).mean().to_numpy()
    atr = float(atr_series[cur_i])
    if not np.isfinite(atr) or atr <= 0:
        return None
    buf = atr * stop_buf_atr
    min_gap = atr * min_gap_atr

    # ---- heavy path: NY calendar geometry for the current NY day ----------
    lo = max(0, cur_i - lookback + 1)
    hh, mm, dd = _ny_fields(ts.iloc[lo:cur_i + 1])
    today = dd[-1]
    today_mask = (dd == today)

    idx_today = np.where(today_mask)[0]
    if len(idx_today) == 0:
        return None
    day_open = float(o[lo + int(idx_today[0])])

    idx_prior = np.where(today_mask & (hh == win_start - 1))[0]
    if len(idx_prior) == 0:
        return None
    prior_high = float(np.max(h[lo + idx_prior]))
    prior_low = float(np.min(l[lo + idx_prior]))

    idx_win = np.where(today_mask & (hh >= win_start) & (hh < win_end))[0]
    if len(idx_win) == 0:
        return None

    # ---- sweep -> FVG -> entry --------------------------------------------
    setup = None
    for k in range(len(idx_win)):
        j = lo + int(idx_win[k])
        if j >= cur_i:
            break
        direction = None
        extreme = None
        if h[j] > prior_high and c[j] < prior_high:
            direction = "short"
            extreme = float(h[j])
        elif l[j] < prior_low and c[j] > prior_low:
            direction = "long"
            extreme = float(l[j])
        if direction is None:
            continue

        for k2 in range(k + 1, len(idx_win)):
            i = lo + int(idx_win[k2])
            if i >= cur_i or (i - j) > max_bars_after_sweep:
                break
            if i - 2 < 0:
                continue
            # ICT 3-candle FVG (candle1 and candle3 do not touch):
            #   bearish / gap-down : l[i-2] > h[i]   -> zone (h[i], l[i-2])
            #   bullish / gap-up   : h[i-2] < l[i]   -> zone (h[i-2], l[i])
            if direction == "short":
                if not (l[i - 2] > h[i]):
                    continue
                zone_low = float(h[i])        # near edge: first touch on the retrace UP
                zone_high = float(l[i - 2])    # far edge: gap fully filled if traded
                if (zone_high - zone_low) < min_gap:
                    continue
                entry_px = zone_low if entry_mode == "edge" else (zone_low + zone_high) / 2.0
                if h[cur_i] < entry_px:
                    continue
                if np.max(h[i + 1:cur_i + 1]) >= zone_high:
                    continue  # gap already fully filled -> setup stale
                if c[cur_i] > zone_high:
                    continue
                stop = extreme + buf
                risk = stop - entry_px
                if risk <= 0:
                    continue
                setup = ("short", entry_px, stop, entry_px - risk * target_r)
                break
            else:
                if not (h[i - 2] < l[i]):
                    continue
                zone_low = float(h[i - 2])    # far edge
                zone_high = float(l[i])       # near edge: first touch on the retrace DOWN
                if (zone_high - zone_low) < min_gap:
                    continue
                entry_px = zone_high if entry_mode == "edge" else (zone_low + zone_high) / 2.0
                if l[cur_i] > entry_px:
                    continue
                if np.min(l[i + 1:cur_i + 1]) <= zone_low:
                    continue
                if c[cur_i] < zone_low:
                    continue
                stop = extreme - buf
                risk = entry_px - stop
                if risk <= 0:
                    continue
                setup = ("long", entry_px, stop, entry_px + risk * target_r)
                break
        if setup is not None:
            break

    if setup is None:
        return None

    direction, entry_px, stop, target = setup

    # ---- the midnight line: daily direction filter -------------------------
    if use_bias:
        if direction == "short" and not (c[cur_i] < day_open):
            return None
        if direction == "long" and not (c[cur_i] > day_open):
            return None

    return {"direction": direction, "entry": float(entry_px),
            "stop": float(stop), "target": float(target),
            "ts": ts.iloc[cur_i]}
