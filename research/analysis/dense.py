"""Dense event study: triggers throughout each coin's life, flow features, realistic-fill outcomes.

Writes data/derived/dense.parquet. Point-in-time: features at trigger row j use rows <= j only;
wallet attributes (fresh funding, prior activity) only use information from earlier slots.
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import numpy as np, polars as pl
from load import load_raw, load_transfers
from sim import Coin, Costs, landing_rows, buy_tokens, sell_sol

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "data", "derived")
AGENT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
SIZE = 0.25
CO = Costs()
EXITS = {"t10s": (99, 0.99, 47), "t30s": (99, 0.99, 140), "t2m": (99, 0.99, 560),
         "tp15sl15_30s": (0.15, 0.15, 140), "tp30sl20_60s": (0.30, 0.20, 280), "tp60sl30_2m": (0.60, 0.30, 560)}


def outcomes(c, j, m_same, m_next):
    res = {}
    n = len(c.slot)
    for variant, m in (("same", m_same), ("next", m_next)):
        e = landing_rows(c, j, variant, m)
        if c.px[e] > c.px[j] * (1 + CO.max_slip):
            for k in EXITS:
                res.setdefault(k, []).append(-CO.fail_fixed / SIZE)
            continue
        toks = buy_tokens(c.vsol[e], c.vtok[e], SIZE, c.fee_rate)
        ref = SIZE * 1e9 / toks
        rel = c.px[e + 1:] / ref
        ds = c.slot[e + 1:] - c.slot[e]
        for k, (tp, sl, tm) in EXITS.items():
            if len(rel):
                hit = (rel >= 1 + tp) | (rel <= 1 - sl) | (ds >= tm)
                q = int(np.argmax(hit))
                r = e + 1 + q if hit[q] else n - 1
            else:
                r = n - 1
            xr = r if r >= n - 1 else landing_rows(c, r, variant, m)
            res.setdefault(k, []).append((sell_sol(c.vsol[xr], c.vtok[xr], toks, c.fee_rate) - SIZE - CO.buy_fixed - CO.sell_fixed) / SIZE)
    return {f"r_{k}": float(np.mean(v)) for k, v in res.items()}


def main(min_gap=5, min_age=5):
    t0 = time.time()
    D = load_raw(kinds=("trades", "creates"))
    tr, cr = D["trades"], D["creates"]
    X = load_transfers()
    # wallet fresh-funding slot (first transfer into a wallet with zero pre-balance)
    fresh = X.filter(pl.col("pre_to") == 0).group_by("to").agg(pl.col("slot").min().alias("fresh_slot"))
    tr = tr.filter((pl.col("sol") > 0) & pl.col("quote").is_null()).sort(["slot", "idx", "k"])
    tr = tr.with_columns(pl.int_range(pl.len()).over("user").alias("user_prior"))  # prior trades by this wallet
    tr = tr.join(fresh, left_on="user", right_on="to", how="left")
    tr = tr.with_columns(((pl.col("fresh_slot").is_not_null()) & (pl.col("slot") - pl.col("fresh_slot") < 8400) &
                          (pl.col("fresh_slot") <= pl.col("slot"))).alias("ufresh"))
    tr = tr.join(cr.select(["mint", pl.col("slot").alias("s0"), "creator", "mayhem"]), on="mint", how="inner").sort(["mint", "slot", "idx", "k"])
    print("prep", f"{time.time()-t0:.0f}s", tr.shape, flush=True)
    rng = np.random.default_rng(3)
    rows = []
    for (mint,), g in tr.group_by(["mint"], maintain_order=True):
        n = g.height
        if n < 8:
            continue
        slot = g["slot"].to_numpy(); s0 = int(g["s0"][0]); creator = g["creator"][0]; mayhem = g["mayhem"][0]
        vsol = g["vsol"].to_numpy().astype(float); vtok = g["vtok"].to_numpy().astype(float)
        buy = g["buy"].to_numpy(); sol = g["sol"].to_numpy() / 1e9; user = g["user"].to_numpy()
        isag = user == AGENT; isdev = user == creator
        uf = g["ufresh"].to_numpy(); up = g["user_prior"].to_numpy(); rsol = g["rsol"].to_numpy() / 1e9
        fr = ((g["fee"] + g["cfee"]) / g["sol"]).filter(~pl.Series(isag)).median()
        c = Coin(mint=mint, slot=slot, idx=g["idx"].to_numpy(), vsol=vsol, vtok=vtok,
                 fee_rate=float(fr) if fr is not None and np.isfinite(fr) else 0.0125)
        px = vsol / vtok; mc = px * 1e6
        bsol = np.where(buy & ~isag, sol, 0.0); ssol = np.where(~buy & ~isag, sol, 0.0)
        cb = np.cumsum(bsol); cs = np.cumsum(ssol); cnb = np.cumsum(buy & ~isag); cns = np.cumsum(~buy & ~isag)
        cfb = np.cumsum(np.where(uf & buy, sol, 0.0)); cdevs = np.cumsum(isdev & ~buy)
        cnewb = np.cumsum(np.where((up < 3) & buy & ~isag, 1, 0)); cag = np.cumsum(isag)
        mcmax = np.maximum.accumulate(mc)
        last = -10**9
        for j in range(n):
            age = slot[j] - s0
            if age < min_age or slot[j] - last < min_gap:
                continue
            if j + 1 >= n:
                break
            last = slot[j]
            f = dict(mint=mint, slot=int(slot[j]), age=int(age), mayhem=mayhem, mc=float(mc[j]), rsol=float(rsol[j]),
                     dd=float(mc[j] / mcmax[j] - 1), mcmax=float(mcmax[j]), n_tr=j + 1,
                     dev_sold=int(cdevs[j] > 0), agent_frac=float(cag[j] / (j + 1)))
            for w in (10, 50, 250):
                k0 = np.searchsorted(slot, slot[j] - w, side="right")  # first row inside window
                pre = k0 - 1
                def win(a):
                    return a[j] - (a[pre] if pre >= 0 else 0.0)
                f[f"buy_{w}"] = float(win(cb)); f[f"sell_{w}"] = float(win(cs))
                f[f"nb_{w}"] = int(win(cnb)); f[f"ns_{w}"] = int(win(cns))
                f[f"fresh_buy_{w}"] = float(win(cfb)); f[f"newb_{w}"] = int(win(cnewb))
                f[f"ret_{w}"] = float(px[j] / px[pre] - 1) if pre >= 0 else float(px[j] / (30 / 1073) * 1e-6 - 1)
                ub = set(user[k0:j + 1][buy[k0:j + 1] & ~isag[k0:j + 1]])
                f[f"ub_{w}"] = len(ub)
            f.update(outcomes(c, j, int(rng.integers(5, 14)), int(rng.integers(0, 3))))
            rows.append(f)
    D = pl.DataFrame(rows)
    D.write_parquet(os.path.join(OUT, "dense.parquet"))
    print("dense", D.shape, f"{time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
