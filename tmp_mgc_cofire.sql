WITH sig AS (
  SELECT strategy_id, strftime('%Y-%m-%dT%H:%M:00Z', ts_utc) AS bar
  FROM signals
  WHERE symbol='MGC' AND ts_utc >= datetime('now', '-14 days')
    AND risk_decision='TRADE'
)
SELECT a_id, b_id, a_bars, b_bars, co_fires,
       ROUND(100.0 * co_fires / a_bars, 1) AS pct_of_a,
       ROUND(100.0 * co_fires / b_bars, 1) AS pct_of_b
FROM (
  SELECT x.strategy_id AS a_id, y.strategy_id AS b_id,
         (SELECT COUNT(DISTINCT bar) FROM sig WHERE strategy_id=x.strategy_id) AS a_bars,
         (SELECT COUNT(DISTINCT bar) FROM sig WHERE strategy_id=y.strategy_id) AS b_bars,
         (SELECT COUNT(*) FROM (SELECT DISTINCT bar FROM sig WHERE strategy_id=x.strategy_id INTERSECT SELECT DISTINCT bar FROM sig WHERE strategy_id=y.strategy_id)) AS co_fires
  FROM (SELECT DISTINCT strategy_id FROM sig) x
  JOIN (SELECT DISTINCT strategy_id FROM sig) y ON x.strategy_id < y.strategy_id
)
WHERE co_fires > 0
ORDER BY co_fires DESC, pct_of_a DESC
LIMIT 35;
