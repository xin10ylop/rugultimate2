"""PumpSwap microstructure on graduated coins with realistic AMM fills.

Re-decodes swap events with fee tiers, keeps SOL-quoted pools, and tests simple, economically
motivated triggers (large buy / large sell / recent run-up / drawdown) with never-first fills.
Costs: per-pool total fee (LP + protocol + creator) on each side, fixed per-tx cost, own impact.
"""
import sys, os, base64, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import numpy as np, polars as pl, base58
from load import files, _iter_gz, DATA

WSOL = "So11111111111111111111111111111111111111112"
OUT = os.path.join(DATA, "derived", "pswap.parquet")


def decode_all():
    rows = []
    for path in files("pump_tx"):
        for r in _iter_gz(path):
            if r.get("err") is not None:
                continue
            for k, e in enumerate(r.get("evs") or []):
                ev = e.get("ev")
                if ev not in ("BuyEvent", "SellEvent") or "raw" not in e:
                    continue
                try:
                    b = base64.b64decode(e["raw"])
                    v = struct.unpack_from("<14Q", b, 8)
                    o = 8 + 14 * 8
                    pool = base58.b58encode(b[o:o + 32]).decode(); user = base58.b58encode(b[o + 32:o + 64]).decode()
                    o += 32 * 7
                    cbps = struct.unpack_from("<Q", b, o)[0]
                except Exception:
                    continue
                rows.append(dict(slot=r["slot"], idx=r["idx"], k=k, pool=pool, user=user, buy=ev == "BuyEvent",
                                 base=v[1], quote=v[7], pbase=v[5], pquote=v[6], fee_bps=v[8] + v[10] + cbps))
    d = pl.DataFrame(rows).unique(subset=["slot", "idx", "k"]).sort(["slot", "idx", "k"])
    d.write_parquet(OUT)
    return d


def sim(ps, j, size, fee, fixed, variant, m, hold):
    """Buy `size` SOL after row j (never first), sell after `hold` slots. AMM: x*y=k, fee on input."""
    slot = ps["slot"]; B = ps["pbase"]; Q = ps["pquote"]; n = len(slot)
    def land(r, mm):
        s = slot[r]; e = r
        while e + 1 < n and slot[e + 1] == s: e += 1
        if variant == "same": return min(r + mm, e)
        if e + 1 >= n or slot[e + 1] > s + 1: return e
        ns = slot[e + 1]; x = e; c = 0
        while x + 1 < n and slot[x + 1] == ns and c < mm: x += 1; c += 1
        return x
    e = land(j, m)
    if (Q[e] / B[e]) > (Q[j] / B[j]) * 1.2:
        return -fixed / size
    qin = size * 1e9 * (1 - fee)
    toks = B[e] - B[e] * Q[e] / (Q[e] + qin)
    t = np.searchsorted(slot, slot[e] + hold, side="left")
    x = min(t, n - 1)
    if x < n - 1:
        x = land(x, m)
    out = Q[x] - Q[x] * B[x] / (B[x] + toks)
    return (out * (1 - fee) / 1e9 - size - 2 * fixed) / size


def main():
    d = pl.read_parquet(OUT) if os.path.exists(OUT) else decode_all()
    d = d.filter((pl.col("pbase") > 0) & (pl.col("pquote") > 0))
    stats = d.group_by("pool").agg(pl.len().alias("n"), pl.col("fee_bps").median().alias("fee"), (pl.col("pquote").median() / 1e9).alias("liq_sol"))
    # SOL-quoted heuristic: quote reserve between 1 and 1e6 SOL
    good = stats.filter((pl.col("n") >= 200) & (pl.col("liq_sol") > 20) & (pl.col("liq_sol") < 1e6))
    print("pools total", stats.height, "active SOL pools", good.height)
    print(good.group_by(pl.col("fee").cut([30, 60, 100])).agg(pl.len(), pl.col("n").sum(), pl.col("liq_sol").median()))
    rng = np.random.default_rng(1)
    size, fixed = 0.5, 0.0015
    res = []
    for (pool,), g in d.join(good.select(["pool", "fee"]), on="pool").sort(["pool", "slot", "idx", "k"]).group_by(["pool"], maintain_order=True):
        ps = {c: g[c].to_numpy() for c in ("slot", "pbase", "pquote", "quote", "buy")}
        ps["pbase"] = ps["pbase"].astype(float); ps["pquote"] = ps["pquote"].astype(float)
        fee = float(g["fee"][0]) / 1e4
        px = ps["pquote"] / ps["pbase"]
        n = len(px); last = -10**9
        for j in range(1, n - 1):
            imp = px[j] / px[j - 1] - 1
            big = abs(imp) >= 0.03
            sampled = ps["slot"][j] - last >= 50
            if not (big or sampled):
                continue
            last = ps["slot"][j] if sampled else last
            k0 = np.searchsorted(ps["slot"], ps["slot"][j] - 280, side="right")
            r60 = px[j] / px[max(k0 - 1, 0)] - 1
            row = dict(pool=pool, slot=int(ps["slot"][j]), fee=fee, imp=float(imp), r60=float(r60), liq=float(ps["pquote"][j] / 1e9), trig="big" if big else "sample")
            for hold, nm in ((47, "10s"), (280, "60s"), (1400, "5m")):
                a = sim(ps, j, size, fee, fixed, "same", int(rng.integers(5, 14)), hold)
                b = sim(ps, j, size, fee, fixed, "next", int(rng.integers(0, 3)), hold)
                row[f"r_{nm}"] = 0.5 * (a + b)
            res.append(row)
    R = pl.DataFrame(res)
    R.write_parquet(os.path.join(DATA, "derived", "pswap_events.parquet"))
    R = R.with_columns(pl.col("fee").cut([0.0031, 0.006, 0.01]).alias("fee_b"), pl.col("imp").cut([-0.1, -0.03, 0.03, 0.1]).alias("imp_b"),
                       pl.col("r60").cut([-0.3, -0.1, 0.1, 0.3]).alias("r60_b"))
    pl.Config.set_tbl_rows(40)
    for by in (["fee_b"], ["fee_b", "imp_b"], ["fee_b", "r60_b"]):
        print(R.group_by(by).agg(pl.len(), pl.col("r_10s").mean().round(4), pl.col("r_60s").mean().round(4), pl.col("r_5m").mean().round(4),
                                 pl.col("pool").n_unique().alias("pools")).sort(by))


if __name__ == "__main__":
    main()
