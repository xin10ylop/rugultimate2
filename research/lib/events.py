"""Event study: for every coin, many candidate trigger points, point-in-time features,
and realistic-fill outcomes under several exit rules.

Point-in-time rule: features at trigger row j only use tape rows <= j (and data from slots < s_j
for anything cross-coin). Outcomes are simulated with sim.simulate (never-first landing).
"""
import numpy as np
from sim import Coin, Exit, Costs, simulate

MAYHEM_AGENT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
LAM = 1e9

EXITS = {
    "tp20_sl20_30s": Exit(tp=0.20, sl=0.20, max_slots=140),
    "tp50_sl30_120s": Exit(tp=0.50, sl=0.30, max_slots=560),
    "tp100_sl40_300s": Exit(tp=1.00, sl=0.40, max_slots=1400),
    "trail15_sl25_120s": Exit(tp=9.0, sl=0.25, max_slots=560, trail=0.15, trail_arm=0.15),
    "t5s": Exit(tp=99, sl=0.99, max_slots=23),
    "t30s": Exit(tp=99, sl=0.99, max_slots=140),
    "t120s": Exit(tp=99, sl=0.99, max_slots=560),
}


def trigger_rows(c: Coin, s0: int):
    """Candidate triggers: first trade at/after given ages, and first crossing of mcap levels."""
    rel = c.slot - s0
    mc = c.vsol / c.vtok * 1e6
    out = {}
    for age in (1, 2, 5, 10, 23, 47, 94, 140, 280, 560):
        w = np.nonzero(rel >= age)[0]
        if len(w):
            out[f"age{age}"] = int(w[0])
    for lvl in (35, 40, 50, 60, 80, 100, 150):
        w = np.nonzero(mc >= lvl)[0]
        if len(w) and rel[w[0]] >= 1:
            out[f"mc{lvl}"] = int(w[0])
    return out


def features(c: Coin, j: int, s0: int, creator: str):
    e = c.extra
    rel = c.slot - s0
    mc = c.vsol / c.vtok * 1e6
    sl = slice(0, j + 1)
    buy = e["buy"][sl]; sol = e["sol"][sl] / LAM; tok = e["tok"][sl].astype(np.float64); user = e["user"][sl]
    s_now = c.slot[j]
    f = {}
    f["age"] = int(rel[j]); f["mc"] = float(mc[j]); f["mc_max"] = float(mc[sl].max())
    f["dd_from_max"] = float(mc[j] / f["mc_max"] - 1)
    s0m = rel[sl] == 0
    f["mc_slot0"] = float(mc[sl][s0m].max()) if s0m.any() else 27.96
    f["n"] = j + 1; f["n_buy"] = int(buy.sum()); f["n_sell"] = int((~buy).sum())
    f["u_buyers"] = len(set(user[buy])); f["u_sellers"] = len(set(user[~buy]))
    f["buy_sol"] = float(sol[buy].sum()); f["sell_sol"] = float(sol[~buy].sum())
    f["net_sol"] = f["buy_sol"] - f["sell_sol"]
    f["slot0_buyers"] = len(set(user[s0m & buy])); f["slot0_sol"] = float(sol[s0m & buy].sum())
    isdev = user == creator
    f["dev_buy_sol"] = float(sol[isdev & buy].sum()); f["dev_sold"] = int((isdev & ~buy).any())
    for w in (5, 23, 94):
        m = c.slot[sl] > s_now - w
        f[f"buy_sol_{w}"] = float(sol[m & buy].sum()); f[f"sell_sol_{w}"] = float(sol[m & ~buy].sum())
        f[f"n_{w}"] = int(m.sum())
        k = np.nonzero(c.slot[sl] <= s_now - w)[0]
        f[f"ret_{w}"] = float(mc[j] / mc[k[-1]] - 1) if len(k) else float(mc[j] / 27.96 - 1)
    f["max_buy"] = float(sol[buy].max()) if buy.any() else 0.0
    # holdings by user (tokens), top concentration of circulating supply bought from the curve
    net = {}
    for u, b, t in zip(user, buy, tok):
        net[u] = net.get(u, 0.0) + (t if b else -t)
    hold = np.array([v for v in net.values() if v > 0])
    circ = 793_100_000e6 - c.vtok[j] + 279_900_000e6  # tokens out of the curve (raw)
    circ = max(circ, 1.0)
    f["holders"] = int(len(hold))
    hs = np.sort(hold)[::-1]
    f["top1"] = float(hs[:1].sum() / 1e15); f["top5"] = float(hs[:5].sum() / 1e15)
    f["dev_hold"] = float(max(net.get(creator, 0.0), 0.0) / 1e15)
    # early-buyer flush: share of tokens bought in slots 0..2 that have been sold back
    early = (rel[sl] <= 2) & buy
    eu = set(user[early])
    eb = sum(t for u, b, t, r in zip(user, buy, tok, rel[sl]) if u in eu and b and r <= 2)
    es = sum(t for u, b, t in zip(user, buy, tok) if u in eu and not b)
    f["early_flush"] = float(min(es / eb, 1.5)) if eb > 0 else 0.0
    f["n_early_buyers"] = len(eu)
    am = user == MAYHEM_AGENT
    f["agent_share"] = float(am.mean())
    return f


def outcomes(c: Coin, j: int, size: float, co: Costs, rng):
    out = {}
    m_e = int(rng.integers(5, 14)); m_x = int(rng.integers(5, 14)); n_e = int(rng.integers(0, 3)); n_x = int(rng.integers(0, 3))
    for name, ex in EXITS.items():
        a = simulate(c, j, size, ex, co, "same", m_e, m_x)
        b = simulate(c, j, size, ex, co, "next", n_e, n_x)
        out[f"r_{name}"] = 0.5 * (a["ret"] + b["ret"])
        out[f"rw_{name}"] = min(a["ret"], b["ret"])
        if name == "tp20_sl20_30s":
            out["slip_same"] = a["slip"]; out["slip_next"] = b["slip"]
            out["landed"] = 0.5 * (a["landed"] + b["landed"])
    # excursions from the (next-slot) fill state, for exit design
    e = b["entry_row"] if b["landed"] else j
    p0 = c.px[e]
    for w, nm in ((140, "30s"), (560, "2m")):
        m = (c.slot > c.slot[e]) & (c.slot <= c.slot[e] + w)
        out[f"mfe_{nm}"] = float(c.px[m].max() / p0 - 1) if m.any() else 0.0
        out[f"mae_{nm}"] = float(c.px[m].min() / p0 - 1) if m.any() else 0.0
    return out


def build(tapes, creates_rows, size=0.5, co=None, seed=7, trig_filter=None):
    co = co or Costs()
    rng = np.random.default_rng(seed)
    rows = []
    for r in creates_rows:
        c = tapes.get(r["mint"])
        if c is None or len(c.slot) < 2:
            continue
        s0 = r["slot"]
        for tname, j in trigger_rows(c, s0).items():
            if trig_filter and tname not in trig_filter:
                continue
            f = features(c, j, s0, r["creator"])
            f.update(outcomes(c, j, size, co, rng))
            f.update(mint=r["mint"], creator=r["creator"], trig=tname, slot0=s0, trig_slot=int(c.slot[j]),
                     mayhem=r.get("mayhem"), bt=r.get("bt"))
            rows.append(f)
    return rows
