"""Vectorised exit-policy grid: realistic-fill PnL of many (TP, SL, time) policies for one entry.

Same landing model as sim.py (never first; 'same' and 'next' slot variants averaged), exits sell
into the real curve after the exit landing delay. Returns a matrix [n_policies] of returns.
"""
import itertools
import numpy as np
from sim import Coin, Costs, landing_rows, buy_tokens, sell_sol

TPS = (0.10, 0.20, 0.30, 0.50, 0.75, 1.00, 1.50, 2.50)
SLS = (0.20, 0.35, 0.50, 0.95)
TMAX = (47, 140, 560, 2800)            # ~10 s, 30 s, 2 min, 10 min
POLICIES = list(itertools.product(TPS, SLS, TMAX))


def _one(c: Coin, j: int, size: float, co: Costs, variant: str, m_entry: int, m_exit: int):
    n = len(c.slot)
    e = landing_rows(c, j, variant, m_entry)
    if c.px[e] > c.px[j] * (1 + co.max_slip):
        return np.full(len(POLICIES), -co.fail_fixed / size), False
    toks = buy_tokens(c.vsol[e], c.vtok[e], size, c.fee_rate)
    ref = size * 1e9 / toks
    px = c.px[e + 1:]
    sl_ = c.slot[e + 1:] - c.slot[e]
    out = np.empty(len(POLICIES))
    # pre-compute exit landing rows for every possible trigger row (cheap: lookup on demand)
    cache = {}
    def exit_ret(r):
        if r not in cache:
            xr = r if r >= n - 1 else landing_rows(c, r, variant, m_exit)
            cache[r] = (sell_sol(c.vsol[xr], c.vtok[xr], toks, c.fee_rate) - size - co.buy_fixed - co.sell_fixed) / size
        return cache[r]
    if len(px) == 0:
        out[:] = exit_ret(n - 1)
        return out, True
    rel = px / ref
    for i, (tp, sl, tm) in enumerate(POLICIES):
        hit = (rel >= 1 + tp) | (rel <= 1 - sl) | (sl_ >= tm)
        k = np.argmax(hit)
        r = (e + 1 + k) if hit[k] else (n - 1)
        out[i] = exit_ret(r)
    return out, True


def grid_returns(c: Coin, j: int, size: float, co: Costs, rng):
    a, la = _one(c, j, size, co, "same", int(rng.integers(5, 14)), int(rng.integers(5, 14)))
    b, lb = _one(c, j, size, co, "next", int(rng.integers(0, 3)), int(rng.integers(0, 3)))
    return 0.5 * (a + b), np.minimum(a, b)
