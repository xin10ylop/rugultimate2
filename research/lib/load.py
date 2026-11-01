"""Load collected block data into polars DataFrames.

All tables are de-duplicated on (slot, idx[, k]) and sorted in exact chain order.
"""
import glob, gzip, os, zlib
import orjson
import polars as pl

DATA = os.environ.get("RUG_DATA", os.path.join(os.path.dirname(__file__), "..", "..", "data"))
LAMPORTS = 1_000_000_000
TOKEN_SUPPLY_RAW = 1_000_000_000 * 10**6  # 1B tokens, 6 decimals


def _iter_gz(path):
    """Read a (possibly multi-member, possibly truncated) gzip jsonl file."""
    try:
        with gzip.open(path, "rb") as f:
            for line in f:
                try:
                    yield orjson.loads(line)
                except orjson.JSONDecodeError:
                    pass
    except (EOFError, zlib.error, gzip.BadGzipFile, OSError):
        return


def files(kind):
    pats = [os.path.join(DATA, "fwd", f"{kind}_*.jsonl.gz"), os.path.join(DATA, "live", f"{kind}_*.jsonl.gz"),
            os.path.join(DATA, "back", "*", f"{kind}_*.jsonl.gz"),
            os.path.join(DATA, "back2", "*", f"{kind}_*.jsonl.gz")]
    out = []
    for p in pats:
        out += glob.glob(p)
    return sorted(out)


def load_raw(min_slot=None, max_slot=None):
    trades, creates, completes, failed, txs, other_ev = [], [], [], [], [], []
    for path in files("pump_tx"):
        for r in _iter_gz(path):
            s = r["slot"]
            if (min_slot and s < min_slot) or (max_slot and s > max_slot):
                continue
            if r.get("err") is not None:
                failed.append(dict(slot=s, idx=r["idx"], signer=r["signer"], err=r["err"], fee=r["fee"],
                                   cu=r.get("cu"), cu_price=r.get("cu_price"), cu_limit=r.get("cu_limit"),
                                   tip=sum(t[1] for t in r.get("tips") or []),
                                   mint=(r.get("mints") or [None])[0], ixs=",".join(r.get("ixs") or [])))
                continue
            tip = sum(t[1] for t in r.get("tips") or [])
            txs.append(dict(slot=s, idx=r["idx"], bt=r.get("bt"), sig=r["sig"], signer=r["signer"], fee=r["fee"],
                            cu=r.get("cu"), cu_price=r.get("cu_price"), cu_limit=r.get("cu_limit"), tip=tip,
                            nblock=r.get("n"), sol_delta=r.get("sol_delta"), ixs=",".join(r.get("ixs") or [])))
            for k, e in enumerate(r.get("evs") or []):
                ev = e.get("ev")
                if ev == "TradeEvent" and "mint" in e:
                    trades.append(dict(slot=s, idx=r["idx"], k=k, bt=r.get("bt"), signer=r["signer"], mint=e["mint"],
                                       user=e["user"], buy=bool(e["buy"]), sol=e["sol"], tok=e["tok"], vsol=e["vsol"],
                                       vtok=e["vtok"], rsol=e["rsol"], rtok=e["rtok"], fee=e.get("fee", 0),
                                       cfee=e.get("cfee", 0), creator=e.get("creator"), ix=e.get("ix"),
                                       ts=e.get("ts"), txfee=r["fee"], cu_price=r.get("cu_price"), tip=tip,
                                       quote=e.get("quote"), cb=e.get("cb", 0), hr=e.get("hr", 0), bb=e.get("bb", 0)))
                elif ev == "CreateEvent" and "mint" in e:
                    creates.append(dict(slot=s, idx=r["idx"], bt=r.get("bt"), signer=r["signer"], mint=e["mint"],
                                        creator=e["creator"], user=e["user"], name=e.get("name"), symbol=e.get("symbol"),
                                        uri=e.get("uri"), mayhem=e.get("mayhem"), curve=e.get("curve"),
                                        quote=e.get("quote"), cfee_bps=e.get("cfee_bps"), cashback=e.get("cashback"),
                                        holder_reward=e.get("holder_reward")))
                elif ev == "CompleteEvent" and "mint" in e:
                    completes.append(dict(slot=s, idx=r["idx"], bt=r.get("bt"), mint=e["mint"], user=e["user"]))
                else:
                    other_ev.append(dict(slot=s, idx=r["idx"], ev=ev, prog=e.get("prog"), signer=r["signer"]))

    def df(rows, keys):
        if not rows:
            return pl.DataFrame()
        d = pl.DataFrame(rows, infer_schema_length=None)
        return d.unique(subset=keys, keep="first").sort(keys)

    return dict(trades=df(trades, ["slot", "idx", "k"]), creates=df(creates, ["slot", "idx", "mint"]),
                completes=df(completes, ["slot", "idx", "mint"]), failed=df(failed, ["slot", "idx"]),
                txs=df(txs, ["slot", "idx"]), other_ev=df(other_ev, ["slot", "idx", "ev"]))


def load_transfers(min_slot=None, max_slot=None):
    rows = []
    for path in files("transfers"):
        for r in _iter_gz(path):
            s = r["slot"]
            if (min_slot and s < min_slot) or (max_slot and s > max_slot):
                continue
            rows.append(r)
    if not rows:
        return pl.DataFrame()
    d = pl.DataFrame(rows, infer_schema_length=None)
    return d.unique(subset=["slot", "idx", "frm", "to", "lam"], keep="first").sort(["slot", "idx"])


def add_price(trades: pl.DataFrame) -> pl.DataFrame:
    """Post-trade spot price (SOL per whole token) and market cap in SOL."""
    return trades.with_columns(
        (pl.col("vsol") / pl.col("vtok")).alias("px_raw"),
        (pl.col("vsol") / pl.col("vtok") * TOKEN_SUPPLY_RAW / LAMPORTS).alias("mcap"),
        (pl.col("sol") / LAMPORTS).alias("sol_f"),
    )
