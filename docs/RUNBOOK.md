# Runbook

## 1. Collect a month of data (Frankfurt box)

Point the collector at your own RPC (Triton HTTP endpoint). Public RPCs rate-limit and fall
behind. `|16` means 16 workers on that URL.

```bash
python3 research/collector/block_collector.py --out data/live --resume --rpc "https://YOUR_TRITON_RPC|16"
```

- It writes hourly `pump_tx_*.jsonl.gz` and `transfers_*.jsonl.gz`, at about 0.55 GB per chain-hour,
  so roughly 400 GB per month. Plan the disk accordingly, or convert finished hours to parquet with
  `research/lib/load.py` (it caches automatically).
- Stop it with `kill $(cat data/live/collector.pid)`. The shutdown is clean, and it resumes from
  `data/live/progress.txt`.
- Backfill older history with `research/collector/backfill.sh START_SLOT 3000 1`.

Not collected yet, needed for the exit-precursor test in the report (§5.2): SPL token balance
changes for pump mints (token consolidation before dumps). Add them to `parse_block` before the
month starts.

## 2. Re-run the research

```bash
cd research
python3 analysis/build_events.py age2      # events + launches/funding tables
python3 analysis/playbook.py               # operator walk-forward (policy selection)
TRIG_AGE=0 python3 analysis/fixed_policy.py   # entry on the create tx, fixed exits
python3 analysis/dense.py && python3 analysis/model.py   # flow model, time split
python3 analysis/pamm.py && python3 analysis/pswap.py    # graduation / PumpSwap
```

Gate before anything goes to paper: mean > +3% after costs in both halves and in 4 time folds,
at least 300 trades, and no single operator above 20% of PnL. Re-score with your real landing numbers
if your infrastructure changes.

## 3. Engine

```bash
cd engine && cargo build --release
./target/release/rugbot replay --start S --end E --rpc URL     # Rust backtest, same logic as paper
./target/release/rugbot paper --rpc URL --warmup 30000          # follows the tip, settles with the fill model
./target/release/rugbot dry-buy --mint M --creator C --as-wallet FUNDED_WALLET --rpc URL   # simulate only
```

- `paper` and `replay` write JSONL journals (`enter`, `skip`, `settle` with cumulative PnL and
  win rate).
- `dry-buy` builds the real 2026 buy transaction and runs `simulateTransaction` with
  `sigVerify=false`, so nothing is sent.

Live sending is deliberately not wired. The transaction builders (`pump.rs`) and the sender
(`rpc.rs::send`) exist, but no strategy has earned real money yet. Before going live:

1. Use a gRPC source (Triton Yellowstone). The JSON-RPC block poller is research-grade and too slow
   for live decisions.
2. Probe trades at 0.01 SOL to measure your own landing and costs, and feed them back into
   `fills.rs` / `sim.py`.
3. Run paper mode for at least 2 weeks with the gate met. Then go live at 0.02–0.05 SOL.

Risk limits are in `engine/src/risk.rs`:
- size
- max open positions
- daily loss limit
- per-operator loss streak pause
- per-operator rate limit (anti-farming)
- kill file `STOP`
