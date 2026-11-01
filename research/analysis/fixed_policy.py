"""Walk-forward with ONE fixed exit policy (no policy selection), entry at a given trigger age.
Gate: operator key's prior mean return under that same policy (matured launches only)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib")); sys.path.insert(0, os.path.dirname(__file__))
import numpy as np, polars as pl
from playbook import build_grid, HORIZON, POLICIES
F, R, W = build_grid()
F = F.with_columns(pl.col("name").str.to_lowercase().str.strip_chars().alias("name_key"),
                   (pl.col("funder") + ":" + pl.col("amt_key").cast(pl.Utf8)).alias("funder_amt")).sort("slot")
allR = np.array(list(R.values()))
print(f"launches with grid {len(R)}; unconditional mean of best fixed policy {allR.mean(0).max():+.4f}")
pols = [(0.2, 0.35, 140), (0.3, 0.35, 140), (0.5, 0.5, 560), (1.0, 0.5, 560), (0.1, 0.2, 47), (2.5, 0.95, 2800)]
for key in ("creator", "funder", "funder_amt", "name_key"):
    for pol in pols:
        pi = POLICIES.index(pol)
        for min_n, gate in ((3, 0.0), (5, 0.05)):
            hist = {}; out = []
            for r in F.iter_rows(named=True):
                m = r["mint"]; k = r[key]
                if m not in R or k is None: continue
                prev = [R[pm][pi] for ps, pm in hist.get(k, []) if ps + HORIZON <= r["slot"]]
                if len(prev) >= min_n and np.mean(prev) > gate:
                    out.append((r["slot"], R[m][pi]))
                hist.setdefault(k, []).append((r["slot"], m))
            if len(out) >= 20:
                a = np.array([x[1] for x in out]); h = len(a) // 2
                print(f"{key:10s} pol={pol} n>={min_n} g={gate:.2f}: trades={len(a):4d} mean={a.mean():+.4f} win={(a>0).mean():.2f} h1={a[:h].mean():+.4f} h2={a[h:].mean():+.4f}")
