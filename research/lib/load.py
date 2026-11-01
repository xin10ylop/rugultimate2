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
            os.path.join(DATA, "live2", f"{kind}_*.jsonl.gz"),
            os.path.join(DATA, "back", "*", f"{kind}_*.jsonl.gz"),
            os.path.join(DATA, "back2", "*", f"{kind}_*.jsonl.gz")]
    out = []
    for p in pats:
        out += glob.glob(p)
    return sorted(out)


CACHE = os.path.join(DATA, "cache")
KINDS = ("trades", "creates", "completes", "failed", "txs", "other_ev")


def _parse_pump_file(path):
    trades, creates, completes, failed, txs, other_ev = [], [], [], [], [], []
    if True:
        for r in _iter_gz(path):
            s = r["slot"]
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

    def df(rows):
        return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    return dict(trades=df(trades), creates=df(creates), completes=df(completes), failed=df(failed), txs=df(txs),
                other_ev=df(other_ev))


KEYS = dict(trades=["slot", "idx", "k"], creates=["slot", "idx", "mint"], completes=["slot", "idx", "mint"],
            failed=["slot", "idx"], txs=["slot", "idx"], other_ev=["slot", "idx", "ev"])


def _cached(path, parse, kinds):
    """Parse a gz file once; cache each table as parquet keyed by path, size and mtime."""
    os.makedirs(CACHE, exist_ok=True)
    st = os.stat(path)
    tag = f"{os.path.relpath(path, DATA).replace(os.sep, '__')}__{st.st_size}_{int(st.st_mtime)}"
    out, ok = {}, True
    for k in kinds:
        cp = os.path.join(CACHE, f"{tag}__{k}.parquet")
        if os.path.exists(cp):
            out[k] = pl.read_parquet(cp)
        else:
            ok = False; break
    if ok:
        return out
    # only cache files that are no longer being written (not modified in the last 2 minutes)
    fresh = parse(path)
    import time as _t
    if _t.time() - st.st_mtime > 120:
        for old in glob.glob(os.path.join(CACHE, f"{os.path.relpath(path, DATA).replace(os.sep, '__')}__*")):
            os.remove(old)
        for k in kinds:
            fresh.get(k, pl.DataFrame()).write_parquet(os.path.join(CACHE, f"{tag}__{k}.parquet"))
    return fresh


def _concat(frames):
    frames = [f for f in frames if f.height]
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")


def load_raw(min_slot=None, max_slot=None, kinds=KINDS):
    parts = {k: [] for k in kinds}
    for path in files("pump_tx"):
        t = _cached(path, _parse_pump_file, KINDS)
        for k in kinds:
            parts[k].append(t.get(k, pl.DataFrame()))
    out = {}
    for k in kinds:
        d = _concat(parts[k])
        if d.height:
            if min_slot: d = d.filter(pl.col("slot") >= min_slot)
            if max_slot: d = d.filter(pl.col("slot") <= max_slot)
            d = d.unique(subset=KEYS[k], keep="first").sort(KEYS[k])
        out[k] = d
    return out


def _parse_xfer_file(path):
    rows = [dict(slot=r["slot"], idx=r["idx"], frm=r["frm"], to=r["to"], lam=r["lam"], pre_to=r.get("pre_to"),
                 ok=r.get("ok", True)) for r in _iter_gz(path)]
    return dict(transfers=pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame())


def load_transfers(min_slot=None, max_slot=None):
    d = _concat([_cached(p, _parse_xfer_file, ("transfers",))["transfers"] for p in files("transfers")])
    if not d.height:
        return d
    if min_slot: d = d.filter(pl.col("slot") >= min_slot)
    if max_slot: d = d.filter(pl.col("slot") <= max_slot)
    return d.unique(subset=["slot", "idx", "frm", "to", "lam"], keep="first").sort(["slot", "idx"])


def add_price(trades: pl.DataFrame) -> pl.DataFrame:
    """Post-trade spot price (SOL per whole token) and market cap in SOL."""
    return trades.with_columns(
        (pl.col("vsol") / pl.col("vtok")).alias("px_raw"),
        (pl.col("vsol") / pl.col("vtok") * TOKEN_SUPPLY_RAW / LAMPORTS).alias("mcap"),
        (pl.col("sol") / LAMPORTS).alias("sol_f"),
    )
