"""Who makes money WITHOUT being first? Per-wallet realised PnL across coins, then filter for wallets
that win consistently while entering late (first buy >= 2 slots after launch).

PnL per (wallet, coin) = sell proceeds net of fees + value of remaining tokens sold into the final
curve state - buy cost incl. fees - tx fees and tips. The Mayhem agent and each coin's creator are
excluded.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import numpy as np, polars as pl
from load import load_raw

AGENT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
OUT = os.path.join(os.path.dirname(__file__), "..", "..", "data", "derived")

D = load_raw(kinds=("trades", "creates"))
tr = D["trades"].filter((pl.col("sol") > 0) & pl.col("quote").is_null())
cr = D["creates"].select(["mint", pl.col("slot").alias("s0"), "creator", "mayhem"])
tr = tr.join(cr, on="mint", how="inner").filter((pl.col("user") != AGENT) & (pl.col("user") != pl.col("creator")))
last = D["trades"].filter(pl.col("sol") > 0).sort(["slot", "idx", "k"]).group_by("mint").last().select(
    ["mint", pl.col("vsol").cast(pl.Float64).alias("lv"), pl.col("vtok").cast(pl.Float64).alias("lt")])
tr = tr.with_columns(
    pl.when(pl.col("buy")).then(pl.col("sol") + pl.col("fee") + pl.col("cfee")).otherwise(0).alias("cost"),
    pl.when(~pl.col("buy")).then(pl.col("sol") - pl.col("fee") - pl.col("cfee")).otherwise(0).alias("proc"),
    pl.when(pl.col("buy")).then(pl.col("tok")).otherwise(-pl.col("tok")).alias("dtok"),
    (pl.col("txfee").fill_null(5000) + pl.col("tip").fill_null(0)).alias("txc"),
    (pl.col("slot") - pl.col("s0")).alias("rel"))
g = tr.group_by(["user", "mint"]).agg(
    pl.col("cost").sum(), pl.col("proc").sum(), pl.col("dtok").sum().alias("rem"), pl.col("txc").sum(),
    pl.col("rel").filter(pl.col("buy")).min().alias("first_rel"), pl.col("slot").min().alias("first_slot"),
    pl.col("slot").max().alias("last_slot"), pl.col("buy").sum().alias("nb"), pl.len().alias("n"),
    pl.col("mayhem").first()).filter(pl.col("cost") > 0)
g = g.join(last, on="mint", how="left").with_columns(pl.col("rem").clip(0, None).cast(pl.Float64))
g = g.with_columns(((pl.col("lv") - pl.col("lv") * pl.col("lt") / (pl.col("lt") + pl.col("rem"))) * 0.9875).fill_null(0).alias("rem_val"))
g = g.with_columns(((pl.col("proc") + pl.col("rem_val") - pl.col("cost") - pl.col("txc")) / 1e9).alias("pnl"),
                   (pl.col("cost") / 1e9).alias("cost_sol"))
g = g.with_columns((pl.col("pnl") / pl.col("cost_sol")).alias("ret"),
                   ((pl.col("last_slot") - pl.col("first_slot")) * 0.214).alias("hold_s"))
g.write_parquet(os.path.join(OUT, "wallet_coin_pnl.parquet"))
med = g["first_slot"].median()
W = g.group_by("user").agg(
    pl.len().alias("coins"), pl.col("pnl").sum().alias("pnl"), pl.col("cost_sol").sum().alias("vol"),
    pl.col("ret").median().alias("med_ret"), (pl.col("pnl") > 0).mean().alias("win"),
    (pl.col("first_rel") >= 2).mean().alias("late_frac"), pl.col("first_rel").median().alias("med_entry_slot"),
    pl.col("hold_s").median().alias("med_hold_s"), pl.col("cost_sol").median().alias("med_size"),
    pl.col("pnl").filter(pl.col("first_slot") <= med).sum().alias("pnl_h1"),
    pl.col("pnl").filter(pl.col("first_slot") > med).sum().alias("pnl_h2"),
    pl.col("mayhem").mean().alias("mayhem_frac"))
W.write_parquet(os.path.join(OUT, "wallets.parquet"))
print("wallet-coin rows", g.height, "wallets", W.height)
print("all wallets: total pnl", round(W["pnl"].sum(), 1), "SOL; share of wallets with pnl>0:", round((W["pnl"] > 0).mean(), 3))
act = W.filter(pl.col("coins") >= 20)
print("active (>=20 coins):", act.height, "profitable:", (act["pnl"] > 0).sum(), "profitable both halves:",
      act.filter((pl.col("pnl_h1") > 0) & (pl.col("pnl_h2") > 0)).height)
late = act.filter((pl.col("late_frac") >= 0.8) & (pl.col("pnl_h1") > 0) & (pl.col("pnl_h2") > 0)).sort("pnl", descending=True)
print("LATE-entry, profitable in both halves:", late.height)
pl.Config.set_tbl_rows(30); pl.Config.set_tbl_cols(14); pl.Config.set_fmt_str_lengths(12)
print(late.select(["user", "coins", "pnl", "vol", "win", "med_ret", "med_entry_slot", "med_hold_s", "med_size", "pnl_h1", "pnl_h2", "mayhem_frac"]).head(30))
early = act.filter((pl.col("late_frac") < 0.3) & (pl.col("pnl_h1") > 0) & (pl.col("pnl_h2") > 0))
print("EARLY-entry (slot 0-1), profitable both halves:", early.height, "total pnl", round(early["pnl"].sum(), 1))
