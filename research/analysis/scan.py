"""Univariate scan: quintile bins of each feature vs realistic-fill returns, first/second half split."""
import sys, os
import numpy as np, polars as pl
D = os.environ.get("RUG_OUT", os.path.join(os.path.dirname(__file__), "..", "..", "data", "derived"))
E = pl.read_parquet(os.path.join(D, "events.parquet"))
trig = sys.argv[1] if len(sys.argv) > 1 else "age2"
ycol = sys.argv[2] if len(sys.argv) > 2 else "r_tp50_sl30_120s"
sub = sys.argv[3] if len(sys.argv) > 3 else "all"
e = E.filter(pl.col("trig") == trig)
if sub == "mayhem": e = e.filter(pl.col("mayhem") == 1)
elif sub == "normal": e = e.filter(pl.col("mayhem") == 0)
med = e["slot0"].median()
skip = {"mint", "creator", "trig", "slot0", "trig_slot", "bt", "landed"}
feats = [c for c in e.columns if c not in skip and not c.startswith(("r_", "rw_", "mfe", "mae", "slip"))]
print(f"trig={trig} y={ycol} sub={sub} n={e.height} mean y={e[ycol].mean():+.4f}")
res = []
for f in feats:
    x = e[f].cast(pl.Float64)
    if x.n_unique() < 3: continue
    try:
        b = e.with_columns(x.rank("random").alias("_r")).with_columns((pl.col("_r") * 5 / (e.height + 1)).floor().alias("_q"))
    except Exception:
        continue
    g = b.group_by("_q").agg(pl.col(ycol).mean().alias("y"), pl.len().alias("n"),
                             pl.col(ycol).filter(pl.col("slot0") <= med).mean().alias("y1"),
                             pl.col(ycol).filter(pl.col("slot0") > med).mean().alias("y2"),
                             pl.col(f).min().alias("lo"), pl.col(f).max().alias("hi")).sort("_q")
    ys = g["y"].to_numpy()
    best = int(np.nanargmax(ys))
    r = g.row(best, named=True)
    spread = np.nanmax(ys) - np.nanmin(ys)
    res.append((f, spread, best, r["y"], r["y1"], r["y2"], r["lo"], r["hi"], r["n"]))
res.sort(key=lambda z: -z[1])
print(f"{'feature':16s} {'spread':>7s} {'bestQ':>5s} {'y_best':>7s} {'half1':>7s} {'half2':>7s}  range")
for f, sp, bq, yb, y1, y2, lo, hi, n in res[:25]:
    print(f"{f:16s} {sp:7.3f} {bq:5d} {yb:+7.3f} {y1 if y1 is not None else float('nan'):+7.3f} {y2 if y2 is not None else float('nan'):+7.3f}  [{lo:.4g}, {hi:.4g}] n={n}")
