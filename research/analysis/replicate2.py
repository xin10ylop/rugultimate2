import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import numpy as np, polars as pl
from load import load_raw
from coins import build_tapes
from sim import landing_rows, buy_tokens, Costs
D = load_raw(kinds=("trades", "creates", "completes"))
tr, cr = D["trades"], D["creates"]
DD = os.path.join(os.path.dirname(__file__), "..", "..", "data", "derived")
g = pl.read_parquet(os.path.join(DD, "wallet_coin_pnl.parquet"))
closed = g.join(D["completes"].select("mint").unique(), on="mint", how="anti").filter(pl.col("rem_val") / 1e9 <= 0.1 * pl.col("cost_sol"))
med = closed["first_slot"].median()
A = closed.filter(pl.col("first_slot") <= med).group_by("user").agg(pl.len().alias("n"), pl.col("pnl").sum().alias("pnl"),
        pl.col("cost_sol").sum().alias("vol"), (pl.col("first_rel") >= 2).mean().alias("late"))
sk = set(A.filter((pl.col("late") >= 0.8) & (pl.col("n") >= 10) & (pl.col("pnl") / pl.col("vol") > 0.1))["user"].to_list())
lo = set(A.filter((pl.col("late") >= 0.8) & (pl.col("n") >= 10) & (pl.col("pnl") < 0))["user"].to_list())
# operator concentration: share of a wallet's coins coming from its single most-traded funder / creator
F = pl.read_parquet(os.path.join(DD, "launches.parquet")).select(["mint", "creator", "funder"])
gc = g.join(F, on="mint", how="left")
def conc(users, col):
    x = gc.filter(pl.col("user").is_in(list(users)) & pl.col(col).is_not_null()).group_by(["user", col]).len()
    tot = x.group_by("user").agg(pl.col("len").sum().alias("t"), pl.col("len").max().alias("m"))
    return (tot["m"] / tot["t"]).median()
for lab, u in (("skilled", sk), ("losers", lo)):
    print(f"{lab}: median share of coins from top funder={conc(u,'funder'):.2f} top creator={conc(u,'creator'):.2f}")
T = build_tapes(tr, cr); co = Costs()
def sellq(vs, vt, t, fr):
    k = vs * vt; return (vs - k / (vt + t)) / 1e9 * (1 - fr)
rows = []; rng = np.random.default_rng(9)
pairs = closed.filter(pl.col("user").is_in(list(sk)) & (pl.col("first_rel") >= 2) & (pl.col("first_slot") > med)).select(["user", "mint", "pnl", "cost_sol"])
for r in pairs.iter_rows(named=True):
    c = T.get(r["mint"])
    if c is None: continue
    u = c.extra["user"]; b = c.extra["buy"]; tok = c.extra["tok"].astype(float)
    mine = np.nonzero(u == r["user"])[0]
    ib = [i for i in mine if b[i]]; isl = [i for i in mine if not b[i]]
    if not ib or not isl: continue
    sold_total = tok[isl].sum()
    out = {}
    for mode in ("before", "after"):
        vals = []
        for var, m in (("same", int(rng.integers(5, 14))), ("next", int(rng.integers(0, 3)))):
            jb = ib[0] - 1 if mode == "before" else ib[0]
            e = max(jb, 0) if mode == "before" else landing_rows(c, jb, var, m)
            toks = buy_tokens(c.vsol[e], c.vtok[e], 0.25, c.fee_rate)
            proceeds = 0.0
            for i in isl:
                frac = tok[i] / sold_total
                x = (i - 1) if mode == "before" else landing_rows(c, i, var, m)
                proceeds += sellq(c.vsol[x], c.vtok[x], toks * frac, c.fee_rate)
            vals.append((proceeds - 0.25 - co.buy_fixed - co.sell_fixed * len(isl)) / 0.25)
            if mode == "before": break
        out[mode] = float(np.mean(vals))
    rows.append(dict(their=r["pnl"] / r["cost_sol"], **out))
R = pl.DataFrame(rows)
print(f"H2 skilled round trips n={R.height}: their mean={R['their'].mean():+.4f} med={R['their'].median():+.4f} | "
      f"us taking their exact place (optimistic) mean={R['before'].mean():+.4f} med={R['before'].median():+.4f} | "
      f"us landing after them (realistic) mean={R['after'].mean():+.4f} med={R['after'].median():+.4f} win={(R['after']>0).mean():.2f}")
