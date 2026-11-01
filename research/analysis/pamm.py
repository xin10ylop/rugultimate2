"""Decode PumpSwap (pAMM) Buy/Sell/CreatePool events and pump migration events from the raw tape."""
import sys, os, base64, struct, glob
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import base58, polars as pl
from load import files, _iter_gz, DATA

def R(b):
    o = [8]
    def pk():
        v = base58.b58encode(b[o[0]:o[0]+32]).decode(); o[0] += 32; return v
    def u64():
        v = struct.unpack_from("<Q", b, o[0])[0]; o[0] += 8; return v
    def u16():
        v = struct.unpack_from("<H", b, o[0])[0]; o[0] += 2; return v
    def u8():
        v = b[o[0]]; o[0] += 1; return v
    return pk, u64, u16, u8

def dec(ev, raw):
    b = base64.b64decode(raw); pk, u64, u16, u8 = R(b)
    if ev in ("BuyEvent", "SellEvent"):
        ts = u64(); amt = u64(); lim = u64(); u64(); u64(); pb = u64(); pq = u64(); q = u64()
        for _ in range(6): u64()
        pool = pk(); user = pk()
        return dict(ts=ts, base=amt, quote=q, pool_base=pb, pool_quote=pq, pool=pool, user=user, buy=ev == "BuyEvent")
    if ev == "CreatePoolEvent":
        ts = u64(); u16(); creator = pk(); bm = pk(); qm = pk(); u8(); u8(); bi = u64(); qi = u64(); pb = u64(); pq = u64()
        u64(); u64(); u64(); u8(); pool = pk()
        return dict(ts=ts, base_mint=bm, quote_mint=qm, pool_base=pb, pool_quote=pq, pool=pool, creator=creator)
    if ev == "CompletePumpAmmMigrationEvent":
        user = pk(); mint = pk(); ma = u64(); sa = u64(); fee = u64(); bc = pk(); ts = u64(); pool = pk()
        return dict(mint=mint, mint_amount=ma, sol_amount=sa, mig_fee=fee, pool=pool, ts=ts)
    return None

def main():
    rows = {k: [] for k in ("swap", "create", "migr")}
    for path in files("pump_tx"):
        for r in _iter_gz(path):
            if r.get("err") is not None: continue
            for k, e in enumerate(r.get("evs") or []):
                ev = e.get("ev")
                if ev in ("BuyEvent", "SellEvent", "CreatePoolEvent", "CompletePumpAmmMigrationEvent") and "raw" in e:
                    try: d = dec(ev, e["raw"])
                    except Exception: continue
                    if d is None: continue
                    d.update(slot=r["slot"], idx=r["idx"], k=k)
                    rows["swap" if ev in ("BuyEvent", "SellEvent") else ("create" if ev == "CreatePoolEvent" else "migr")].append(d)
    out = os.path.join(DATA, "derived")
    for k, v in rows.items():
        if v:
            pl.DataFrame(v).unique(subset=["slot", "idx", "k"]).sort(["slot", "idx", "k"]).write_parquet(os.path.join(out, f"pamm_{k}.parquet"))
        print(k, len(v))

if __name__ == "__main__":
    main()
