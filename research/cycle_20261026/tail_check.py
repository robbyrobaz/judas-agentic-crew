import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]; sys.path.insert(0,str(ROOT))
from src import bar_archive
PER={"5m":288,"15m":96,"1h":24}
for sym,tf,days in (("MGC","15m",60),("MCL","15m",30),("MNQ","15m",45),("ZF","15m",30),("6J","15m",30),
                    ("MGC","5m",45),("MNQ","5m",30),("MCL","5m",15),("6J","5m",15),("ZF","5m",15)):
    df=bar_archive.read_archive(sym,tf)
    n=PER[tf]*days
    w=df.tail(min(n,len(df))).reset_index(drop=True)
    rng=w["high"]-w["low"]
    dirty=int(((rng<=0)|(w["volume"]<=0)).sum())
    print(f"{sym:4s} {tf:3s} days={days:3d} window_bars={len(w):6d} dirty={dirty:6d} "
          f"({100.0*dirty/len(w):.1f}%) span {w['ts'].iloc[0]} -> {w['ts'].iloc[-1]}")
