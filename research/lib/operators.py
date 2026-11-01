"""Operator attribution from the SOL funding graph, strictly point-in-time.

For each launch (CreateEvent) we find the last SOL transfer into the creator wallet before the
create transaction (exact slot/idx order). The funder, the amount fingerprint and the funder's own
funder (upstream) define operator keys. Track records only use earlier launches whose outcome
horizon has fully elapsed before the current launch.
"""
import numpy as np
import polars as pl

AMT_Q = 100_000  # amount fingerprint resolution: 0.0001 SOL


def creator_funding(creates: pl.DataFrame, transfers: pl.DataFrame) -> pl.DataFrame:
    c = creates.select(["mint", "creator", "slot", "idx"]).rename({"slot": "cslot", "idx": "cidx"})
    x = transfers.filter(pl.col("ok")).select(["slot", "idx", "frm", "to", "lam", "pre_to"])
    f = c.join(x.rename({"to": "creator"}), on="creator", how="inner")
    f = f.filter((pl.col("slot") < pl.col("cslot")) | ((pl.col("slot") == pl.col("cslot")) & (pl.col("idx") < pl.col("cidx"))))
    f = f.filter(pl.col("frm") != pl.col("creator"))
    last = f.sort(["slot", "idx"]).group_by("mint").last()
    last = last.rename({"frm": "funder", "lam": "fund_lam", "slot": "fslot", "idx": "fidx", "pre_to": "fund_pre"})
    # upstream: last transfer into the funder before the funding transfer
    up = last.select(["mint", "funder", "fslot", "fidx"]).join(x.rename({"to": "funder"}), on="funder", how="inner")
    up = up.filter((pl.col("slot") < pl.col("fslot")) | ((pl.col("slot") == pl.col("fslot")) & (pl.col("idx") < pl.col("fidx"))))
    up = up.filter(pl.col("frm") != pl.col("funder"))
    up = up.sort(["slot", "idx"]).group_by("mint").last().select(["mint", pl.col("frm").alias("upstream")])
    out = creates.select(["mint", "creator", "slot", "idx", "mayhem", "uri", "name", "symbol"]).join(
        last.select(["mint", "funder", "fund_lam", "fslot", "fund_pre"]), on="mint", how="left").join(up, on="mint", how="left")
    out = out.with_columns(
        ((pl.col("slot") - pl.col("fslot")) * 0.214).alias("fund_lead_s"),
        (pl.col("fund_lam") // AMT_Q).alias("amt_key"),
        (pl.col("fund_pre") == 0).alias("fresh"),
        pl.col("uri").str.extract(r"https?://([^/]+)/").alias("uri_host"),
    )
    return out.sort(["slot", "idx"])


def track_records(launch: pl.DataFrame, outcome: pl.DataFrame, keys=("funder", "creator", "upstream"),
                  horizon_slots=600, metrics=("y",)):
    """Point-in-time track record per operator key.

    launch : one row per mint with key columns and 'slot'.
    outcome: one row per mint with metric columns (realised outcome of a launch).
    For each launch L and key k, aggregates metrics over earlier launches L' with the same key and
    L'.slot + horizon_slots <= L.slot (their outcome was fully observable at L's launch).
    Also counts all earlier launches (n_any) regardless of horizon.
    """
    L = launch.join(outcome, on="mint", how="left").sort("slot")
    slots = L["slot"].to_numpy()
    res = {}
    for k in keys:
        kv = L[k].to_list()
        groups = {}
        for i, v in enumerate(kv):
            if v is not None:
                groups.setdefault(v, []).append(i)
        n_any = np.zeros(len(L), dtype=np.int32); n_ev = np.zeros(len(L), dtype=np.int32)
        agg = {m: np.full(len(L), np.nan) for m in metrics}
        mvals = {m: L[m].to_numpy() for m in metrics}
        for v, idxs in groups.items():
            for pos, i in enumerate(idxs):
                prev = idxs[:pos]
                n_any[i] = len(prev)
                ev = [p for p in prev if slots[p] + horizon_slots <= slots[i]]
                n_ev[i] = len(ev)
                if ev:
                    for m in metrics:
                        vals = mvals[m][ev]
                        vals = vals[np.isfinite(vals)]
                        if len(vals):
                            agg[m][i] = vals.mean()
        res[f"{k}_n_any"] = n_any; res[f"{k}_n_ev"] = n_ev
        for m in metrics:
            res[f"{k}_prev_{m}"] = agg[m]
    return L.with_columns([pl.Series(n, v) for n, v in res.items()])
