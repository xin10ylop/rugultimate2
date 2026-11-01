"""Does an operator's past (point-in-time) performance predict its next launch?"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import numpy as np, polars as pl
from operators import track_records

D = os.environ.get("RUG_OUT", os.path.join(os.path.dirname(__file__), "..", "..", "data", "derived"))
E = pl.read_parquet(os.path.join(D, "events.parquet")); F = pl.read_parquet(os.path.join(D, "launches.parquet"))
trig = sys.argv[1] if len(sys.argv) > 1 else "age2"
ycol = sys.argv[2] if len(sys.argv) > 2 else "r_tp20_sl20_30s"
o = E.filter(pl.col("trig") == trig).select(["mint", pl.col(ycol).alias("y"), "mfe_2m", "mae_2m"])
o = o.with_columns((pl.col("mfe_2m") < 0.05).cast(pl.Float64).alias("dead"))
L = track_records(F, o, keys=("funder", "creator", "upstream"), horizon_slots=700, metrics=("y", "mfe_2m", "dead"))
L = L.filter(pl.col("y").is_not_null())
print(f"launches with outcome: {L.height}, trig={trig}, y={ycol}, overall mean y={L['y'].mean():.4f}")
for k in ("funder", "creator", "upstream"):
    s = L.filter(pl.col(f"{k}_n_ev") >= 2)
    if s.height < 10:
        print(k, "too few", s.height); continue
    x = s[f"{k}_prev_y"].to_numpy(); y = s["y"].to_numpy()
    r = np.corrcoef(x, y)[0, 1]
    good = s.filter(pl.col(f"{k}_prev_y") > 0); bad = s.filter(pl.col(f"{k}_prev_y") <= 0)
    print(f"{k:9s} n={s.height:5d} corr(prev_y,y)={r:+.3f} | prev>0: n={good.height} mean y={good['y'].mean() if good.height else float('nan'):+.4f}"
          f" | prev<=0: n={bad.height} mean y={bad['y'].mean() if bad.height else float('nan'):+.4f}"
          f" | distinct keys={s[k].n_unique()}")
    dd = s.filter(pl.col(f"{k}_prev_dead") >= 0.8)
    print(f"          prev mostly dead (>=80%): n={dd.height} mean y={dd['y'].mean() if dd.height else float('nan'):+.4f} dead_now={dd['dead'].mean() if dd.height else float('nan'):.2f}")
