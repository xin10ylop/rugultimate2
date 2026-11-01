"""Point-in-time operator clustering (union-find) over launches.

Links between a new launch and earlier launches (all known at the moment the link is used):
  creator   : same creator wallet
  funder    : same direct funder of the creator wallet (skipped for hub funders, see below)
  name      : same normalised coin name within NAME_WINDOW slots
  insider   : a wallet that bought in slots 0..2 of both launches, excluding generic snipers
              (wallets that were early buyers in > SNIPER_FRAC of recent launches) and the Mayhem agent
  devbuy    : identical, non-round dev-buy amount (to 1e-4 SOL) and same mayhem flag

The cluster id of a launch is assigned when its insider info is complete (slot0 + 2) and only uses
links to launches created before it; later merges never rewrite the id used for an earlier decision.
"""
import numpy as np
import polars as pl

MAYHEM_AGENT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
NAME_WINDOW = 6 * 3600 / 0.214
SNIPER_FRAC = 0.02
HUB_FUNDER = 40  # a funder that already funded > this many creators is treated as a hub (CEX/service)


class UF:
    def __init__(self):
        self.p = {}
        self.members = {}

    def find(self, x):
        p = self.p
        if x not in p:
            p[x] = x; self.members[x] = [x]
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            lo, hi = min(ra, rb), max(ra, rb)
            self.p[hi] = lo
            self.members[lo] += self.members.pop(hi)


def early_buyers(trades: pl.DataFrame, launches: pl.DataFrame, max_rel=2):
    t = trades.filter(pl.col("buy")).join(launches.select(["mint", pl.col("slot").alias("s0"), "creator"]), on="mint")
    t = t.filter((pl.col("slot") - pl.col("s0") <= max_rel) & (pl.col("user") != pl.col("creator")) & (pl.col("user") != MAYHEM_AGENT))
    devb = trades.filter(pl.col("buy")).join(launches.select(["mint", pl.col("slot").alias("s0"), "creator"]), on="mint")
    devb = devb.filter((pl.col("slot") == pl.col("s0")) & (pl.col("user") == pl.col("creator"))).group_by("mint").agg(pl.col("sol").sum().alias("dev_lam"))
    eb = t.group_by("mint").agg(pl.col("user").unique().alias("early"))
    return eb, devb


def assign(launches: pl.DataFrame, trades: pl.DataFrame, use=("creator", "funder", "name", "insider", "devbuy")):
    L = launches.sort(["slot", "idx"])
    eb, devb = early_buyers(trades, L)
    L = L.join(eb, on="mint", how="left").join(devb, on="mint", how="left")
    uf = UF()
    last_by = {k: {} for k in ("creator", "funder", "name", "devbuy")}
    name_slot = {}
    insider_hist = {}           # wallet -> list of mints
    recent_early = []           # (slot, set(wallets)) for sniper detection
    funder_count = {}
    cid = []; prior = []
    for r in L.iter_rows(named=True):
        m = r["mint"]; s = r["slot"]
        uf.find(m)
        if "creator" in use and r["creator"]:
            if r["creator"] in last_by["creator"]:
                uf.union(m, last_by["creator"][r["creator"]])
            last_by["creator"][r["creator"]] = m
        f = r.get("funder")
        if "funder" in use and f:
            n = funder_count.get(f, 0)
            if n <= HUB_FUNDER:
                if f in last_by["funder"]:
                    uf.union(m, last_by["funder"][f])
                last_by["funder"][f] = m
            funder_count[f] = n + 1
        nm = (r.get("name") or "").strip().lower()
        if "name" in use and len(nm) >= 3:
            if nm in last_by["name"] and s - name_slot[nm] <= NAME_WINDOW:
                uf.union(m, last_by["name"][nm])
            last_by["name"][nm] = m; name_slot[nm] = s
        dl = r.get("dev_lam")
        if "devbuy" in use and dl:
            q = int(round(dl / 1e5))
            if q % 100 != 0:  # non-round (not a multiple of 0.01 SOL)
                key = (q, r.get("mayhem"))
                if key in last_by["devbuy"]:
                    uf.union(m, last_by["devbuy"][key])
                last_by["devbuy"][key] = m
        early = set(r.get("early") or [])
        if "insider" in use and early:
            # generic-sniper filter from launches in the last ~hour
            recent_early = [(rs, w) for rs, w in recent_early if s - rs <= 16800]
            freq = {}
            for _, w in recent_early:
                for x in w:
                    freq[x] = freq.get(x, 0) + 1
            lim = max(3, SNIPER_FRAC * len(recent_early))
            for x in early:
                if freq.get(x, 0) > lim:
                    continue
                for pm in insider_hist.get(x, [])[-3:]:
                    uf.union(m, pm)
                insider_hist.setdefault(x, []).append(m)
            recent_early.append((s, early))
        root = uf.find(m)
        cid.append(root)
        prior.append([x for x in uf.members[root] if x != m][-200:])
    return L.with_columns(pl.Series("cluster", cid), pl.Series("prior", prior, dtype=pl.List(pl.Utf8)))
