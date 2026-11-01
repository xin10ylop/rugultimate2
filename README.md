# rugultimate2

Data-first research and an execution engine for pump.fun on Solana, built around one hard rule from
the previous bot's failure: **we are never first in the block**. Every backtest uses the measured
landing model, real fees and our own price impact. A strategy only counts if it survives those.

**Current verdict: no strategy has passed validation.** See [docs/RESEARCH_REPORT.md](docs/RESEARCH_REPORT.md)
for the numbers, what failed and why, and what could still change the verdict. Don't run anything
here with real money as a profit strategy.

## Layout

| Path | What it is |
|---|---|
| `research/collector/` | Complete-block collector (`getBlock`, several RPCs, exact in-block order, failed txs, fees/tips, SOL transfers) and a history backfill script |
| `research/lib/` | Loader (parquet cache), exact bonding-curve maths, never-first fill simulator, exit-policy grid, operator fingerprints and clustering, event-study builder |
| `research/analysis/` | One script per hypothesis: operator walk-forward (`playbook*.py`, `fixed_policy.py`), dense flow model (`dense.py`, `model*.py`), Mayhem, dips, graduation (`pamm.py`), PumpSwap (`pswap.py`), feature scan |
| `engine/` | `rugbot` (Rust): 2026 pump.fun decoder and buy/sell builders verified with `simulateTransaction`, fill model identical to research, point-in-time operator registry, risk limits, `replay` / `paper` / `dry-buy` modes |
| `engine/idl/` | On-chain Anchor IDLs of pump.fun, PumpSwap and the fee program |
| `docs/` | Research report and runbook |

## Quick start

See [docs/RUNBOOK.md](docs/RUNBOOK.md).

```bash
pip install aiohttp orjson base58 polars pyarrow lightgbm scikit-learn
```

```bash
python3 research/collector/block_collector.py --out data/live --rpc "https://YOUR_TRITON_RPC|16"
```

```bash
cd engine && cargo build --release && cargo test --release
```
