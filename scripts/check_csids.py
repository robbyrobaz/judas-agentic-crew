import sqlite3, json
conn = sqlite3.connect('judas_crew.db')
ids = [237, 270, 269, 261, 203, 333, 276, 262, 194, 250, 231, 191, 295, 332, 253, 271, 229, 257, 263, 225]

print("=== custom_strategies ===")
for r in conn.execute(f"SELECT id, name, symbol FROM custom_strategies WHERE id IN ({','.join(str(i) for i in ids)}) ORDER BY id").fetchall():
    print(r)

print()
print("=== latest 30 research_experiments ===")
for r in conn.execute("SELECT id, ts_utc, symbol, experiment_type, name, status FROM research_experiments ORDER BY id DESC LIMIT 30").fetchall():
    print(r)