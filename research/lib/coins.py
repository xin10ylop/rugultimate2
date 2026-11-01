"""Per-coin tapes and summaries built from the trade table."""
import numpy as np
import polars as pl
from sim import Coin

SLOT_S = 0.214  # measured slot time (2026): ~4.67 slots/s


def build_tapes(trades: pl.DataFrame, creates: pl.DataFrame, only_created=True):
    """Return {mint: Coin} for coins whose CreateEvent we observed (full history from birth)."""
    t = trades.filter(pl.col("sol") > 0)
    if only_created:
        t = t.join(creates.select("mint"), on="mint", how="semi")
    t = t.sort(["mint", "slot", "idx", "k"])
    out = {}
    for (mint,), g in t.group_by(["mint"], maintain_order=True):
        fr = ((g["fee"] + g["cfee"]) / g["sol"]).median()
        out[mint] = Coin(mint=mint, slot=g["slot"].to_numpy(), idx=g["idx"].to_numpy(),
                         vsol=g["vsol"].to_numpy().astype(np.float64), vtok=g["vtok"].to_numpy().astype(np.float64),
                         fee_rate=float(fr) if fr is not None and np.isfinite(fr) else 0.0125,
                         extra=dict(user=g["user"].to_numpy(), buy=g["buy"].to_numpy(), sol=g["sol"].to_numpy(),
                                    tok=g["tok"].to_numpy(), signer=g["signer"].to_numpy()))
    return out


def summarize(tapes, creates: pl.DataFrame):
    cr = {r["mint"]: r for r in creates.iter_rows(named=True)}
    rows = []
    for m, c in tapes.items():
        info = cr.get(m)
        if info is None:
            continue
        s0 = info["slot"]
        px0 = 30e9 / 1073e12
        mc = c.vsol / c.vtok * 1e15 / 1e9
        rel = c.slot - s0
        def peak_within(ns):
            sel = rel <= ns
            return float(mc[sel].max()) if sel.any() else np.nan
        same = rel == 0
        n_same = int(same.sum())
        dev_buy = float(c.extra["sol"][(c.extra["user"] == info["creator"]) & same & c.extra["buy"]].sum() / 1e9)
        rows.append(dict(mint=m, creator=info["creator"], slot0=s0, n=len(c.slot), n_slot0=n_same,
                         mc_slot0=float(mc[same].max()) if n_same else np.nan,
                         dev_buy=dev_buy, mc_peak=float(mc.max()), mc_last=float(mc[-1]),
                         peak_slot=int(rel[int(np.argmax(mc))]), last_slot=int(rel[-1]),
                         pk5s=peak_within(23), pk30s=peak_within(140), pk2m=peak_within(560),
                         mayhem=info.get("mayhem"), name=info.get("name"), uri=info.get("uri")))
    return pl.DataFrame(rows)
