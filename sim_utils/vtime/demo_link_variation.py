#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""How losses cluster in time and across two parents, by loss model
(docs/MEDIUM.md §12.7).

    python3 sim_utils/vtime/demo_link_variation.py build-vt/wssimserver [--logdir DIR] [--sock PATH]

A child (node 1) sends a 100-byte frame every second for DUR (3600) virtual
seconds; two parents (2 and 3) hear it at the same mean level (LEVEL, -98
dBm). Fake nodes (test_conductor.py's), central medium, SEEDS seeds per
model. Two tables: every model at the same mean level, then at about the
same average loss (the shadowed links 5 dB stronger, the bursts rarer). For each model: the loss rate per parent, the loss runs (consecutive
frames lost at one parent: count, mean and longest), and the losses at both
parents against the product of their rates (independent losses give a
ratio of 1). About 2 s of wall time per run.
"""
import argparse
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_conductor as tc  # noqa: E402
from test_conductor import Radio, medium_run  # noqa: E402

# (name, options, mean level offset of the matched table from LEVEL)
MODELS = [
    ("empirical", [], 0),
    ("layered", ["--medium-rx-model=layered"], 0),
    ("layered+shadowing", ["--medium-rx-model=layered", "--medium-shadowing=4,60", "--medium-node-share=0.5"], 5),
    ("layered+bursts", ["--medium-rx-model=layered", "--medium-burst=300,10,15,node"], 0),
]
# The matched table: about the same average loss for every model (bursts
# rarer, the shadowed links 5 dB stronger)
MATCHED_BURSTS = "--medium-burst=3600,8,15,node"


def runs_of(lost):
    out, n = [], 0
    for x in lost + [False]:
        if x:
            n += 1
        elif n:
            out.append(n)
            n = 0
    return out


def one(opts, seed, n, level):
    r = {1: Radio(1, {1000 + k * 1000000: [("tx", {"payload": b"d" * 100})] for k in range(n)}),
         2: Radio(2), 3: Radio(3)}
    d, out, rows = medium_run(r, [(1, 2, level), (1, 3, level)], opts=opts, seed=str(seed))
    lost = {}
    for p in (2, 3):
        # From the medium log: a frame too weak to lock (below_lock) is lost too
        xv = sorted((fid, outcome == "ok") for t, src, dst, fid, outcome, dbm, sinr in rows if dst == p)
        tc.check(len(xv) == n, f"parent {p}: {len(xv)} outcomes")
        lost[p] = [not ok for fid, ok in xv]
    return lost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("server")
    ap.add_argument("--sock", default="/tmp/wssim-demo-var.sock")
    ap.add_argument("--logdir", default=None)
    ap.add_argument("--dur", type=int, default=3600)
    ap.add_argument("--level", type=float, default=-98)
    ap.add_argument("--seeds", default="1,2,3")
    a = ap.parse_args()
    a.logdir = a.logdir or tempfile.mkdtemp(prefix="demo_var_")
    os.makedirs(a.logdir, exist_ok=True)
    a.bin = os.path.join(a.logdir, "wssimserver-demo")
    shutil.copy2(a.server, a.bin)
    tc.ARGS = a
    seeds = [int(s) for s in a.seeds.split(",")]
    print(f"child -> 2 parents, 100-byte frames, 1 per s, {a.dur} s, seeds {seeds}")
    for table in ("same mean level", "about the same average loss"):
        print(f"\n{table}")
        print(f"{'model':20s} {'level':>6s} {'loss %':>7s} {'runs':>5s} {'mean run':>8s} {'longest':>7s} "
              f"{'in runs>=2 %':>12s} {'both lost':>9s} {'both/indep':>10s}")
        for name, opts, delta in MODELS:
            level = a.level
            if table != "same mean level":
                level += delta
                opts = [MATCHED_BURSTS if o.startswith("--medium-burst=") else o for o in opts]
            tot = lost_n = n_runs = run_sum = longest = in_long = both = 0
            indep = 0.0
            for s in seeds:
                lost = one(opts, s, a.dur, level)
                for p in (2, 3):
                    rr = runs_of(lost[p])
                    tot += len(lost[p])
                    lost_n += sum(lost[p])
                    n_runs += len(rr)
                    run_sum += sum(rr)
                    longest = max([longest] + rr)
                    in_long += sum(x for x in rr if x >= 2)
                both += sum(x and y for x, y in zip(lost[2], lost[3]))
                indep += sum(lost[2]) * sum(lost[3]) / a.dur
            ratio = f"{both / indep:10.1f}" if indep else f"{'-':>10s}"
            print(f"{name:20s} {level:6.0f} {100 * lost_n / tot:7.2f} {n_runs:5d} {run_sum / max(n_runs, 1):8.2f} "
                  f"{longest:7d} {100 * in_long / max(lost_n, 1):12.1f} {both:9d} {ratio}")

if __name__ == "__main__":
    main()
