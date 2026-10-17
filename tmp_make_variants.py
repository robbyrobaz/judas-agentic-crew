"""Prepare variant code strings — bakes new defaults into CSID 270 and 229."""
import json
import sqlite3, os
db_path = os.environ.get("JUDAS_DB", "judas_crew.db")
conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row

csids = {}
for csid in [270, 229]:
    row = conn.execute("SELECT code FROM custom_strategies WHERE id=?", (csid,)).fetchone()
    csids[csid] = row["code"]
conn.close()


# Variant A: CSID 270 with rolling_pdhl_bars=24 (default was 12)
code_270_roll24 = csids[270].replace(
    "rolling_pdhl_bars\", 12)", "rolling_pdhl_bars\", 24)", 1
)
assert "rolling_pdhl_bars\", 24)" in code_270_roll24, "Failed to substitute rolling_pdhl_bars=24"

# Variant B: CSID 270 with rolling_pdhl_bars=36
code_270_roll36 = csids[270].replace(
    "rolling_pdhl_bars\", 12)", "rolling_pdhl_bars\", 36)", 1
)
assert "rolling_pdhl_bars\", 36)" in code_270_roll36, "Failed to substitute rolling_pdhl_bars=36"

# Variant C: CSID 229 MGC loose3 (target_r=2.5, stop_buf=8, break_thr=5, rolling=12)
code_229_loose3 = csids[229]
code_229_loose3 = code_229_loose3.replace(
    'float(params.get("target_r", 2.0))',
    'float(params.get("target_r", 2.5))', 1
)
code_229_loose3 = code_229_loose3.replace(
    'float(params.get("stop_buf_ticks", 6))',
    'float(params.get("stop_buf_ticks", 8))', 1
)
code_229_loose3 = code_229_loose3.replace(
    'float(params.get("break_threshold_ticks", 4))',
    'float(params.get("break_threshold_ticks", 5))', 1
)
code_229_loose3 = code_229_loose3.replace(
    'int(params.get("rolling_window", 24))',
    'int(params.get("rolling_window", 12))', 1
)
assert 'float(params.get("target_r", 2.5))' in code_229_loose3

# Variant D: CSID 229 MGC loose1 (target_r=3.0, stop_buf=8, break_thr=6, max_retest=72, rolling=12)
code_229_loose1 = csids[229]
code_229_loose1 = code_229_loose1.replace(
    'float(params.get("target_r", 2.0))',
    'float(params.get("target_r", 3.0))', 1
)
code_229_loose1 = code_229_loose1.replace(
    'float(params.get("stop_buf_ticks", 6))',
    'float(params.get("stop_buf_ticks", 8))', 1
)
code_229_loose1 = code_229_loose1.replace(
    'float(params.get("break_threshold_ticks", 4))',
    'float(params.get("break_threshold_ticks", 6))', 1
)
code_229_loose1 = code_229_loose1.replace(
    'int(params.get("max_retest_bars", 36))',
    'int(params.get("max_retest_bars", 72))', 1
)
code_229_loose1 = code_229_loose1.replace(
    'int(params.get("rolling_window", 24))',
    'int(params.get("rolling_window", 12))', 1
)
assert 'float(params.get("target_r", 3.0))' in code_229_loose1
assert 'int(params.get("max_retest_bars", 72))' in code_229_loose1

with open('tmp_variant_codes.json', 'w') as f:
    json.dump({
        'code_270_roll24': code_270_roll24,
        'code_270_roll36': code_270_roll36,
        'code_229_loose3': code_229_loose3,
        'code_229_loose1': code_229_loose1,
    }, f)
print("Wrote 4 variant code strings to tmp_variant_codes.json")