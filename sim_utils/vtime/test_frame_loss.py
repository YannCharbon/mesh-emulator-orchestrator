#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests of the central medium's frame loss that does not depend on the link
(docs/MEDIUM.md §12.2): random independent loss (--medium-loss, as ns-3's
--loss) and frame-loss bursts (--medium-loss-burst, a Gilbert-Elliott
process that destroys frames in its bad state).

    python3 sim_utils/vtime/test_frame_loss.py build-vt/wssimserver [--sock /tmp/wssim-loss.sock]

The burst process from --medium-var-dump samples (no run): share of time in
the bad state, mean sojourns, the link, receiver and node scopes,
reciprocity, a process of its own (not the attenuation bursts'). Then the
medium with fake nodes (test_conductor.py's): the loss rate of each kind on
a perfect link and a strong one against its expected value, independence
between receivers (random loss) and runs of lost frames (bursts), the
counts of the server's summary against the medium log, determinism by seed,
the options. About 10 s. Exit status 0 when every test passes.
"""
import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_conductor as tc  # noqa: E402
from test_conductor import Radio, Server, check, got, medium_run  # noqa: E402
from test_channel import mean, runs  # noqa: E402

ARGS = None


def dump(opts, specs, seed="1"):
    """--medium-var-dump samples with the loss-burst column: {spec: [bad]}."""
    env = dict(os.environ, WSSIM_SEED=seed)
    args = [ARGS.bin, *tc.pin_basic(opts)] + [f"--medium-var-dump={s}" for s in specs] + ["unused_socket"]
    p = subprocess.run(args, capture_output=True, text=True, env=env, timeout=120)
    check(p.returncode == 0, f"dump exit {p.returncode}: {p.stderr[-500:]}")
    out, i = {}, 0
    lines = p.stdout.split("\n")
    for s in specs:
        n = int(s.split(",")[4])
        rows = [ln.split() for ln in lines[i:i + n]]
        check(all(len(r) == 7 for r in rows), "dump: no seventh column with --medium-loss-burst")
        out[s] = [(int(r[5]), int(r[6])) for r in rows]     # (attenuation burst, loss burst)
        i += n
    return out


# ---- the process

def test_process():
    """Good 20 s, bad 2 s, a sample every 0.05 s over 2e4 s: the share of time
    in the bad state 2 / 22 (within 0.02), mean sojourns 2 s and 20 s (within
    15 % and 20 %), exponential (coefficient of variation near 1); two links
    of one receiver independent per link, together per receiver (rx); per
    node, a link bad when either end is (1 - (1 - p)^2); both directions
    together by default, apart with --medium-reciprocal=off; a process of its
    own, not the attenuation bursts' (--medium-burst with the same values)."""
    specs = ["1,2,0,0.05,400000", "1,3,0,0.05,400000", "2,1,0,0.05,400000"]
    r = dump(["--medium-loss-burst=20,2,1", "--medium-burst=20,2,20"], specs)
    bad = [v[1] for v in r[specs[0]]]
    share = mean(bad)
    rr = runs(bad)
    mb, mg = mean(rr[1][1:-1]) * 0.05, mean(rr[0][1:-1]) * 0.05
    cv = math.sqrt(mean([(x * 0.05) ** 2 for x in rr[1]]) - (mean(rr[1]) * 0.05) ** 2) / (mean(rr[1]) * 0.05)
    other = [v[1] for v in r[specs[1]]]
    both = mean([a & b for a, b in zip(bad, other)])
    att = [v[0] for v in r[specs[0]]]
    both_kinds = mean([a & b for a, b in zip(bad, att)])
    print(f"     bad share {share:.4f}, mean bad {mb:.2f} s ({len(rr[1])} bursts), good {mg:.1f} s, CV {cv:.2f}; "
          f"two links of a receiver both bad {both:.4f}; with the attenuation bursts {both_kinds:.4f}")
    check(abs(share - 2 / 22) < 0.02, f"bad share {share}")
    check(abs(mb - 2) < 0.3 and abs(mg - 20) < 4, f"sojourns {mb} {mg}")
    check(0.8 < cv < 1.2, f"CV {cv}")
    check(both < 0.03, f"per link: both bad {both}")
    check(both_kinds < 0.03, f"the loss bursts follow the attenuation bursts: {both_kinds}")
    check(bad == [v[1] for v in r[specs[2]]], "per link: the two directions differ with reciprocity")
    r = dump(["--medium-loss-burst=20,2,1", "--medium-reciprocal=off"], specs)
    rev = mean([a & b for a, b in zip([v[1] for v in r[specs[0]]], [v[1] for v in r[specs[2]]])])
    check(rev < 0.03, f"--medium-reciprocal=off: the two directions together {rev}")
    r = dump(["--medium-loss-burst=20,2,1,rx"], specs)
    check([v[1] for v in r[specs[0]]] == [v[1] for v in r[specs[1]]], "per receiver: links of 1 differ")
    specs = ["1,2,0,0.05,400000", "3,1,0,0.05,400000"]
    r = dump(["--medium-loss-burst=20,2,1,node"], specs)
    x, y = [v[1] for v in r[specs[0]]], [v[1] for v in r[specs[1]]]
    p = 2 / 22
    both = mean([a & b for a, b in zip(x, y)])
    print(f"     per node: bad share {mean(x):.4f} (expected {1 - (1 - p) ** 2:.4f}), links sharing a node both bad "
          f"{both:.4f} (expected {p + p * p * (1 - p):.4f})")
    check(abs(mean(x) - (1 - (1 - p) ** 2)) < 0.03, f"per node: bad share {mean(x)}")
    check(abs(both - (p + p * p * (1 - p))) < 0.03, f"per node: both bad {both}")


# ---- the medium

def stream(n, every_us, first=1000):
    return {first + k * every_us: [("tx", {})] for k in range(n)}


def lost_series(radio):
    return [0 if m["ok"] else 1 for t, m in got(radio, "xv")]


def destroyed(out, opt):
    for ln in out.split("\n"):
        if f"frames destroyed by --{opt}" in ln:
            return int(ln.split("medium: ")[1].split()[0])
    return None


def test_random_loss():
    """--medium-loss=0.2: node 1 sends 5000 frames, one every 0.2 s, heard by
    2 over a perfect link (an edge without a value) and by 3 at -60 dBm.
    Each loses 20 % (within 4 sigma), the frames lost at both are 4 %
    (independent receivers, within 4 sigma), the runs of losses are as short
    as independent draws give (mean run 1 / (1 - 0.2) = 1.25 within 0.1);
    the server's count equals the noise outcomes of the medium log, which
    has no other loss on these links."""
    n, p = 5000, 0.2
    r = {1: Radio(1, stream(n, 200000)), 2: Radio(2), 3: Radio(3)}
    d, out, rows = medium_run(r, [(1, 2, None), (1, 3, -60)], opts=["--medium-loss=0.2"])
    a, b = lost_series(r[2]), lost_series(r[3])
    check(len(a) == n and len(b) == n, f"verdicts {len(a)} {len(b)}")
    sigma = math.sqrt(n * p * (1 - p))
    both = sum(x & y for x, y in zip(a, b))
    sb = math.sqrt(n * p * p * (1 - p * p))
    ra = runs(a)[1]
    print(f"     lost {sum(a)} and {sum(b)} of {n} (expected {n * p:.0f}, sigma {sigma:.0f}), at both {both} "
          f"(expected {n * p * p:.0f}), mean run {mean(ra):.2f}; server count {destroyed(out, 'medium-loss')}")
    for x in (a, b):
        check(abs(sum(x) - n * p) < 4 * sigma, f"lost {sum(x)}, expected {n * p}")
    check(abs(both - n * p * p) < 4 * sb, f"lost at both {both}, expected {n * p * p}")
    check(abs(mean(ra) - 1 / (1 - p)) < 0.1, f"mean run {mean(ra)}")
    noise = sum(1 for x in rows if x[4] == "noise")
    check(destroyed(out, "medium-loss") == noise == sum(a) + sum(b), f"server count against the log's {noise}")


def test_burst_loss():
    """--medium-loss-burst=1,0.2,1: frames every 0.05 s for 300 s to 2
    (perfect link) and 3 (-60 dBm). Each loses 1 / 6 of its frames (the share
    of time in the bad state, within 0.045, 3.5 sigma of 250 bursts), in runs
    of about 0.2 s (mean run 3 to 7 frames); the two links are independent
    (link scope). With LOSS 0.5 the loss halves and the runs shorten. Every
    loss is counted by the server and is noise in the log; no other frame is
    lost."""
    n = 6000
    for loss, want_runs in ((1.0, (3, 7)), (0.5, (1.3, 2.6))):
        r = {1: Radio(1, stream(n, 50000)), 2: Radio(2), 3: Radio(3)}
        d, out, rows = medium_run(r, [(1, 2, None), (1, 3, -60)], opts=[f"--medium-loss-burst=1,0.2,{loss:g}"])
        a, b = lost_series(r[2]), lost_series(r[3])
        check(len(a) == n and len(b) == n, f"verdicts {len(a)} {len(b)}")
        want = loss / 6
        ra = runs(a)[1]
        both = sum(x & y for x, y in zip(a, b)) / n
        print(f"     loss {loss:g} in the bad state: lost {sum(a) / n:.4f} and {sum(b) / n:.4f} (expected {want:.4f}), "
              f"mean run {mean(ra):.1f} frames, longest {max(ra)}, both {both:.4f}; server count "
              f"{destroyed(out, 'medium-loss-burst')}")
        for x in (a, b):
            check(abs(sum(x) / n - want) < 0.045 * loss, f"lost {sum(x) / n}, expected {want}")
        check(want_runs[0] < mean(ra) < want_runs[1], f"mean run {mean(ra)}")
        check(both < 2 * (sum(a) / n) * (sum(b) / n) + 0.01, f"links not independent: {both}")
        noise = sum(1 for x in rows if x[4] == "noise")
        check(destroyed(out, "medium-loss-burst") == noise == sum(a) + sum(b), f"server count against the log's {noise}")
        check("frame-loss bursts per link (both directions together): good 1 s, bad 0.2 s" in out, "settings not printed")


def test_together():
    """Both options: the frames destroyed by --medium-loss are the same as
    with --medium-loss alone (its own draws, made first), bursts add
    theirs; the same seed gives the same medium log, another seed another."""
    def one(opts, seed="1"):
        r = {1: Radio(1, stream(3000, 100000)), 2: Radio(2)}
        d, out, rows = medium_run(r, [(1, 2, -70)], opts=opts, seed=seed)
        with open(os.path.join(ARGS.logdir, "medium.log")) as f:
            return lost_series(r[2]), f.read(), out
    alone, _, _ = one(["--medium-loss=0.1"])
    both, log_a, out = one(["--medium-loss=0.1", "--medium-loss-burst=5,1,1"])
    _, log_b, _ = one(["--medium-loss=0.1", "--medium-loss-burst=5,1,1"])
    _, log_c, _ = one(["--medium-loss=0.1", "--medium-loss-burst=5,1,1"], seed="2")
    rnd, bst = destroyed(out, "medium-loss"), destroyed(out, "medium-loss-burst")
    print(f"     --medium-loss alone {sum(alone)} lost; with bursts {sum(both)} lost ({rnd} random, {bst} in bursts)")
    check(all(y >= x for x, y in zip(alone, both)), "a frame lost to --medium-loss alone is received with bursts")
    check(rnd + bst == sum(both) and bst > 0, f"counts {rnd} + {bst} against {sum(both)}")
    check(log_a == log_b, "same seed, different medium logs")
    check(log_a != log_c, "another seed, same medium log")


def test_options():
    """Errors; both options refused with the legacy medium and without --vtime;
    without them the server prints none of their lines."""
    for args, msg in ((["--vtime=1", "--medium=legacy", "--medium-loss=0.1"], "need the central medium"),
                      (["--vtime=1", "--medium=legacy", "--medium-loss-burst=1,1,1"], "need the central medium"),
                      (["--medium-loss=0.1"], "needs --vtime"),
                      (["--medium-loss-burst=1,1,1"], "need the central medium"),
                      (["--vtime=1", "--medium-loss=1.5"], "from 0 to 1"),
                      (["--vtime=1", "--medium-loss-burst=1,1"], "expected GOOD_S,BAD_S,LOSS"),
                      (["--vtime=1", "--medium-loss-burst=1,1,2"], "from 0 to 1"),
                      (["--vtime=1", "--medium-loss-burst=1,1,0.5,all"], "link, rx or node"),
                      (["--vtime=1", "--medium-loss-burst=1,1,0.5,rx", "--medium-reciprocal=off"], "needs --medium-fading")):
        srv = Server(args)
        check(srv.wait() != 0 and msg in srv.output(), f"{args} accepted, or no \"{msg}\"")
    r = {1: Radio(1, {1000: [("tx", {})]}), 2: Radio(2)}
    d, out, rows = medium_run(r, [(1, 2, -80)])
    check("destroyed" not in out and "frame-loss" not in out, "without the options: loss lines printed")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("server", help="wssimserver binary (copied before use)")
    ap.add_argument("--sock", default="/tmp/wssim-loss.sock")
    ap.add_argument("--logdir", default=None)
    ap.add_argument("-k", default="", help="run only tests whose name contains this")
    ARGS = ap.parse_args()
    if ARGS.sock == "/tmp/sim_socket":
        sys.exit("refusing the default socket: it may belong to a real run")
    ARGS.logdir = ARGS.logdir or tempfile.mkdtemp(prefix="loss_")
    os.makedirs(ARGS.logdir, exist_ok=True)
    ARGS.copy = os.path.join(ARGS.logdir, "wssimserver-loss")
    shutil.copy2(ARGS.server, ARGS.copy)
    ARGS.bin = ARGS.copy
    tc.ARGS = ARGS
    tests = [
        ("process", test_process),
        ("random_loss", test_random_loss),
        ("burst_loss", test_burst_loss),
        ("together", test_together),
        ("options", test_options),
    ]
    failed = 0
    for name, fn in tests:
        if ARGS.k not in name:
            continue
        t0 = time.monotonic()
        try:
            fn()
            print(f"PASS {name} ({time.monotonic() - t0:.1f} s)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"logs in {ARGS.logdir}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
