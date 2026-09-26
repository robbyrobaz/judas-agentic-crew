"""
Portfolio-level concurrency guard test for MGC 15m duplicate-fire fan-out.

Simulates the 4 sister atr_disp_continuation variants:
  #4567 (CSID 156) -- atr_disp_continuation, both dirs, target_r=2.0
  #4601 (CSID 264) -- atr_disp_long_only, long only, target_r=1.5
  #4602 (CSID 265) -- range_cycle_disp_cont, both dirs, target_r=1.5
  #4605 (CSID 266) -- atr_disp_continuation_strict, both dirs, target_r=1.5

When displacement fires, all 4 strategies that pass their own threshold see
the SAME bar and fire (within seconds of each other -- confirmed by live data
showing 4-way same-bar entries 2026-09-10T01:20Z and 2026-09-07T23:45Z).

This BT quantifies: BASELINE (all fire) vs CAPPED (max 2 per same-bar same-dir).
"""

import pandas as pd
import numpy as np


def atr(high, low, close, period=14):
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def is_displacement(open, high, low, close, atr_val, disp_atr_mult, body_ratio_min):
    rng = high - low
    body = abs(close - open)
    if rng <= 0 or pd.isna(atr_val) or atr_val <= 0:
        return False
    return (rng >= disp_atr_mult * atr_val) and (body / rng >= body_ratio_min)


def evaluate(bars: pd.DataFrame, params: dict) -> dict:
    p = dict(params)
    if 'variants' not in p:
        # Default 4 sister variants as on book 2026-09-12
        p['variants'] = [
            {'name': 'v7_cs156', 'disp_atr_mult': 1.5, 'body_ratio_min': 0.75,
             'pullback_pct': 0.5, 'target_r': 2.0, 'side_filter': 'both'},
            {'name': 'v11_cs264', 'disp_atr_mult': 1.5, 'body_ratio_min': 0.70,
             'pullback_pct': 0.4, 'target_r': 1.5, 'side_filter': 'long_only'},
            {'name': 'v12_cs265', 'disp_atr_mult': 1.3, 'body_ratio_min': 0.70,
             'pullback_pct': 0.5, 'target_r': 1.5, 'side_filter': 'both'},
            {'name': 'v13_cs266', 'disp_atr_mult': 1.8, 'body_ratio_min': 0.70,
             'pullback_pct': 0.5, 'target_r': 1.5, 'side_filter': 'both'},
        ]

    atr_period = p.get('atr_period', 14)
    variants = p['variants']
    cap_per_dir = p.get('cap_per_dir', 2)
    tick_size = p.get('tick_size', 0.1)
    point_value = p.get('point_value', 10)
    slippage_ticks = p.get('slippage_ticks', 1)
    commission = p.get('commission_dollars', 1.50)
    stop_buffer_atr = p.get('stop_buffer_atr', 0.15)

    bars = bars.copy()
    bars['atr'] = atr(bars['high'], bars['low'], bars['close'], atr_period)

    for v in variants:
        disp_key = f"disp_{v['name']}"
        bars[disp_key] = [
            is_displacement(bars.iloc[i]['open'], bars.iloc[i]['high'],
                            bars.iloc[i]['low'], bars.iloc[i]['close'],
                            bars.iloc[i]['atr'], v['disp_atr_mult'],
                            v['body_ratio_min'])
            for i in range(len(bars))
        ]

    n = len(bars)

    def run_variant(v):
        trades = []
        i = 0
        while i < n - 2:
            if not bars.iloc[i][f"disp_{v['name']}"]:
                i += 1
                continue
            prev = bars.iloc[i]
            direction = 'long' if prev['close'] > prev['open'] else 'short'
            if v['side_filter'] == 'long_only' and direction != 'long':
                i += 1
                continue
            if v['side_filter'] == 'short_only' and direction != 'short':
                i += 1
                continue

            entry_idx = i + 1
            if entry_idx >= n:
                break
            cur = bars.iloc[entry_idx]

            if direction == 'long':
                entry_px = cur['open'] + tick_size * slippage_ticks
                stop_px = prev['low'] - stop_buffer_atr * prev['atr']
            else:
                entry_px = cur['open'] - tick_size * slippage_ticks
                stop_px = prev['high'] + stop_buffer_atr * prev['atr']

            risk = abs(entry_px - stop_px)
            if risk <= 0 or pd.isna(risk):
                i += 1
                continue
            target_px = entry_px + v['target_r'] * risk * (1 if direction == 'long' else -1)

            exit_idx = entry_idx
            exit_px = entry_px
            exit_reason = 'eod'
            for j in range(entry_idx + 1, min(entry_idx + 200, n)):
                bar = bars.iloc[j]
                if direction == 'long':
                    if bar['low'] <= stop_px:
                        exit_idx = j
                        exit_px = stop_px
                        exit_reason = 'stop'
                        break
                    if bar['high'] >= target_px:
                        exit_idx = j
                        exit_px = target_px
                        exit_reason = 'target'
                        break
                else:
                    if bar['high'] >= stop_px:
                        exit_idx = j
                        exit_px = stop_px
                        exit_reason = 'stop'
                        break
                    if bar['low'] <= target_px:
                        exit_idx = j
                        exit_px = target_px
                        exit_reason = 'target'
                        break
            else:
                exit_idx = n - 1
                exit_px = bars.iloc[-1]['close']
                exit_reason = 'eod'

            pnl_pts = (exit_px - entry_px) * (1 if direction == 'long' else -1)
            pnl_dollars = pnl_pts * point_value - 2 * commission
            trades.append({
                'entry_idx': entry_idx, 'variant': v['name'], 'direction': direction,
                'entry_px': entry_px, 'exit_px': exit_px, 'pnl_dollars': pnl_dollars,
                'exit_reason': exit_reason, 'disp_bar_idx': i
            })
            i = exit_idx + 1
        return trades

    baseline_trades = []
    for v in variants:
        baseline_trades.extend(run_variant(v))
    baseline_trades.sort(key=lambda t: (t['entry_idx'], t['direction']))

    # Apply cap per (entry_idx, direction)
    capped_trades = []
    by_bar_dir = {}
    for t in baseline_trades:
        key = (t['entry_idx'], t['direction'])
        by_bar_dir.setdefault(key, []).append(t)
    for key, ts in by_bar_dir.items():
        kept = ts[:cap_per_dir]
        capped_trades.extend(kept)
    capped_trades.sort(key=lambda t: t['entry_idx'])

    def stats(trades, label):
        if not trades:
            return {'label': label, 'n': 0}
        wins = [t for t in trades if t['pnl_dollars'] > 0]
        losses = [t for t in trades if t['pnl_dollars'] <= 0]
        n = len(trades)
        n_w = len(wins)
        n_l = len(losses)
        wr = n_w / n if n else 0
        gross_win = sum(t['pnl_dollars'] for t in wins)
        gross_loss = abs(sum(t['pnl_dollars'] for t in losses))
        pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
        total = sum(t['pnl_dollars'] for t in trades)
        return {
            'label': label, 'n': n, 'n_wins': n_w, 'n_losses': n_l,
            'winrate': round(wr, 3),
            'profit_factor': round(pf, 2) if pf != float('inf') else 'inf',
            'avg_pnl_per_trade': round(total / n, 2),
            'total_pnl': round(total, 2),
            'gross_win': round(gross_win, 2),
            'gross_loss': round(gross_loss, 2),
        }

    base_stats = stats(baseline_trades, 'baseline_all_4_fire')
    cap_stats = stats(capped_trades, f'capped_max_{cap_per_dir}_per_bar_per_dir')

    # Duplicate-fire event analysis
    baskets = {}
    for t in baseline_trades:
        baskets.setdefault(t['entry_idx'], []).append(t)
    duplicate_baskets = [(k, v) for k, v in baskets.items() if len(v) > 1]
    duplicate_count = len(duplicate_baskets)
    duplicate_total_pnl = sum(sum(t['pnl_dollars'] for t in v) for _, v in duplicate_baskets)
    duplicate_winning_baskets = sum(1 for _, v in duplicate_baskets
                                    if all(t['pnl_dollars'] > 0 for t in v))

    blocked_basket_pnl = []
    blocked_basket_wins = 0
    blocked_basket_losses = 0
    blocked_trades_detail = []
    for bar_idx, basket in duplicate_baskets:
        long_trades = [t for t in basket if t['direction'] == 'long']
        short_trades = [t for t in basket if t['direction'] == 'short']
        blocked = long_trades[cap_per_dir:] + short_trades[cap_per_dir:]
        for t in blocked:
            blocked_basket_pnl.append(t['pnl_dollars'])
            if t['pnl_dollars'] > 0:
                blocked_basket_wins += 1
            else:
                blocked_basket_losses += 1
            blocked_trades_detail.append({
                'bar_idx': int(bar_idx), 'variant': t['variant'],
                'direction': t['direction'], 'pnl_dollars': round(t['pnl_dollars'], 2),
                'exit_reason': t['exit_reason']
            })

    blocked_total = sum(blocked_basket_pnl)
    blocked_wr = blocked_basket_wins / max(1, len(blocked_basket_pnl))

    # Per-variant stats
    per_variant_stats = {}
    for v in variants:
        v_trades = [t for t in baseline_trades if t['variant'] == v['name']]
        per_variant_stats[v['name']] = stats(v_trades, v['name'])

    return {
        'baseline': base_stats,
        'capped': cap_stats,
        'duplicate_basket_summary': {
            'total_entry_bars': len(baskets),
            'duplicate_baskets_count': duplicate_count,
            'duplicate_baskets_pnl_total': round(duplicate_total_pnl, 2),
            'duplicate_baskets_all_winning': duplicate_winning_baskets,
            'avg_basket_size': round(sum(len(v) for v in baskets.values())
                                     / max(1, len(baskets)), 2),
        },
        'blocked_trades_summary': {
            'n_blocked': len(blocked_basket_pnl),
            'blocked_total_pnl': round(blocked_total, 2),
            'blocked_wins': blocked_basket_wins,
            'blocked_losses': blocked_basket_losses,
            'blocked_winrate': round(blocked_wr, 3),
            'blocked_detail': blocked_trades_detail[:30],
        },
        'pnl_delta_cap_vs_base': round(cap_stats['total_pnl'] - base_stats['total_pnl'], 2),
        'per_variant': per_variant_stats,
        'params': {
            'cap_per_dir': cap_per_dir,
            'n_variants': len(variants),
            'n_bars': n,
            'first_bar': str(bars.iloc[0].get('timestamp', bars.iloc[0].name)),
            'last_bar': str(bars.iloc[-1].get('timestamp', bars.iloc[-1].name)),
        }
    }