"""Rebuild the event table (features + realistic-fill outcomes) and the launch/funding table."""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import polars as pl
from load import load_raw, load_transfers
from coins import build_tapes
from events import build
from operators import creator_funding

OUT = os.environ.get("RUG_OUT", os.path.join(os.path.dirname(__file__), "..", "..", "data", "derived"))
os.makedirs(OUT, exist_ok=True)
t0 = time.time()
D = load_raw(kinds=("trades", "creates", "completes")); X = load_transfers()
tr, cr = D["trades"], D["creates"]
print("loaded", {k: v.shape for k, v in D.items()}, X.shape, f"{time.time()-t0:.0f}s", flush=True)
T = build_tapes(tr, cr)
trig = sys.argv[1].split(",") if len(sys.argv) > 1 else None
E = pl.DataFrame(build(T, cr.iter_rows(named=True), trig_filter=trig))
E.write_parquet(os.path.join(OUT, "events.parquet"))
F = creator_funding(cr, X)
F.write_parquet(os.path.join(OUT, "launches.parquet"))
D["completes"].write_parquet(os.path.join(OUT, "completes.parquet"))
print("events", E.shape, "launches", F.shape, f"{time.time()-t0:.0f}s")
