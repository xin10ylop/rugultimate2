#!/usr/bin/env python3
"""Complete-block collector for pump.fun research.

Polls confirmed blocks from a Solana RPC (getBlock, full tx details) and writes
compact, ordered records:
  pump_tx   : every tx (ok AND failed) touching pump.fun / PumpSwap: slot, block index,
              signer, fee, CU price/limit/used, tip transfers, decoded events.
  transfers : every system-program SOL transfer >= MIN_XFER lamports in the block
              (top-level + inner), with recipient pre-balance (0 => fresh wallet).
Blocks are complete and ordered, so in-block position (idx) is exact.

Usage: block_collector.py --out DATA_DIR [--rpc URL] [--workers 3] [--start SLOT] [--end SLOT]
"""
import argparse, asyncio, base64, gzip, hashlib, os, struct, sys, time
import aiohttp, orjson, base58

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PAMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
CB = "ComputeBudget111111111111111111111111111111"
SYS = "11111111111111111111111111111111"
WATCH = {PUMP, PAMM}
MIN_XFER = 10_000_000  # 0.01 SOL for the general transfer table

def disc(n): return hashlib.sha256(f"event:{n}".encode()).digest()[:8]
EV = {disc(n): n for n in ["TradeEvent", "CreateEvent", "CompleteEvent", "CompletePumpAmmMigrationEvent",
                           "BuyEvent", "SellEvent", "CreatePoolEvent", "DistributeFeeToHoldersEvent",
                           "DistributeCreatorFeesEvent", "CollectCoinCreatorFeeEvent", "ClaimCashbackEvent",
                           "MigrateBondingCurveCreatorEvent"]}

class R:
    __slots__ = ("b", "o")
    def __init__(s, b, o=8): s.b, s.o = b, o
    def pk(s): v = base58.b58encode(s.b[s.o:s.o+32]).decode(); s.o += 32; return v
    def u64(s): v = struct.unpack_from("<Q", s.b, s.o)[0]; s.o += 8; return v
    def i64(s): v = struct.unpack_from("<q", s.b, s.o)[0]; s.o += 8; return v
    def u8(s): v = s.b[s.o]; s.o += 1; return v
    def st(s):
        n = struct.unpack_from("<I", s.b, s.o)[0]; s.o += 4
        v = s.b[s.o:s.o+n].decode("utf-8", "replace"); s.o += n; return v

def decode_event(name, b):
    r = R(b)
    try:
        if name == "TradeEvent":
            d = dict(mint=r.pk(), sol=r.u64(), tok=r.u64(), buy=r.u8(), user=r.pk(), ts=r.i64(),
                     vsol=r.u64(), vtok=r.u64(), rsol=r.u64(), rtok=r.u64(), fee_rcpt=r.pk(),
                     fee_bps=r.u64(), fee=r.u64(), creator=r.pk(), cfee_bps=r.u64(), cfee=r.u64())
            try:
                r.u8(); r.u64(); r.u64(); r.u64(); r.i64(); d["ix"] = r.st()
                d["mayhem"] = r.u8(); d["cb_bps"] = r.u64(); d["cb"] = r.u64(); d["bb_bps"] = r.u64(); d["bb"] = r.u64()
                nsh = struct.unpack_from("<I", r.b, r.o)[0]; r.o += 4 + nsh * 34
                q = r.pk(); d["quote"] = None if q == SYS else q
                d["qamt"] = r.u64(); r.u64(); r.u64(); d["hr_bps"] = r.u64(); d["hr"] = r.u64()
            except Exception: pass
            return d
        if name == "CreateEvent":
            d = dict(name=r.st()[:64], symbol=r.st()[:32], uri=r.st()[:200], mint=r.pk(), curve=r.pk(),
                     user=r.pk(), creator=r.pk(), ts=r.i64(), vtok=r.u64(), vsol=r.u64(), rtok=r.u64(), supply=r.u64())
            try:
                d["token_program"] = r.pk(); d["mayhem"] = r.u8(); d["cashback"] = r.u8()
                q = r.pk(); d["quote"] = None if q == SYS else q
                d["vquote"] = r.u64(); d["cfee_bps"] = r.u64(); d["holder_reward"] = r.u8()
            except Exception: pass
            return d
        if name == "CompleteEvent":
            return dict(user=r.pk(), mint=r.pk(), curve=r.pk(), ts=r.i64())
    except Exception as e:
        return {"decode_error": str(e)}
    return None

def ix_iter(tx, keys):
    msg = tx["transaction"]["message"]
    for i, ix in enumerate(msg["instructions"]):
        yield (i, -1, ix)
    for inner in (tx["meta"].get("innerInstructions") or []):
        for j, ix in enumerate(inner["instructions"]):
            yield (inner["index"], j, ix)

def parse_block(slot, blk, tip_accts):
    out_tx, out_xfer = [], []
    bt = blk.get("blockTime")
    for idx, tx in enumerate(blk["transactions"]):
        meta = tx["meta"]; msg = tx["transaction"]["message"]
        la = meta.get("loadedAddresses") or {}
        keys = msg["accountKeys"] + la.get("writable", []) + la.get("readonly", [])
        pre = meta["preBalances"]; post = meta["postBalances"]
        logs = meta.get("logMessages") or []
        watched = any(k in WATCH for k in keys)
        cu_price = cu_limit = None
        xfers = []
        for (top, inner, ix) in ix_iter(tx, keys):
            pid = keys[ix["programIdIndex"]]
            if pid == SYS:
                try:
                    data = base58.b58decode(ix["data"])
                except Exception:
                    continue
                if len(data) >= 12 and data[0] == 2 and data[1:4] == b"\0\0\0":
                    lam = struct.unpack_from("<Q", data, 4)[0]
                    a = ix["accounts"]
                    if len(a) >= 2:
                        frm, to = keys[a[0]], keys[a[1]]
                        xfers.append((frm, to, lam, pre[a[1]] if a[1] < len(pre) else None, top, inner))
            elif pid == CB and inner == -1:
                try:
                    data = base58.b58decode(ix["data"])
                    if data[0] == 3: cu_price = struct.unpack_from("<Q", data, 1)[0]
                    elif data[0] == 2: cu_limit = struct.unpack_from("<I", data, 1)[0]
                except Exception: pass
        sig = tx["transaction"]["signatures"][0]
        signer = keys[0]
        for (frm, to, lam, pre_to, top, inner) in xfers:
            if lam >= MIN_XFER:
                out_xfer.append(dict(slot=slot, idx=idx, sig=sig, frm=frm, to=to, lam=lam, pre_to=pre_to,
                                     ok=meta["err"] is None, signer=signer))
        if not watched:
            continue
        evs = []
        if meta["err"] is None:
            stack = []
            for l in logs:
                if l.startswith("Program ") and " invoke [" in l:
                    stack.append(l.split(" ")[1]); continue
                if l.startswith("Program ") and (l.endswith(" success") or " failed" in l):
                    if stack and l.split(" ")[1] == stack[-1]: stack.pop()
                    continue
                if l.startswith("Program data: ") and stack and stack[-1] in WATCH:
                    try: b = base64.b64decode(l[14:])
                    except Exception: continue
                    nm = EV.get(b[:8])
                    if nm is None:
                        evs.append({"ev": "?", "prog": stack[-1][:4], "raw": l[14:]})
                        continue
                    d = decode_event(nm, b)
                    if d is None:
                        d = {"raw": l[14:]}
                    d["ev"] = nm; d["prog"] = stack[-1][:4]
                    evs.append(d)
        ixnames = [l[26:] for l in logs if l.startswith("Program log: Instruction: ")][:12]
        tips = [(to, lam) for (frm, to, lam, _, _, _) in xfers if frm == signer and (to in tip_accts)]
        small = [(to, lam) for (frm, to, lam, _, _, _) in xfers if frm == signer and lam < 50_000_000][:8]
        if meta["err"] is not None:
            if PUMP not in keys:      # failed PumpSwap arbitrage spam: not stored
                continue
            mints = [k for k in keys if k.endswith("pump")][:2]
            out_tx.append(dict(slot=slot, idx=idx, signer=signer, err=str(meta["err"])[:60], fee=meta["fee"], mints=mints,
                               cu=meta.get("computeUnitsConsumed"), cu_price=cu_price, cu_limit=cu_limit, tips=tips,
                               ixs=[x for x in ixnames if x[:1] in "BSC"][:3]))
            continue
        out_tx.append(dict(slot=slot, bt=bt, idx=idx, n=len(blk["transactions"]), sig=sig, signer=signer,
                           err=None if meta["err"] is None else str(meta["err"])[:120],
                           fee=meta["fee"], cu=meta.get("computeUnitsConsumed"), cu_price=cu_price,
                           cu_limit=cu_limit, tips=tips, small_xfers=small, ixs=ixnames, evs=evs,
                           sol_delta=(post[0] - pre[0])))
    return out_tx, out_xfer

TIP_ACCTS = set("""96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5 HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe
Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49
DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt
DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL 3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT
TEMPaMeCRFAS9EKF53Jd6KpHxgL47uWLcpFArU1Fanq noz3jAjPiHuBPqiSPkkugaJDkJscPuRhYnSpbi8UvC4
noz3str9KXfpKknefHji8L1mPgimezaiUyCHYMDv1GE noz6uoYCDijhu1V7cutCpwxNiSovEwLdRHPwmgCGDNo
noz9EPNcT7WH6Sou3sr3GGjHQYVkN3DNirpbvDkv9YJ nozc5yT15LazbLTFVZzoNZCwjh3yUtW86LoUyqsBu4L
nozFrhfnNGoyqwVuwPAW4aaGqempx4PU6g6D9CJMv7Z nozievPk7HyK1Rqy1MPJwVQ7qQg2QoJGyP71oeDwbsu
noznbgwYnBLDHu8wcQVCEw6kDrXkPdKkydGJGNXGvL7 nozNVWs5N8mgzuD3qigrCG2UoKxZttxzZ85pvAQVrbP
nozpEGbwx4BcGp6pvEdAh1JoC2CQGZdU6HbNP1v2p6P nozrhjhkCr3zXT3BiT4WCodYCUFeQvcdUkM7MqhKqge
nozrwQtWhEdrA6W8dkbt9gnUaMs52PdAv5byipnadq3 nozUacTVWub3cL4mJmGCYjKZTnE9RbdY5AP46iQgbPJ
nozWCyTPppJjRuw2fpzDhhWbW355fzosWSzrrMYB1Qk nozWNju6dY353eMkMqURqwQEoM3SFgEKC6psLCSfUne
nozxNBgWohjR75vdspfxR5H9ceC7XXH99xpxhVGt3Bb""".split())

class Sink:
    def __init__(s, out):
        s.out = out; s.hour = None; s.f = {}
    def write(s, kind, rows, slot_time):
        if not rows: return
        h = time.strftime("%Y%m%d_%H", time.gmtime(slot_time or time.time()))
        if h != s.hour:
            for f in s.f.values(): f.close()
            s.f = {}; s.hour = h
        if kind not in s.f:
            s.f[kind] = gzip.open(os.path.join(s.out, f"{kind}_{h}_{os.getpid()}.jsonl.gz"), "ab", compresslevel=5)
        f = s.f[kind]
        for r in rows: f.write(orjson.dumps(r) + b"\n")
    def flush(s):
        for f in s.f.values(): f.flush()
    def close(s):
        for f in s.f.values(): f.close()
        s.f = {}

async def rpc(sess, url, method, params):
    async with sess.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) as r:
        if r.status == 429: return {"error": {"code": 429, "message": "rate"}}
        return orjson.loads(await r.read())

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default="https://api.mainnet-beta.solana.com,https://solana-rpc.publicnode.com/,https://solana.leorpc.com/?api_key=FREE,https://rpc.solanatracker.io/public,https://public.rpc.solanavibestation.com/",
                    help="comma-separated RPC URLs; blocks are spread across them")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=2, help="workers per RPC")
    ap.add_argument("--start", type=int); ap.add_argument("--end", type=int)
    ap.add_argument("--lag", type=int, default=4, help="stay this many slots behind confirmed tip")
    ap.add_argument("--resume", action="store_true", help="continue from OUT/progress.txt")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    urls = []
    for u in a.rpc.split(","):
        if not u: continue
        u, _, w = u.partition("|")          # "url|workers" gives a per-RPC worker count
        urls += [u] * (int(w) if w else a.workers)
    sink = Sink(a.out)
    conn = aiohttp.TCPConnector(limit=a.workers * len(urls) * 2)
    async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=30),
                                     headers={"Accept-Encoding": "gzip"}) as sess:
        tip = (await rpc(sess, urls[0], "getSlot", [{"commitment": "confirmed"}]))["result"]
        nxt = a.start if a.start else tip - a.lag
        prog = os.path.join(a.out, "progress.txt")
        if a.resume and os.path.exists(prog):
            nxt = int(open(prog).read().strip()) + 1
        q = asyncio.Queue(); results = {}; done_upto = nxt - 1
        stats = dict(ok=0, skip=0, err=0, rate=0, tx=0, xf=0)
        state = {"tip": tip, "next": nxt}
        stop = asyncio.Event()
        import signal
        term = {"flag": False}
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: term.update(flag=True))
        with open(os.path.join(a.out, "collector.pid"), "w") as pf: pf.write(str(os.getpid()))

        async def tipper():
            while not stop.is_set():
                try:
                    state["tip"] = (await rpc(sess, urls[0], "getSlot", [{"commitment": "confirmed"}]))["result"]
                except Exception: pass
                hi = state["tip"] - a.lag if a.end is None else min(a.end, state["tip"] - a.lag)
                while state["next"] <= hi and q.qsize() < 200:
                    await q.put(state["next"]); state["next"] += 1
                if a.end is not None and state["next"] > a.end and q.empty():
                    break
                await asyncio.sleep(1.0)

        per_url = {u: dict(ok=0, rate=0, err=0) for u in set(urls)}
        async def worker(wid):
            backoff = 0.5
            url = urls[wid % len(urls)]
            while not stop.is_set():
                try: slot = await asyncio.wait_for(q.get(), 5)
                except asyncio.TimeoutError:
                    if a.end is not None and state["next"] > a.end: return
                    continue
                for attempt in range(8):
                    try:
                        u = url if attempt < 2 else urls[(wid + attempt * 3) % len(urls)]
                        res = await rpc(sess, u, "getBlock", [slot, {"encoding": "json", "transactionDetails": "full",
                                        "maxSupportedTransactionVersion": 1, "rewards": False, "commitment": "confirmed"}])
                    except Exception as e:
                        res = {"error": {"code": -1, "message": str(e)}}
                    if "result" in res and res["result"] is not None:
                        txr, xfr = parse_block(slot, res["result"], TIP_ACCTS)
                        results[slot] = (res["result"].get("blockTime"), txr, xfr); stats["ok"] += 1; per_url[u]["ok"] += 1
                        backoff = max(0.25, backoff * 0.8); break
                    code = res.get("error", {}).get("code")
                    if code in (-32007, -32009):  # skipped / missing in long-term storage
                        results[slot] = None; stats["skip"] += 1; break
                    if code == -32004 or code == -32014:  # not available yet
                        await asyncio.sleep(1.0); continue
                    if code == -32001 or code == -32603:  # node pruned this slot: try another RPC
                        await asyncio.sleep(0.1); continue
                    if code == 429: stats["rate"] += 1; per_url[u]["rate"] += 1
                    else:
                        stats["err"] += 1; per_url[u]["err"] += 1
                        if stats["err"] < 20: print("rpc error", u[:30], slot, str(res)[:160], flush=True)
                    await asyncio.sleep(backoff); backoff = min(8, backoff * 2)
                else:
                    results[slot] = None; stats["err"] += 1
                    with open(os.path.join(a.out, "gaps.txt"), "a") as g: g.write(f"{slot}\n")

        async def writer():
            nonlocal done_upto
            last = time.time()
            while not stop.is_set():
                progressed = False
                while (done_upto + 1) in results:
                    s = done_upto + 1; r = results.pop(s)
                    if r is not None:
                        bt, txr, xfr = r
                        sink.write("pump_tx", txr, bt); sink.write("transfers", xfr, bt)
                        stats["tx"] += len(txr); stats["xf"] += len(xfr)
                    done_upto = s; progressed = True
                if time.time() - last > 30:
                    sink.flush(); last = time.time()
                    with open(prog + ".tmp", "w") as pf: pf.write(str(done_upto))
                    os.replace(prog + ".tmp", prog)
                    print(time.strftime("%H:%M:%S"), "done", done_upto, "tip", state["tip"], "behind", state["tip"] - done_upto,
                          stats, {k.split("//")[1][:18]: v for k, v in per_url.items()}, flush=True)
                if (a.end is not None and done_upto >= a.end) or term["flag"]:
                    sink.close()
                    with open(prog, "w") as pf: pf.write(str(done_upto))
                    if term["flag"]: os._exit(0)
                    stop.set(); break
                if not progressed: await asyncio.sleep(0.2)

        tasks = [asyncio.create_task(tipper()), asyncio.create_task(writer())] + \
                [asyncio.create_task(worker(i)) for i in range(len(urls))]
        await stop.wait() if a.end is not None else await asyncio.gather(*tasks)
        sink.flush()
        with open(prog, "w") as pf: pf.write(str(done_upto))

if __name__ == "__main__":
    asyncio.run(main())
