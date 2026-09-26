#!/usr/bin/env python3
"""Per-strategy health audit for the 7 active strategies.
Computes: n_trades_30d, wins/losses, win_rate, net_pnl_30d, profit_factor,
max_consecutive_losers, days_since_last_fire, avg_R.
Also computes lifetime stats from trades table.
"""
import sqlite3
import json
from datetime import datetime, timezone

DB = "judas_crew.db"
NOW = datetime(2026, 9, 17, 15, 30, 4, tzinfo=timezone.utc)
THIRTY_DAYS_AGO = "2026-08-17T00:00:00Z"

# Active strategies from active_strategies table (state='active')
ACTIVE_IDS = [4582, 4591, 4599, 4603, 4607, 4612, 4613]

# Friendly names
NAMES = {
    4582: ("MNQ", "custom_5m", 21, "silver_bullet_pdh_pdl_retest_5m_mnq_roll12_tR15_v1"),
    4591: ("ZF",  "custom_15m", 1, "atr_disp_continuation_15m_zf_tR15_v1"),
    4599: ("MNQ", "custom_15m", 8, "silver_bullet_pdh_pdl_retest_15m_roll96_mnq_v2"),
    4603: ("MNQ", "custom_5m", 24, "atr_disp_continuation_5m_mnq_short_only_tR15_v1"),
    4607: ("ZF",  "custom_15m", 2, "atr_disp_continuation_strict_15m_zf_v1"),
    4612: ("MGC", "custom_15m", 8, "atr_disp_continuation_15m_mgc_v1"),
    4613: ("MGC", "custom_15m", 14, "atr_disp_continuation_strict_15m_mgc_v1"),
}

def max_consecutive_losses(pnls_chrono):
    """pnls_chrono: list of pnl_dollars in chronological order"""
    max_run = 0
    cur = 0
    for p in pnls_chrono:
        if p is None:
            continue
        if p < 0:
            cur += 1
            if cur > max_run:
                max_run = cur
        else:
            cur = 0
    return max_run

def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    rows_out = []

    for sid in ACTIVE_IDS:
        sym, fam, ver, name = NAMES[sid]

        # All trades (lifetime) for this strategy
        cur.execute("""
            SELECT pnl_dollars, opened_at, closed_at, status
            FROM trades
            WHERE strategy_id = ?
            ORDER BY COALESCE(closed_at, opened_at) ASC
        """, (sid,))
        all_trades = cur.fetchall()

        # 30d trades
        cur.execute("""
            SELECT pnl_dollars, opened_at, closed_at, status
            FROM trades
            WHERE strategy_id = ?
              AND closed_at >= ?
              AND status = 'closed'
            ORDER BY COALESCE(closed_at, opened_at) ASC
        """, (sid, THIRTY_DAYS_AGO))
        trades_30d = cur.fetchall()

        n_30 = len(trades_30d)
        wins = [t[0] for t in trades_30d if t[0] is not None and t[0] > 0]
        losses = [t[0] for t in trades_30d if t[0] is not None and t[0] < 0]
        n_wins = len(wins)
        n_losses = len(losses)
        n_open_30 = sum(1 for t in trades_30d if t[3] == 'open')
        win_rate = (n_wins / n_30) if n_30 > 0 else 0.0

        net_30 = sum(t[0] for t in trades_30d if t[0] is not None)
        sum_wins = sum(wins)
        sum_losses = sum(losses)
        pf = (sum_wins / abs(sum_losses)) if sum_losses != 0 else float('inf') if sum_wins > 0 else 0.0

        # Lifetime pnl (closed only)
        all_closed = [t for t in all_trades if t[3] == 'closed' and t[0] is not None]
        life_pnl = sum(t[0] for t in all_closed)
        life_n = len(all_closed)

        # Max consecutive losses
        pnls_chrono = [t[0] for t in trades_30d]
        mcl_30 = max_consecutive_losses(pnls_chrono)
        pnls_life = [t[0] for t in all_closed]
        mcl_life = max_consecutive_losses(pnls_life)

        # Avg R
        avg_win = (sum_wins / n_wins) if n_wins > 0 else 0.0
        avg_loss = (abs(sum_losses) / n_losses) if n_losses > 0 else 0.0
        avg_R = (avg_win / avg_loss) if avg_loss > 0 else 0.0

        # Days since last fire (use max(closed_at, opened_at) of all trades)
        if all_trades:
            latest_ts = max((t[2] or t[1]) for t in all_trades)
            try:
                dt = datetime.fromisoformat(latest_ts.replace('Z','+00:00'))
                days_since = (NOW - dt).total_seconds() / 86400.0
            except Exception:
                days_since = None
        else:
            days_since = None

        # Flags
        flags = []
        if net_30 < 0 and n_30 >= 20:
            flags.append("FLAG_A: 30d net<0 with >=20 trades")
        if mcl_30 >= 6:
            flags.append("FLAG_B: 30d MCL>=6")
        if days_since is not None and days_since >= 14:
            flags.append(f"FLAG_C: silent >=14d ({days_since:.1f}d)")
        if life_pnl < -500 and life_n >= 30:
            flags.append(f"FLAG_D: lifetime < -$500 with >=30 trades (${life_pnl:.0f})")
        if pf < 1.0 and n_30 >= 15:
            flags.append("FLAG_E: 30d PF<1 with >=15 trades")

        # Verdict
        if any(f.startswith("FLAG_D") for f in flags):
            verdict = "RETIRE"
            confidence = "HIGH"
        elif (any(f.startswith("FLAG_A") for f in flags) and
              any(f.startswith("FLAG_E") for f in flags)):
            verdict = "RETIRE"
            confidence = "HIGH"
        elif any(f.startswith(("FLAG_A","FLAG_B","FLAG_E")) for f in flags):
            verdict = "WATCH" if n_30 < 30 else "RETIRE"
            confidence = "MEDIUM"
        elif any(f.startswith("FLAG_C") for f in flags):
            verdict = "WATCH"
            confidence = "MEDIUM"
        else:
            verdict = "KEEP"
            confidence = "OK"

        # Estimated weekly savings if retired
        # 30d weekly prorate = net_30 / (30/7)
        weekly_pnl = net_30 / (30.0 / 7.0)
        est_weekly_savings = -weekly_pnl if net_30 < 0 else 0.0  # savings = loss avoided

        rows_out.append({
            "id": sid,
            "name": name,
            "symbol": sym,
            "family": fam,
            "version": ver,
            "n_30d": n_30,
            "n_open_30d": n_open_30,
            "n_wins": n_wins,
            "n_losses": n_losses,
            "win_rate": win_rate,
            "net_30d": net_30,
            "sum_wins": sum_wins,
            "sum_losses": sum_losses,
            "profit_factor": pf,
            "mcl_30d": mcl_30,
            "mcl_life": mcl_life,
            "avg_R": avg_R,
            "life_n": life_n,
            "life_pnl": life_pnl,
            "days_since_last_fire": days_since,
            "weekly_prorate": weekly_pnl,
            "est_weekly_savings": est_weekly_savings,
            "flags": flags,
            "verdict": verdict,
            "confidence": confidence,
        })

    conn.close()

    # Pretty print
    print(f"\n=== ACTIVE BOOK HEALTH AUDIT — 2026-09-17T15:30Z ===")
    print(f"Window: {THIRTY_DAYS_AGO} to now\n")
    hdr = f"{'ID':>5} {'Sym':<5} {'Family':<11} {'Ver':>3} {'N_30d':>6} {'W':>4} {'L':>4} {'WR':>5} {'Net_30d':>9} {'PF':>5} {'MCL30':>5} {'DaysSince':>9} {'Avg_R':>5} {'LifeNet':>9} {'Verdict':<7} {'Flags'}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows_out:
        wr = f"{r['win_rate']*100:.0f}%"
        pf = f"{r['profit_factor']:.2f}" if r['profit_factor'] != float('inf') else "inf"
        ds = f"{r['days_since_last_fire']:.1f}" if r['days_since_last_fire'] is not None else "N/A"
        ar = f"{r['avg_R']:.2f}"
        print(f"{r['id']:>5} {r['symbol']:<5} {r['family']:<11} {r['version']:>3} "
              f"{r['n_30d']:>6} {r['n_wins']:>4} {r['n_losses']:>4} {wr:>5} "
              f"${r['net_30d']:>8.2f} {pf:>5} {r['mcl_30d']:>5} {ds:>9} {ar:>5} "
              f"${r['life_pnl']:>8.2f} {r['verdict']:<7} {' | '.join(r['flags'])}")

    print()
    print("=== Summary ===")
    retire = [r for r in rows_out if r['verdict'] == 'RETIRE']
    watch = [r for r in rows_out if r['verdict'] == 'WATCH']
    keep = [r for r in rows_out if r['verdict'] == 'KEEP']
    print(f"RETIRE ({len(retire)}):")
    for r in retire:
        print(f"  #{r['id']} {r['symbol']} {r['family']} v{r['version']} ({r['name']})")
        print(f"    Flags: {' | '.join(r['flags'])}")
        print(f"    Est. weekly $-savings if retired: ${r['est_weekly_savings']:.2f}")
    print(f"WATCH ({len(watch)}):")
    for r in watch:
        print(f"  #{r['id']} {r['symbol']} {r['family']} v{r['version']} ({r['name']})")
        print(f"    Flags: {' | '.join(r['flags'])}")
    print(f"KEEP ({len(keep)}):")
    for r in keep:
        print(f"  #{r['id']} {r['symbol']} {r['family']} v{r['version']} ({r['name']})")

    # Save to JSON for the finding
    with open("scripts/audit_results.json", "w") as f:
        json.dump({"as_of": NOW.isoformat(), "rows": rows_out}, f, indent=2, default=str)

if __name__ == "__main__":
    main()
