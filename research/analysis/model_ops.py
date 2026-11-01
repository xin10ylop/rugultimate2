"""Dense model + point-in-time operator-history features (prior rug/dead/MFE per creator, funder, name)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import numpy as np, polars as pl, lightgbm as lgb
from operators import track_records
DD = os.path.join(os.path.dirname(__file__), "..", "..", "data", "derived")
E = pl.read_parquet(os.path.join(DD, "events.parquet")).filter(pl.col("trig") == "age2")
F = pl.read_parquet(os.path.join(DD, "launches.parquet")).with_columns(pl.col("name").str.to_lowercase().str.strip_chars().alias("name_key"))
o = E.select(["mint", "mfe_2m", "mae_2m"]).with_columns((pl.col("mfe_2m") < 0.05).cast(pl.Float64).alias("dead"),
        (pl.col("mae_2m") < -0.5).cast(pl.Float64).alias("rug"), pl.col("mfe_2m").clip(-1, 50).log1p().alias("lmfe"))
L = track_records(F, o.select(["mint", "dead", "rug", "lmfe"]), keys=("creator", "funder", "name_key"), horizon_slots=700, metrics=("dead", "rug", "lmfe"))
opf = [c for c in L.columns if c.endswith(("_n_any", "_n_ev")) or "_prev_" in c]
L = L.select(["mint", "fund_lam", "fund_lead_s", "fresh"] + opf).with_columns(pl.col("fresh").cast(pl.Float64))
D = pl.read_parquet(os.path.join(DD, "dense.parquet")).join(L, on="mint", how="left")
TG = [c for c in D.columns if c.startswith("r_")]
FE = [c for c in D.columns if c not in TG and c not in ("mint", "slot")]
D = D.sort("slot"); cut = D["slot"].quantile(0.6)
tr = D.filter(pl.col("slot") < cut - 3000); te = D.filter(pl.col("slot") >= cut)
Xtr = tr.select(FE).to_numpy().astype(np.float32); Xte = te.select(FE).to_numpy().astype(np.float32)
print(f"train={tr.height} test={te.height} feats={len(FE)} (operator feats={len(opf)+3})")
for tgt in TG:
    ytr = np.clip(tr[tgt].to_numpy(), -1, 3); yte = te[tgt].to_numpy()
    m = lgb.LGBMRegressor(n_estimators=300, learning_rate=0.03, num_leaves=31, min_child_samples=200, subsample=0.8,
                          subsample_freq=1, colsample_bytree=0.8, verbose=-1).fit(Xtr, ytr)
    p = m.predict(Xte); ptr = m.predict(Xtr)
    out = [f"{tgt:14s} base={yte.mean():+.4f} corr={np.corrcoef(p, yte)[0,1]:+.3f}"]
    for q in (0.9, 0.97, 0.99):
        sel = te.with_columns(pl.Series("p", p)).filter(pl.col("p") > np.quantile(ptr, q)).sort("slot").group_by("mint").first()
        if sel.height: out.append(f"q{q}: n={sel.height} mean={sel[tgt].mean():+.4f} med={sel[tgt].median():+.4f} win={(sel[tgt]>0).mean():.2f}")
    print(" | ".join(out))
    if tgt == "r_tp15sl15_30s":
        print("   top:", ", ".join(f"{n}({v})" for v, n in sorted(zip(m.feature_importances_, FE), reverse=True)[:12]))
