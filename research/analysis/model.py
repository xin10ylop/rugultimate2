"""Time-split model on the dense table: can any regime beat costs out of sample?

Train on the earliest TRAIN_FRAC of slots, test on the rest (no shuffling, a gap between them).
Strategy evaluation on test: per coin, take only the FIRST trigger whose prediction exceeds the
threshold (one position per coin), and report realised net returns of those trades.
"""
import sys, os
import numpy as np, polars as pl
import lightgbm as lgb

D = pl.read_parquet(os.path.join(os.path.dirname(__file__), "..", "..", "data", "derived", "dense.parquet"))
TARGETS = [c for c in D.columns if c.startswith("r_")]
FEATS = [c for c in D.columns if c not in TARGETS and c not in ("mint", "slot")]
TRAIN_FRAC = float(os.environ.get("TRAIN_FRAC", "0.6"))
D = D.sort("slot")
cut = D["slot"].quantile(TRAIN_FRAC); gap = 3000
tr = D.filter(pl.col("slot") < cut - gap); te = D.filter(pl.col("slot") >= cut)
print(f"rows train={tr.height} test={te.height} coins test={te['mint'].n_unique()}  features={len(FEATS)}")
Xtr = tr.select(FEATS).to_numpy().astype(np.float32); Xte = te.select(FEATS).to_numpy().astype(np.float32)
for tgt in sys.argv[1:] or TARGETS:
    ytr = np.clip(tr[tgt].to_numpy(), -1, 3); yte = te[tgt].to_numpy()
    m = lgb.LGBMRegressor(n_estimators=300, learning_rate=0.03, num_leaves=31, min_child_samples=200,
                          subsample=0.8, subsample_freq=1, colsample_bytree=0.8, verbose=-1)
    m.fit(Xtr, ytr)
    p = m.predict(Xte)
    corr = np.corrcoef(p, yte)[0, 1]
    out = [f"{tgt:14s} base test={yte.mean():+.4f} corr={corr:+.3f}"]
    for q in (0.9, 0.97, 0.99, 0.997):
        th = np.quantile(m.predict(Xtr), q)
        sel = te.with_columns(pl.Series("p", p)).filter(pl.col("p") > th)
        first = sel.sort("slot").group_by("mint").first()
        if first.height:
            out.append(f"q{q}: n={first.height} mean={first[tgt].mean():+.4f} win={(first[tgt]>0).mean():.2f}")
    print(" | ".join(out))
    if tgt == (sys.argv[1:] or TARGETS)[0]:
        imp = sorted(zip(m.feature_importances_, FEATS), reverse=True)[:12]
        print("   top features:", ", ".join(f"{n}({v})" for v, n in imp))
