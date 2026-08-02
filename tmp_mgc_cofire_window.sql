-- Co-fire within +-N bars window (treat as same session overlap)
WITH sig AS (
  SELECT strategy_id, strftime('%Y-%m-%dT%H:%M:00Z', ts_utc) AS bar
  FROM signals
  WHERE symbol='MGC' AND ts_utc >= datetime('now', '-14 days')
    AND risk_decision='TRADE'
),
bars AS (
  SELECT strategy_id, datetime(bar) AS bar_dt FROM sig
)
SELECT x.strategy_id AS a_id, y.strategy_id AS b_id,
       (SELECT COUNT(DISTINCT bar_dt) FROM bars WHERE strategy_id=x.strategy_id) AS a_bars,
       (SELECT COUNT(DISTINCT bar_dt) FROM bars WHERE strategy_id=y.strategy_id) AS b_bars,
       (WITH RECURSIVE window(b) AS (
         SELECT DISTINCT bar_dt FROM bars WHERE strategy_id=x.strategy_id
         UNION
         SELECT datetime(b, '+10 minutes') FROM window WHERE datetime(b, '+10 minutes') <= datetime('now')
       )
       SELECT COUNT(*) FROM window w WHERE EXISTS (SELECT 1 FROM bars WHERE strategy_id=y.strategy_id AND bar_dt = w.b)) AS co_fires_10min
FROM (SELECT DISTINCT strategy_id FROM bars) x
JOIN (SELECT DISTINCT strategy_id FROM bars) y ON x.strategy_id < y.strategy_id
WHERE (SELECT COUNT(DISTINCT bar_dt) FROM bars WHERE strategy_id=x.strategy_id) >= 3
  AND (SELECT COUNT(DISTINCT bar_dt) FROM bars WHERE strategy_id=y.strategy_id) >= 3
ORDER BY co_fires_10min DESC
LIMIT 30;
