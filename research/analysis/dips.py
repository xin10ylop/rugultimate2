"""Liquidity-dip hypothesis: after a large non-agent, non-creator SELL that moves the price down by
>= X%, does the price rebound enough to pay costs? Entry = realistic late fill after the sell."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib")); sys.path.insert(0, os.path.dirname(__file__))
import numpy as np, polars as pl
from load import load_raw
from coins import build_tapes
from dense import outcomes, EXITS

AGENT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
D = load_raw(kinds=("trades", "creates"))
cr = D["creates"]; T = build_tapes(D["trades"], cr)
crd = {r["mint"]: r for r in cr.iter_rows(named=True)}
rng = np.random.default_rng(5)
rows = []
for m, c in T.items():
    info = crd.get(m)
    if info is None or len(c.slot) < 10:
        continue
    px = c.px; e = c.extra
    for j in range(1, len(px) - 1):
        if e["buy"][j] or e["user"][j] == AGENT or e["user"][j] == info["creator"]:
            continue
        drop = px[j] / px[j - 1] - 1
        if drop > -0.10:
            continue
        age = c.slot[j] - info["slot"]
        prior_max = px[:j].max()
        rows.append(dict(mint=m, slot=int(c.slot[j]), age=int(age), drop=float(drop), sell_sol=float(e["sol"][j] / 1e9),
                         dd=float(px[j] / prior_max - 1), mc=float(px[j] * 1e6), n_before=j, mayhem=info["mayhem"],
                         **outcomes(c, j, int(rng.integers(5, 14)), int(rng.integers(0, 3)))))
E = pl.DataFrame(rows)
print("large sells", E.height, "coins", E["mint"].n_unique())
E = E.with_columns(pl.col("drop").cut([-0.5, -0.3, -0.2, -0.15]).alias("drop_b"), pl.col("dd").cut([-0.8, -0.6, -0.4, -0.2]).alias("dd_b"))
rc = [f"r_{k}" for k in EXITS]
pl.Config.set_tbl_rows(30); pl.Config.set_tbl_cols(12)
for by in ("drop_b", "dd_b", "mayhem"):
    print(E.group_by(by).agg([pl.len()] + [pl.col(c).mean().round(4) for c in rc]).sort(by))
