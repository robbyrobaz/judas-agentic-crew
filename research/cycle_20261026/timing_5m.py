import sys, time, json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
from honest_screen import load_bars, metrics, run_books
CODE = (ROOT/"research/cycle_20261025/strategies/market_entry_ict_v1.py").read_text()
cfg = {"setup":"sweep_cont","lookback":24,"min_sweep_atr":0.30,"disp_k":1.0,"rr":1.5,
       "confirm_bars":6,"body_ratio_min":0.55,"stop_buf_atr":0.15,"atr_period":14}
for sym in ["MGC","MNQ","MCL","ZF","6J"]:
    bars = load_bars(sym,"5m",days=0)
    t0=time.time()
    b,lv,fa,meta = run_books(CODE,bars,cfg,sym)
    print(sym,"5m bars",len(bars),"wall",round(time.time()-t0,1),"s  live",json.dumps(metrics(lv)),"timed_out",meta.get("timed_out"))
