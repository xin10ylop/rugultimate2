"""How do the out-of-sample skilled wallets exit? Distance from the triggering buy, fees paid."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import numpy as np, polars as pl
from load import load_raw
D = load_raw(kinds=("trades", "creates", "completes"))
tr = D["trades"].filter((pl.col("sol") > 0) & pl.col("quote").is_null()).sort(["mint", "slot", "idx", "k"])
g = pl.read_parquet(os.path.join(os.path.dirname(__file__), "..", "..", "data", "derived", "wallet_coin_pnl.parquet"))
closed = g.join(D["completes"].select("mint").unique(), on="mint", how="anti").filter(pl.col("rem_val") / 1e9 <= 0.1 * pl.col("cost_sol"))
med = closed["first_slot"].median()
A = closed.filter(pl.col("first_slot") <= med).group_by("user").agg(pl.len().alias("n"), pl.col("pnl").sum().alias("pnl"),
        pl.col("cost_sol").sum().alias("vol"), (pl.col("first_rel") >= 2).mean().alias("late"))
sk = A.filter((pl.col("late") >= 0.8) & (pl.col("n") >= 10) & (pl.col("pnl") / pl.col("vol") > 0.1))["user"].to_list()
tr = tr.with_columns(pl.col("buy").shift(1).over("mint").alias("prev_buy"), pl.col("sol").shift(1).over("mint").alias("prev_sol"),
                     pl.col("slot").shift(1).over("mint").alias("prev_slot"), pl.col("idx").shift(1).over("mint").alias("prev_idx"),
                     pl.col("user").shift(1).over("mint").alias("prev_user"))
tr = tr.with_columns((pl.col("cu_price").fill_null(0)).alias("cup"))
def describe(lab, s):
    same = (s["slot"] == s["prev_slot"])
    print(f"{lab:22s} n={s.height:6d} prev trade is buy={s['prev_buy'].mean():.2f} same slot as prev={same.mean():.2f} "
          f"idx gap<=2 (same slot)={(same & ((s['idx'] - s['prev_idx']) <= 2)).mean():.2f} med prev buy SOL={s.filter(pl.col('prev_buy'))['prev_sol'].median()/1e9 if s.height else 0:.3f} "
          f"cu_price med={s['cup'].median():.0f} p90={s['cup'].quantile(0.9):.0f} tip>0={(s['tip'].fill_null(0) > 0).mean():.2f} txfee med={s['txfee'].median():.0f}")
sells = tr.filter(~pl.col("buy") & (pl.col("slot") > med))
describe("skilled sells (H2)", sells.filter(pl.col("user").is_in(sk)))
describe("all other sells (H2)", sells.filter(~pl.col("user").is_in(sk) & (pl.col("user") != "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s")))
buys = tr.filter(pl.col("buy") & (pl.col("slot") > med))
describe("skilled buys (H2)", buys.filter(pl.col("user").is_in(sk)))
