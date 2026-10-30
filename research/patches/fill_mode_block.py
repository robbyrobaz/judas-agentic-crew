    n_signals = 0
    wins = 0
    losses = 0
    pnls: list[float] = []
    rs: list[float] = []
    open_trade: dict | None = None
    error: str | None = None
    # Retro-fill diagnostic (finding 07f3a4c2). COUNTED, never dropped, so no
    # existing result changes -- but every stamp now carries its own
    # contamination, because a signal filled on a bar that had already closed
    # can produce an envelope the market never offered.
    stale_fill_n = 0
    # A measurement that cannot be taken must never read as a clean result
    # (see _stale_gap_seconds below). Counted and stamped separately.
    stale_measure_fail_n = 0
    # --- PRODUCTION-FILL bookkeeping (finding 9164) -----------------------
    # n_entries_filled   : decisions that produced a real market fill.
    # n_entries_unfilled : decisions with no later bar to fill on.
    # entry_slip_ticks   : SIGNED distance between the ACHIEVABLE fill and the
    #                      entry the strategy ASKED for, in ticks. Positive
    #                      == the market made us pay worse than the strategy
    #                      wanted. Under the retro-fill model this was
    #                      structurally zero and therefore invisible.
    n_entries_filled = 0
    n_entries_unfilled = 0
    entry_slip_ticks_sum = 0.0
    entry_slip_ticks_abs_sum = 0.0
    n_entry_slip_samples = 0
    fair_fill = fill_mode in ("production", "production_close")
    tick_size = float(spec["tick"])

    def _resolve_exit(trade: dict, bar, fair: bool):
        """Return ``(points, reason)`` when the trade closes on ``bar``.

        ``None`` means the trade is still open.

        ``fair=True`` applies PRODUCTION semantics:
          * a stop already breached at the bar's OPEN fills at the OPEN (a
            stop-MARKET order triggers into the gap) -- without this, a long
            whose stop sits above the open books a phantom PROFIT on a stop;
          * adverse-first: a bar spanning both the stop and the target books
            the STOP, because the intrabar path is unknown.
        """
        direction = trade["direction"]
        entry = float(trade["entry"])
        stop = float(trade["stop"])
        target = float(trade["target"])
        o = float(bar["open"])
        h = float(bar["high"])
        l = float(bar["low"])
        if direction == "long":
            if fair and o <= stop:
                return (o - entry), "stop"
            if l <= stop:
                return (stop - entry), "stop"
            if h >= target:
                return (target - entry), "target"
        else:
            if fair and o >= stop:
                return (entry - o), "stop"
            if h >= stop:
                return (entry - stop), "stop"
            if l <= target:
                return (entry - target), "target"
        return None

    try:
        def _run_loop():
            nonlocal n_signals, wins, losses, open_trade, stale_fill_n
            nonlocal stale_measure_fail_n, n_entries_filled, n_entries_unfilled
            nonlocal entry_slip_ticks_sum, entry_slip_ticks_abs_sum
            nonlocal n_entry_slip_samples
            n = len(bars)
            # A decision that has been taken but not yet filled (fair modes).
            pending: dict | None = None
            # cap iterations to bound runtime
            for i in range(2, n):
                if time.time() - started > timeout_s:
                    raise _StrategyTimeout(f"backtest exceeded {timeout_s}s")
                slice_df = bars.iloc[: i + 1]
                bar = bars.iloc[i]

                # ---- 1) FILL a pending decision (fair modes only) ----------
                if pending is not None:
                    if fill_mode == "production_close":
                        fill_price = float(bars.iloc[pending["decision_index"]]["close"])
                    else:
                        fill_price = float(bar["open"])
                    want = pending.get("want_entry")
                    if want is not None and tick_size > 0:
                        sign = 1.0 if pending["direction"] == "long" else -1.0
                        slip_ticks = (fill_price - want) * sign / tick_size
                        entry_slip_ticks_sum += slip_ticks
                        entry_slip_ticks_abs_sum += abs(slip_ticks)
                        n_entry_slip_samples += 1
                    open_trade = {
                        "direction": pending["direction"],
                        "entry": fill_price,
                        "stop": pending["stop"],
                        "target": pending["target"],
                    }
                    pending = None
                    n_entries_filled += 1

                # ---- 2) RESOLVE the open trade against this bar -------------
                if open_trade is not None:
                    res = _resolve_exit(open_trade, bar, fair=fair_fill)
                    if res is not None:
                        points, reason = res
                        entry = float(open_trade["entry"])
                        risk = abs(entry - float(open_trade["stop"])) or 1.0
                        if not fair_fill:
                            # legacy: reproduce the historical point formula exactly
                            target_won = reason == "target"
                            points = (abs(float(open_trade["target"]) - entry) if target_won
                                      else -abs(entry - float(open_trade["stop"])))
                        gross_pnl = points * dollars_per_point * trade_qty
                        pnl = apply_costs_to_trade(
                            symbol=sym,
                            gross_pnl=gross_pnl,
                            exit_reason="target" if reason == "target" else "stop",
                            qty=trade_qty,
                        )
                        if pnl > 0:
                            wins += 1
                        else:
                            losses += 1
                        pnls.append(float(pnl))
                        risk_dollars = risk * dollars_per_point * trade_qty
                        rs.append(float(pnl / risk_dollars) if risk_dollars else 0.0)
                        open_trade = None
                    continue

                # ---- 3) LOOK for a new decision on this bar -----------------
                try:
                    sig = fn(slice_df, p)
                except Exception:
                    sig = None
                if sig is None or not isinstance(sig, dict):
                    continue
                direction = str(sig.get("direction", "")).lower()
                if direction not in ("long", "short"):
                    continue
                try:
                    stop = float(sig["stop"])
                    target = float(sig["target"])
                except (KeyError, TypeError, ValueError):
                    continue
                try:
                    entry = float(sig["entry"])
                except (KeyError, TypeError, ValueError):
                    entry = None
                if entry is None and not fair_fill:
                    # legacy path needs the derived entry as the fill price
                    continue
                n_signals += 1
                # RETRO-FILL DIAGNOSTIC. The engine only ever hands evaluate()
                # bars[:i+1], so it cannot legitimately know a price from any bar
                # but the decision bar. If the signal's ts predates that bar by
                # more than 90s it was filled retroactively. Counted only --
                # dropping the signal here would silently rewrite history.
                _sig_ts = sig.get("ts")
                if _sig_ts is not None:
                    _gap = _stale_gap_seconds(bars.iloc[i]["ts"], _sig_ts)
                    if _gap is None:
                        stale_measure_fail_n += 1
                    elif _gap > _STALE_GAP_S:
                        stale_fill_n += 1
                if fair_fill:
                    pending = {
                        "direction": direction,
                        "stop": stop,
                        "target": target,
                        "want_entry": entry,
                        "decision_index": i,
                    }
                else:
                    open_trade = {
                        "direction": direction,
                        "entry": entry,
                        "stop": stop,
                        "target": target,
                    }
                    n_entries_filled += 1
            if pending is not None:
                # decided on the final bar -- there is no later bar to fill on
                n_entries_unfilled += 1

