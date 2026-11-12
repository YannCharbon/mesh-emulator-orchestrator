#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""How losses cluster in time and across two parents, by loss model
(docs/MEDIUM.md §12.7).

    python3 sim_utils/vtime/demo_link_variation.py build-vt/wssimserver [--logdir DIR] [--sock PATH]

A child (node 1) sends a 100-byte frame every second for DUR (3600) virtual
seconds; two parents (2 and 3) hear it at the same mean level (LEVEL, -98
dBm). Fake nodes (test_conductor.py's), central medium, SEEDS seeds per
model. Two tables: every model at the same mean level, then at about the
same average loss (the shadowed links 5 dB stronger, the bursts rarer).

A third table, --hop: frequency hopping. The child sends 10 frames a second
(0.1 s apart, a third of the default coherence time) in mode 3 (150 kbit/s,
channels 200 kHz apart), on one channel or hopping over 35 channels in a
pseudo-random order (the parents follow), layered model, fading flat
(--medium-fading-bc=inf) or frequency selective (5.5 MHz, the default;
1 MHz). DUR / 4 per run. For each model: the loss rate per parent, the loss runs (consecutive
frames lost at one parent: count, mean and longest), and the losses at both
parents against the product of their rates (independent losses give a
ratio of 1). About 2 s of wall time per run.
"""
import argparse
import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_conductor as tc  # noqa: E402
from test_conductor import Radio, medium_run  # noqa: E402
from fake_node import RADIO_LISTEN  # noqa: E402

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


def one(opts, seed, n, level, period_us=1000000, hop=0, mode3=False):
    """hop: 0, one channel; else the frames hop over that many channels.
    mode3: 150 kbit/s frames with the FG25's 14-byte SHR."""
    rnd = random.Random(12345)
    chans = [rnd.randrange(hop) if hop else 0 for _ in range(n)]
    fields = {"payload": b"d" * 100}
    if mode3:
        fields.update(rate_bps=150000, shr_len=14)
    t0 = 1000
    child = {t0 + k * period_us: [("tx", dict(fields, chan=chans[k]))] for k in range(n)}
    par = {t0 + k * period_us - 200: [("radio", RADIO_LISTEN, chans[k])] for k in range(n)} if hop else {}
    r = {1: Radio(1, child), 2: Radio(2, dict(par)), 3: Radio(3, dict(par))}
    d, out, rows = medium_run(r, [(1, 2, level), (1, 3, level)], opts=opts, seed=str(seed))
    lost = {}
    for p in (2, 3):
        # From the medium log: a frame too weak to lock (below_lock) is lost too
        xv = sorted((fid, outcome == "ok") for t, src, dst, fid, outcome, dbm, sinr in rows if dst == p)
        tc.check(len(xv) == n, f"parent {p}: {len(xv)} outcomes")
        lost[p] = [not ok for fid, ok in xv]
    return lost


def row(name, level, seeds, run):
    tot = lost_n = n_runs = run_sum = longest = in_long = both = 0
    indep = 0.0
    for s in seeds:
        lost = run(s)
        n = len(lost[2])
        for p in (2, 3):
            rr = runs_of(lost[p])
            tot += len(lost[p])
            lost_n += sum(lost[p])
            n_runs += len(rr)
            run_sum += sum(rr)
            longest = max([longest] + rr)
            in_long += sum(x for x in rr if x >= 2)
        both += sum(x and y for x, y in zip(lost[2], lost[3]))
        indep += sum(lost[2]) * sum(lost[3]) / n
    ratio = f"{both / indep:10.1f}" if indep else f"{'-':>10s}"
    print(f"{name:28s} {level:6.0f} {100 * lost_n / tot:7.2f} {n_runs:5d} {run_sum / max(n_runs, 1):8.2f} "
          f"{longest:7d} {100 * in_long / max(lost_n, 1):12.1f} {both:9d} {ratio}")


HEAD = (f"{'model':28s} {'level':>6s} {'loss %':>7s} {'runs':>5s} {'mean run':>8s} {'longest':>7s} "
        f"{'in runs>=2 %':>12s} {'both lost':>9s} {'both/indep':>10s}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("server")
    ap.add_argument("--sock", default="/tmp/wssim-demo-var.sock")
    ap.add_argument("--logdir", default=None)
    ap.add_argument("--dur", type=int, default=3600)
    ap.add_argument("--level", type=float, default=-98)
    ap.add_argument("--seeds", default="1,2,3")
    ap.add_argument("--hop", action="store_true", help="also the hopping table")
    ap.add_argument("--hop-only", action="store_true", help="only the hopping table")
    a = ap.parse_args()
    a.logdir = a.logdir or tempfile.mkdtemp(prefix="demo_var_")
    os.makedirs(a.logdir, exist_ok=True)
    a.bin = os.path.join(a.logdir, "wssimserver-demo")
    shutil.copy2(a.server, a.bin)
    tc.ARGS = a
    seeds = [int(s) for s in a.seeds.split(",")]
    print(f"child -> 2 parents, 100-byte frames, 1 per s, {a.dur} s, seeds {seeds}")
    if not a.hop_only:
        for table in ("same mean level", "about the same average loss"):
            print(f"\n{table}")
            print(HEAD)
            for name, opts, delta in MODELS:
                level = a.level
                if table != "same mean level":
                    level += delta
                    opts = [MATCHED_BURSTS if o.startswith("--medium-burst=") else o for o in opts]
                row(name, level, seeds, lambda s: one(opts, s, a.dur, level))
    if a.hop or a.hop_only:
        n = a.dur * 10 // 4
        print(f"\nhopping: 10 frames a second, mode 3, {n // 10} s per run, layered (K 10, Tc 0.3 s)")
        print(HEAD)
        lay = ["--medium-rx-model=layered"]
        for name, opts, hop in (("one channel", lay, 0),
                                ("35 channels, flat fading", lay + ["--medium-fading-bc=inf"], 35),
                                ("35 channels, Bc 5.5 MHz", lay, 35),
                                ("35 channels, Bc 1 MHz", lay + ["--medium-fading-bc=1"], 35)):
            row(name, a.level, seeds, lambda s: one(opts, s, n, a.level, 100000, hop, True))

if __name__ == "__main__":
    main()
