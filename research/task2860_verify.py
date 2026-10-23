"""Sanity checks: signal geometry + a hand-verified worked example."""
import sqlite3
import numpy as np
import pandas as pd

con = sqlite3.connect("judas_crew.db")
s = pd.read_sql("""SELECT id, ts_utc, symbol, direction, entry, stop, target, strategy_id,
                          strategy_family FROM signals
                   WHERE ts_utc>='2026-10-12' AND ts_utc<'2026-10-22'
                   AND entry IS NOT NULL AND stop IS NOT NULL AND target IS NOT NULL""",
                con)
con.close()
d = np.where(s.direction == "long", 1, -1)
risk = d * (s.entry - s.stop)          # >0 means stop on the correct side
rew = d * (s.target - s.entry)         # >0 means target on the correct side
print("stop on correct side :", f"{(risk>0).mean():.4%}")
print("target on correct side:", f"{(rew>0).mean():.4%}")
print("reward/risk ratio: median %.2f  mean %.2f  p10 %.2f p90 %.2f" %
      (np.median(rew / risk), np.mean(rew / risk),
       np.nanpercentile(rew / risk, 10), np.nanpercentile(rew / risk, 90)))
ok = (risk > 0) & (rew > 0)
print("both-sane rows:", int(ok.sum()), "of", len(s))
b = pd.read_parquet("research/task2860_shadow_honest.parquet")
b = b.merge(s[["id", "risk"]].rename(columns={"id": "signal_id", "risk": "risk_pts_sig"}),
            on="signal_id", how="left")
print("\nHONEST realised risk vs signal risk (pts): "
      "median ratio %.3f" % np.median((b.risk_pts / b.risk_pts_sig).abs()))

print("\n" + "=" * 100)
print("HAND-VERIFIED EXAMPLE — #4634 MNQ 1h silver bullet (BT PF 5.45, +$31,064)")
print("=" * 100)
ids = pd.read_sql("SELECT id FROM signals WHERE strategy_id=4634 "
                  "AND ts_utc>='2026-10-12' AND ts_utc<'2026-10-22' ORDER BY ts_utc", con)
H = pd.read_parquet("research/task2860_shadow_honest.parquet")
R = pd.read_parquet("research/task2860_shadow_retro.parquet")
L = pd.read_parquet("research/task2860_shadow_limit.parquet")
sid = int(ids.id.iloc[0])
row = s[s.id == sid].iloc[0]
print(f"signal {sid}  {row.ts_utc}  {row.symbol} {row.direction}  strategy {row.strategy_id}")
print(f"  entry={row.entry}  stop={row.stop}  target={row.target}  R:R={(row.target-row.entry)/(row.entry-row.stop):.2f}")
bars = pd.read_parquet(f"cache_1h/{row.symbol}_5m.parquet")
bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
t = pd.Timestamp(row.ts_utc)
last_closed = bars[bars.ts + pd.Timedelta("5min") <= t].iloc[-1]
print(f"  last fully-closed 5m bar: {last_closed.ts}  O={last_closed.open} "
      f"H={last_closed.high} L={last_closed.low} C={last_closed.close}")
print(f"  entry sits BELOW the closed bar (long, must return down): "
      f"{row.entry} < low {last_closed.low} -> {row.entry < last_closed.low}")
nxt = bars[bars.ts > last_closed.ts].head(4)
print("  next bars after the signal:")
print(nxt[["ts", "open", "high", "low", "close"]].to_string(index=False))
for tag, dfr in (("HONEST", H), ("RETRO", R), ("LIMIT", L)):
    x = dfr[dfr.signal_id == sid]
    if len(x):
        x = x.iloc[0]
        print(f"  {tag:<7} fill={x.fill:<10} exit={x.exit_px:<10} res={x.res:<5} R={x.r:+.3f}")
    else:
        print(f"  {tag:<7} no fill")