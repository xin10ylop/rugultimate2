"""Exit-precursor test: do wallet-to-wallet token moves (consolidation) precede dumps early enough
for a never-first seller to exit before the dump lands?

Dump = one slot in which the curve price falls >= DUMP (non-agent sells).
Signal = in a window of W slots, >= 2 distinct senders move tokens of the coin to the same receiver,
         or one move of >= MIN_SUPPLY of supply.
Reports: lead time (slots from first signal to dump), hit rate, false-alarm rate, and the PnL change
when the signal is added as an exit rule to a plain hold (entry age2, never-first fills).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import numpy as np, polars as pl
from load import load_raw, files, _iter_gz
from coins import build_tapes
from sim import landing_rows, buy_tokens, sell_sol, Costs

DUMP, W, MIN_SUPPLY = 0.40, 6, 0.02
AGENT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"

mv = pl.DataFrame([r for p in files("tok_moves") for r in _iter_gz(p)])
if mv.height == 0:
    sys.exit("no token-move data yet")
lo = mv["slot"].min()
D = load_raw(min_slot=lo, kinds=("trades", "creates"))
T = build_tapes(D["trades"], D["creates"])
print(f"token moves {mv.height}, coins with tapes since slot {lo}: {len(T)}")

# signals per mint: slots where consolidation is visible
mv = mv.filter(pl.col("mint").is_in(list(T.keys())))
recv = mv.filter(pl.col("delta") > 0).select(["slot", "idx", "mint", pl.col("owner").alias("to"), "delta"])
send = mv.filter(pl.col("delta") < 0).select(["slot", "idx", "mint", pl.col("owner").alias("frm")])
pairs = recv.join(send, on=["slot", "idx", "mint"])  # same tx: frm -> to
sig = {}
for (mint,), g in pairs.sort("slot").group_by(["mint"], maintain_order=True):
    s = g["slot"].to_numpy(); to = g["to"].to_numpy(); frm = g["frm"].to_numpy(); d = g["delta"].to_numpy()
    out = []
    for i in range(len(s)):
        m = (s >= s[i] - W) & (s <= s[i]) & (to == to[i])
        if len(set(frm[m])) >= 2 or d[i] >= MIN_SUPPLY * 1e15:
            out.append(int(s[i]))
    if out:
        sig[mint] = np.array(sorted(set(out)))
print("coins with a consolidation signal:", len(sig))

leads, hits, dumps_total, false_alarms = [], 0, 0, 0
co = Costs(); rng = np.random.default_rng(4)
base_r, sig_r = [], []
cr = {r["mint"]: r["slot"] for r in D["creates"].iter_rows(named=True)}
for m, c in T.items():
    px = c.px
    # dumps: slot-level price drop >= DUMP
    slots = np.unique(c.slot)
    last_px = {}
    for i, s in enumerate(c.slot):
        last_px[s] = px[i]
    sp = np.array([last_px[s] for s in slots])
    drops = slots[1:][sp[1:] / sp[:-1] - 1 <= -DUMP]
    dumps_total += len(drops)
    ss = sig.get(m, np.array([], dtype=int))
    for dslot in drops:
        prior = ss[(ss < dslot) & (ss >= dslot - 200)]
        if len(prior):
            hits += 1; leads.append(int(dslot - prior[0]))
    for s in ss:
        if not ((drops > s) & (drops <= s + 200)).any():
            false_alarms += 1
    # strategy impact: enter at age2, hold up to 10 min, optional exit on signal
    s0 = cr.get(m)
    if s0 is None:
        continue
    w = np.nonzero(c.slot - s0 >= 2)[0]
    if not len(w):
        continue
    j = int(w[0])
    for var, mm in (("next", int(rng.integers(0, 3))),):
        e = landing_rows(c, j, var, mm)
        toks = buy_tokens(c.vsol[e], c.vtok[e], 0.25, c.fee_rate)
        horizon = np.nonzero(c.slot >= c.slot[e] + 2800)[0]
        xb = int(horizon[0]) if len(horizon) else len(c.slot) - 1
        later = ss[ss > c.slot[e]]
        xs = xb
        if len(later):
            k = np.nonzero(c.slot >= later[0])[0]
            if len(k):
                xs = min(xb, landing_rows(c, int(k[0]), var, mm))
        f = lambda x: (sell_sol(c.vsol[x], c.vtok[x], toks, c.fee_rate) - 0.25 - co.buy_fixed - co.sell_fixed) / 0.25
        base_r.append(f(xb)); sig_r.append(f(xs))
print(f"dumps={dumps_total} preceded by a signal (<=200 slots): {hits} ({hits / max(dumps_total, 1):.1%}); false alarms={false_alarms}")
if leads:
    L = np.array(leads)
    print("lead slots quantiles (10/25/50/75/90):", np.quantile(L, [0.1, 0.25, 0.5, 0.75, 0.9]), "share lead >= 3 slots:", round((L >= 3).mean(), 3))
if base_r:
    print(f"hold-10min from age2: mean={np.mean(base_r):+.4f} | with consolidation exit: mean={np.mean(sig_r):+.4f} (n={len(base_r)})")
