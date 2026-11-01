import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib")); sys.path.insert(0, os.path.dirname(__file__))
import numpy as np, polars as pl
from playbook import build_grid, walk_forward_cluster, report, POLICIES
from load import load_raw
from cluster import assign
F, R, W = build_grid()
Dr = load_raw(kinds=("trades", "creates"))
small = [i for i, p in enumerate(POLICIES) if p[0] in (0.2, 0.5, 1.0) and p[1] in (0.35, 0.95) and p[2] in (140, 560)]
for use in [("creator",), ("funder",), ("name",), ("insider",), ("devbuy",), ("creator", "funder", "name", "insider", "devbuy")]:
    L = assign(F, Dr["trades"], use=use)
    for min_n, gate in ((3, 0.03), (5, 0.05), (10, 0.03)):
        report(f"{'+'.join(use):35s} n>={min_n:2d} g={gate} full ", walk_forward_cluster(L, R, min_n, gate))
        report(f"{'+'.join(use):35s} n>={min_n:2d} g={gate} small", walk_forward_cluster(L, R, min_n, gate, policy_idx=small))
