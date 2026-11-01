#!/usr/bin/env bash
# Backfill contiguous history backwards from a start slot, in chunks.
# Usage: backfill.sh START_SLOT [CHUNK_SLOTS] [FIRST_CHUNK_INDEX]
cd "$(dirname "$0")/../.."
S=${1:?start slot}; CH=${2:-3000}; k=${3:-1}
RPCS=${RPCS:-"https://solana-rpc.publicnode.com/|4,https://api.mainnet-beta.solana.com|1,https://public.rpc.solanavibestation.com/|1"}
while true; do
  start=$((S - k*CH)); end=$((S - (k-1)*CH - 1))
  mkdir -p "data/back/c$k"
  python3 research/collector/block_collector.py --out "data/back/c$k" --start "$start" --end "$end" --rpc "$RPCS" >> data/back.log 2>&1
  echo "chunk $k done $start..$end $(date -u +%H:%M:%S)" >> data/back.log
  k=$((k+1))
done
