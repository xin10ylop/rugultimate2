"""Walk-forward test of the Operator Playbook idea.

For each launch L of an operator key K (funder / upstream / name...), using ONLY earlier launches of K
whose 10-minute outcome window closed before L was created:
  * pick the exit policy with the best mean realistic-fill return on those earlier launches
  * trade L with it if the earlier evidence passes the gate (n >= MIN_N, mean > GATE, lower bound > 0)
Report the realised returns of the traded launches. Nothing about L itself is used to decide.
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import numpy as np, polars as pl
from load import load_raw
from coins import build_tapes
from grid import grid_returns, POLICIES
from sim import Costs

D = os.environ.get("RUG_OUT", os.path.join(os.path.dirname(__file__), "..", "..", "data", "derived"))
SIZE = float(os.environ.get("SIZE", "0.25"))
TRIG_AGE = int(os.environ.get("TRIG_AGE", "2"))
HORIZON = 2800 + 50  # slots: longest policy window + margin


def build_grid(cache=os.path.join(D, f"grid_age{TRIG_AGE}_s{SIZE}_f{os.environ.get('BUY_FIXED','0.0015')}.npz")):
    F = pl.read_parquet(os.path.join(D, "launches.parquet"))
    if os.path.exists(cache) and os.path.getmtime(cache) > os.path.getmtime(os.path.join(D, "launches.parquet")):
        z = np.load(cache, allow_pickle=True)
        return F, dict(zip(z["mints"], z["R"])), dict(zip(z["mints"], z["W"]))
    Dr = load_raw(kinds=("trades", "creates"))
    T = build_tapes(Dr["trades"], Dr["creates"])
    rng = np.random.default_rng(11); co = Costs()
    R, W = {}, {}
    for r in F.iter_rows(named=True):
        c = T.get(r["mint"])
        if c is None:
            continue
        rel = c.slot - r["slot"]
        w = np.nonzero(rel >= TRIG_AGE)[0]
        if not len(w):
            continue
        R[r["mint"]], W[r["mint"]] = grid_returns(c, int(w[0]), SIZE, co, rng)
    np.savez(cache, mints=np.array(list(R.keys())), R=np.array(list(R.values())), W=np.array(list(W.values())))
    return F, R, W


def walk_forward(F, R, key, min_n=3, gate=0.03, lb_z=1.0, max_slot=None):
    F = F.filter(pl.col(key).is_not_null()).sort("slot")
    hist = {}
    trades = []
    for r in F.iter_rows(named=True):
        m = r["mint"]
        if m not in R:
            continue
        k = r[key]; s = r["slot"]
        prev = [(ps, pm) for ps, pm in hist.get(k, []) if ps + HORIZON <= s]
        if len(prev) >= min_n:
            M = np.array([R[pm] for _, pm in prev])
            mu = M.mean(0)
            best = int(np.argmax(mu))
            sd = M[:, best].std(ddof=1) if len(prev) > 1 else 1.0
            lb = mu[best] - lb_z * sd / np.sqrt(len(prev))
            if mu[best] > gate and lb > 0:
                trades.append(dict(mint=m, key=k, slot=s, n_prev=len(prev), policy=POLICIES[best],
                                   exp=mu[best], lb=lb, ret=R[m][best]))
        hist.setdefault(k, []).append((s, m))
    return pl.DataFrame(trades) if trades else pl.DataFrame()


if __name__ == "__main__":
    t0 = time.time()
    F, R, W = build_grid()
    print(f"launches={F.height} with grid={len(R)} build {time.time()-t0:.0f}s size={SIZE} trig_age={TRIG_AGE}")
    allR = np.array(list(R.values()))
    print(f"unconditional best policy mean (in-sample, all launches): {allR.mean(0).max():+.4f} {POLICIES[int(allR.mean(0).argmax())]}")
    F = F.with_columns(pl.col("name").str.to_lowercase().str.strip_chars().alias("name_key"),
                       (pl.col("funder") + ":" + pl.col("amt_key").cast(pl.Utf8)).alias("funder_amt"))
    for key in ("funder", "funder_amt", "upstream", "creator", "name_key", "uri_host"):
        for min_n, gate in ((3, 0.03), (5, 0.05)):
            t = walk_forward(F, R, key, min_n=min_n, gate=gate)
            if t.height == 0:
                print(f"{key:11s} min_n={min_n} gate={gate}: no trades"); continue
            half = t["slot"].median()
            h1 = t.filter(pl.col("slot") <= half)["ret"]; h2 = t.filter(pl.col("slot") > half)["ret"]
            print(f"{key:11s} min_n={min_n} gate={gate}: trades={t.height:4d} keys={t['key'].n_unique():3d} "
                  f"mean={t['ret'].mean():+.4f} med={t['ret'].median():+.4f} win={(t['ret']>0).mean():.2f} "
                  f"h1={h1.mean():+.4f} h2={h2.mean():+.4f} exp={t['exp'].mean():+.3f} sumPnL={t['ret'].sum()*SIZE:+.2f} SOL")


def walk_forward_cluster(L, R, min_n=3, gate=0.03, lb_z=1.0, policy_idx=None):
    """Like walk_forward, but the track record is the operator cluster's prior members at decision time."""
    slot_of = dict(zip(L["mint"].to_list(), L["slot"].to_list()))
    trades = []
    for r in L.iter_rows(named=True):
        m = r["mint"]
        if m not in R:
            continue
        s = r["slot"]
        prev = [pm for pm in (r["prior"] or []) if pm in R and slot_of.get(pm, 1e18) + HORIZON <= s]
        if len(prev) < min_n:
            continue
        M = np.array([R[pm] for pm in prev])
        if policy_idx is not None:
            M = M[:, policy_idx]
        mu = M.mean(0); best = int(np.argmax(mu))
        sd = M[:, best].std(ddof=1)
        lb = mu[best] - lb_z * sd / np.sqrt(len(prev))
        if mu[best] > gate and lb > 0:
            bi = policy_idx[best] if policy_idx is not None else best
            trades.append(dict(mint=m, key=r["cluster"], slot=s, n_prev=len(prev), policy=POLICIES[bi],
                               exp=mu[best], lb=lb, ret=R[m][bi]))
    return pl.DataFrame(trades) if trades else pl.DataFrame()


def report(name, t):
    if t.height == 0:
        print(f"{name}: no trades"); return
    half = t["slot"].median()
    h1 = t.filter(pl.col("slot") <= half)["ret"]; h2 = t.filter(pl.col("slot") > half)["ret"]
    h1 = h1 if h1.len() else pl.Series([float("nan")]); h2 = h2 if h2.len() else pl.Series([float("nan")])
    print(f"{name}: trades={t.height:4d} keys={t['key'].n_unique():3d} mean={t['ret'].mean():+.4f} "
          f"med={t['ret'].median():+.4f} win={(t['ret']>0).mean():.2f} h1={h1.mean():+.4f} h2={h2.mean():+.4f} "
          f"exp={t['exp'].mean():+.3f} sumPnL={t['ret'].sum()*SIZE:+.2f} SOL")
