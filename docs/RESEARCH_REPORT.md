# Research report: pump.fun strategy search (Nov 2026 data)

**Status: no strategy has passed validation.** Every family tested has negative expected value for a
trader who is never first in the block, once realistic fills and costs are applied. Nothing in this
repo should be run with real money as a profit strategy. Paper mode and the month-long collection
exist to keep testing honestly.

## 1. Data

Complete confirmed blocks were pulled with `getBlock` from several free RPCs
(`research/collector/block_collector.py`). Order inside each block is exact. Failed transactions,
fees, tips and every SOL transfer of at least 0.01 SOL are kept.

| | |
|---|---|
| Span | slots 464,156,733 – 464,276,790, about 7.15 hours of chain time (1 Nov 2026) |
| Launches (CreateEvent) | 12,062; 30% Mayhem mode |
| Bonding-curve trades | 1.14 M |
| Successful / failed pump txs | 7.24 M / 11.46 M |
| PumpSwap swaps | 2.8 M |

This is hours of data, not the one month planned. The conclusions below are strong where effects are
large and consistent across halves of the data. They are weaker for niche sub-strategies with small
samples.

## 2. How trades were scored (the lessons from the previous bot, applied)

`research/lib/sim.py` and `grid.py` score trades the same way as `engine/src/fills.rs`.

- **Never first.** A trigger at trade row *j* fills either behind 5–13 more trades of the coin in the
  same slot, or after the slot behind 0–2 trades of the next slot. Each case gets 50% weight, taken
  from your landing table. A buy is refused if the fill price is more than 20% above the trigger
  price, and a refused buy still pays its fee.
- **No taking anyone's place.** The real tape is kept. Our own price impact is charged on entry and
  again on exit (we sell into the real curve). This is conservative.
- **Costs.**
  - Curve fee: 1.25% per side (0.95% protocol + 0.30% creator), measured from TradeEvents.
  - Fixed cost: 0.0015 SOL per landed tx.
  - Exits land with the same delay model as entries.
- **Point-in-time only.**
  - Operator track records use only earlier launches whose 10-minute outcome window had closed.
  - Models use a time split: train on the first 60% of slots, then a gap, then test.
  - One position per coin.

At 0.25 SOL a round trip costs about 5% before the coin moves at all.

## 3. Results

### 3.1 Unconditional
Buying a random coin at a random moment, at a realistic fill, loses −5% to −19% depending on the
horizon (−13.6% at 10 s, −19.5% at 2 min). That is costs of about 5% plus strong negative drift for
anyone who is not an insider.

### 3.2 Operator following (the videos' method, automated)
Operators were fingerprinted point-in-time by creator wallet, funder, funder+amount, distinctive
dev-buy amount, coin name, upstream funder and shared early insiders (`lib/operators.py`,
`lib/cluster.py`).

- **Character persists.** Between consecutive launches of the same creator:
  - rug within 2 min: correlation **+0.36**
  - dead launch: correlation **+0.28**
  - pump size: correlation **+0.16**
- **Late-entry PnL does not persist** (correlation 0.00 to +0.04).
- **Walk-forward with policy selection** (best of 128 exits on the operator's history; 8,312 scored
  launches, minimum history 5, gate +5%):
  - creator: −8.2%/trade (862 trades)
  - funder: −6.3%
  - name: −4.7%
  - upstream funder: −19%
  - The history predicted about +35%. That gap is pure selection noise.
- **Fixed policies, no selection:** all 24 key × policy cells are negative, at both entry ages
  (create tx and slot +2).
- **Small early positives** (+3% to +15% on 20–90 trades in the first hour of data) disappeared as
  the data grew. Watch for this pattern in any future result.

Why it fails: an operator that a late entrant could reliably profit from would be losing money to
that entrant. Operators adapt ("farming"), and the profitable part of the pump goes to insiders and
first snipers.

### 3.3 Flow / microstructure model
- Setup:
  - 148k–257k triggers through each coin's life.
  - 33 features: flows, unique buyers, fresh-wallet buying, drawdown, real SOL in the curve, dev
    sells, Mayhem share.
  - Optional operator-history features (18 more).
  - LightGBM, time split.
- It ranks well out of sample. On the full 7-hour data (257k triggers), correlation reaches 0.65
  for TP15/SL15/30s.
- The best decile is still −4.1% to −6.9%, and the best percentile −1.3% to −12.8%.
- Adding operator features doesn't change that. The only positive cells are lottery tails with
  2–31 trades.

### 3.4 Mayhem mode (≈30–50% of launches)
- The agent `BwWK17cb…de6s` trades by rescaling the curve's virtual SOL reserve. A 0.005 SOL agent
  sell moved price −8%; a 0.25 SOL agent buy moved it +38%.
- Its direction is independent of state (buy share 0.50 in every bin), its steps have no
  autocorrelation, and E[exp(step)] = 1.0008 over 30,713 steps.
- It is a fair random walk by design: noise, not an edge. Most serial ruggers use Mayhem coins (free
  apparent activity).
- **Warning:** after the agent pushes virtual SOL down, even 0.25 SOL buys lose about 66% to their
  own impact.

### 3.5 Other mechanical events
- **Liquidity dips** (large non-creator sells of 10% or more): no rebound. −7% to −18% on normal
  coins, about −66% on Mayhem coins (own impact).
- **Graduation:** the PumpSwap pool opens at the final curve price (median gap −0.02%). After a pop
  of +11% at 5 s, graduated coins fall a median −90% within 10 minutes. Not usable long-only.
- **PumpSwap:** fees are dynamic, with a median of 1.04%/side (LP 0.20% + protocol 0.05% + creator
  0.05–0.95%).
  - In the 0.30% tier (bigger coins), the median 5-minute return is −1.1%: costs, no edge.
  - Freshly graduated coins lose about −18% to −24% over 5 minutes.
- **Funding "dry powder":** fresh wallets funded by the creator's funder almost never buy the coin
  (78 of 81,715). Funders are mostly hubs, and operators pump with other wallets.

## 4. Facts worth money regardless

1. **Stop overpaying priority fees.** For successful curve buys, priority + tip is:

   | percentile | lamports |
   |---|---|
   | 25th | 10,000 |
   | 50th | 24,000 |
   | 75th | 100,000 |
   | 90th | 405,000 |
   | 99th | 7.0 M |

   Only 1.8% of buyers pay 3.4M lamports or more, which is what the previous bot paid on every
   buy. A strategy that isn't racing can pay about 100× less.
2. **Slots are ~214 ms** (about 4.67/s), so "next slot" means 0.2–0.4 s.
3. **Failed bot transactions outnumber successful pump transactions 1.6:1.** Racing is a crowded
   loss-maker.
4. **Never size into a thin Mayhem curve.** Check virtual SOL before any buy; the engine's
   simulator does this exactly.
5. **2026 transaction layout:** the buy/sell instructions carry two extra accounts beyond the IDL —
   the `bonding-curve-v2` PDA and a buyback-fee recipient from Global. Verified with
   `simulateTransaction`.

## 5. What would change the verdict

The search space is not exhausted. These are the remaining avenues, ranked by prior:

1. **A month of data** (the original plan). It allows per-operator tests with 30+ launches per key
   and regime and time-of-day splits. Rerun `analysis/fixed_policy.py`, `playbook.py` and
   `model.py`.
   - Go/no-go gate for paper trading:
     - mean > +3% after costs in **both** halves and in each of 4 time folds
     - at least 300 trades
     - no single operator above 20% of PnL
2. **Exit-side precursors.** The rug comes in one block. Token consolidation transfers 1–2 slots
   before the dump (the video's "front-run sell") need SPL token-transfer capture, which isn't
   collected yet. This could turn operator riding from negative to positive only if exits get ahead
   of the dump.
3. **Being first (infrastructure, not strategy).** Shred-level feeds and bundle placement change
   the landing model, and every result above should then be re-scored with the new landing
   numbers. Your measured landing makes first-slot strategies unreachable today.

Anything that passes the gate goes to paper mode (`rugbot paper`) for at least 2 weeks, then live
at 0.02–0.05 SOL with the risk limits in `engine/src/risk.rs`, before any size increase.
