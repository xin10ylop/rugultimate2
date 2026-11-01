"""Realistic-fill simulator for pump.fun bonding-curve trades.

Design rules (learned from the previous bot's live failure):
  * We are NEVER first. A trigger seen at trade row j (slot s) is filled later:
      - "same"  : behind m ~ U{5..13} further trades of this coin in slot s (or end of slot s)
      - "next"  : after every slot-s trade, behind m ~ U{0..2} trades of slot s+1
    Both variants are simulated; EV uses their mean, and the worst one is reported too.
  * We do not take anyone's place: the recorded tape is kept as-is and our own price impact
    is charged on entry AND on exit (exit sells into the real curve, i.e. conservative).
  * Every landed tx pays fixed costs (priority fee + tip); a refused buy (price limit) still pays.
  * Fees: pump.fun protocol + creator fee, taken from the tape (fee_bps + cfee_bps) per coin.
Prices are post-trade curve states (vsol, vtok) from TradeEvents, so the curve is exact.
"""
from dataclasses import dataclass, field
import numpy as np

LAM = 1e9


@dataclass
class Costs:
    buy_fixed: float = float(__import__("os").environ.get("BUY_FIXED", "0.0015"))   # SOL: priority + tip, landed buy
    sell_fixed: float = float(__import__("os").environ.get("SELL_FIXED", "0.0015")) # SOL: priority + tip, sell
    fail_fixed: float = float(__import__("os").environ.get("FAIL_FIXED", "0.0006")) # SOL: refused buy still pays
    max_slip: float = 0.20       # refuse the buy if fill price > trigger price * (1+max_slip)


@dataclass
class Exit:
    tp: float = 0.30             # take profit on spot price vs our average fill price
    sl: float = 0.25             # stop loss
    max_slots: int = 140         # time stop (~30 s at 214 ms slots)
    trail: float = 0.0           # trailing stop from peak (0 = off)
    trail_arm: float = 0.0       # trailing stop arms once peak >= entry*(1+trail_arm)


@dataclass
class Coin:
    """Chain-ordered tape of one coin: arrays aligned per trade."""
    mint: str
    slot: np.ndarray
    idx: np.ndarray
    vsol: np.ndarray   # post-trade virtual SOL reserve (lamports)
    vtok: np.ndarray   # post-trade virtual token reserve (raw)
    fee_rate: float    # total fee fraction per side
    extra: dict = field(default_factory=dict)

    @property
    def px(self):
        return self.vsol / self.vtok


def landing_rows(c: Coin, j: int, variant: str, rng_m: int):
    """Return the tape row whose post-trade state our tx executes on (we execute right after it)."""
    s = c.slot[j]
    n = len(c.slot)
    # rows still in slot s after j
    end_same = j
    while end_same + 1 < n and c.slot[end_same + 1] == s:
        end_same += 1
    if variant == "same":
        return min(j + rng_m, end_same)
    # next slot: after all slot-s trades, then behind rng_m trades of the next slot that has trades
    k = end_same
    if k + 1 >= n:
        return k
    nxt_slot = c.slot[k + 1]
    if nxt_slot > s + 1:          # nothing traded in slot s+1: we are first there
        return k
    r = k
    m = 0
    while r + 1 < n and c.slot[r + 1] == nxt_slot and m < rng_m:
        r += 1; m += 1
    return r


def buy_tokens(vsol, vtok, sol_gross, fee_rate):
    net = sol_gross / (1 + fee_rate)
    k = vsol * vtok
    new_vsol = vsol + net * LAM
    return vtok - k / new_vsol  # raw tokens


def sell_sol(vsol, vtok, tokens, fee_rate):
    k = vsol * vtok
    out = vsol - k / (vtok + tokens)
    return out / LAM * (1 - fee_rate)


def simulate(c: Coin, j: int, size: float, ex: Exit, co: Costs, variant: str, m_entry: int, m_exit: int):
    """Simulate one trade triggered at row j. Returns dict or None if coin tape too short."""
    n = len(c.slot)
    trig_px = c.px[j]
    e = landing_rows(c, j, variant, m_entry)
    fill_state_px = c.px[e]
    toks = buy_tokens(c.vsol[e], c.vtok[e], size, c.fee_rate)
    avg_px = size * LAM / toks  # lamports per raw token incl. fee
    if fill_state_px > trig_px * (1 + co.max_slip):
        return dict(landed=False, pnl=-co.fail_fixed, ret=-co.fail_fixed / size, entry_row=e, exit_row=e,
                    reason="refused", hold_slots=0, slip=fill_state_px / trig_px - 1)
    entry_slot = c.slot[e]
    ref = avg_px  # TP/SL measured on spot vs our all-in average price
    peak = c.px[e]
    reason = "end"
    x = n - 1
    for r in range(e + 1, n):
        p = c.px[r]
        peak = max(peak, p)
        if p >= ref * (1 + ex.tp):
            reason, x = "tp", r; break
        if p <= ref * (1 - ex.sl):
            reason, x = "sl", r; break
        if ex.trail > 0 and peak >= ref * (1 + ex.trail_arm) and p <= peak * (1 - ex.trail):
            reason, x = "trail", r; break
        if c.slot[r] - entry_slot >= ex.max_slots:
            reason, x = "time", r; break
    else:
        x = n - 1
        # coin went quiet: time stop at the last known state (curve still quotes)
        reason = "end"
    if reason == "end":
        xr = x
    else:
        xr = landing_rows(c, x, variant, m_exit)
    proceeds = sell_sol(c.vsol[xr], c.vtok[xr], toks, c.fee_rate)
    pnl = proceeds - size - co.buy_fixed - co.sell_fixed
    return dict(landed=True, pnl=pnl, ret=pnl / size, entry_row=e, exit_row=xr, reason=reason,
                hold_slots=int(c.slot[xr] - entry_slot), slip=fill_state_px / trig_px - 1)


def simulate_both(c: Coin, j: int, size: float, ex: Exit, co: Costs, rng=None):
    """Average of 'same slot' and 'next slot' landings (user's 50/50), with random queue depth."""
    rng = rng or np.random.default_rng(0)
    a = simulate(c, j, size, ex, co, "same", int(rng.integers(5, 14)), int(rng.integers(5, 14)))
    b = simulate(c, j, size, ex, co, "next", int(rng.integers(0, 3)), int(rng.integers(0, 3)))
    return a, b
