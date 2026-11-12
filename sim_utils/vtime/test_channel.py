#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests of the central medium's link variation and layered loss model
(docs/MEDIUM.md §12).

    python3 sim_utils/vtime/test_channel.py build-vt/wssimserver [--sock /tmp/wssim-chan.sock]

Two kinds of tests. The processes themselves, from samples that the server
prints with --medium-var-dump (no run): the statistics of the shadowing
(standard deviation, autocorrelation at the decorrelation time), of the
fading (mean power, K, deep fades, autocorrelation at the coherence time),
of the bursts (share of time in the bad state, mean sojourns, their spread),
reciprocity, the common share of a node's links, determinism by seed and
independence from the order of the queries. Then the medium with fake nodes
(test_conductor.py's): the loss rate of the layered model under its default
fading against its average formula and against the empirical curve, the
combination of noise and interference, the medium log's columns against the
dump, determinism of a run, the options. Exit status 0 when every test
passes.
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
from test_conductor import Radio, Server, check, got, medium_run, NOISE_DBM  # noqa: E402

ARGS = None


def dump(opts, specs, seed="1"):
    """--medium-var-dump samples: {spec: [(t_us, shadow_db, fading_db, bad)]}."""
    env = dict(os.environ, WSSIM_SEED=seed)
    args = [ARGS.bin, *opts] + [f"--medium-var-dump={s}" for s in specs] + ["unused_socket"]
    p = subprocess.run(args, capture_output=True, text=True, env=env, timeout=120)
    check(p.returncode == 0, f"dump exit {p.returncode}: {p.stderr[-500:]}")
    out, i = {}, 0
    lines = p.stdout.split("\n")
    for s in specs:
        n = int(s.split(",")[4])
        rows = [ln.split() for ln in lines[i:i + n]]
        out[s] = [(int(r[2]), float(r[3]), float(r[4]), int(r[5])) for r in rows]
        i += n
    return out


def mean(x):
    return sum(x) / len(x)


def corr(x, y):
    mx, my = mean(x), mean(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return sxy / math.sqrt(sxx * syy)


def autocorr(x, lag):
    return corr(x[:-lag], x[lag:])


# Rician power (mean 1) and the layered curve averaged over it (§12.3)
from layered_fit import rician_cdf, per_layered_avg, per_empirical  # noqa: E402


# ---- the processes

def test_shadowing():
    """sigma 4 dB, tau 30 s, a sample every 3 s over 3e5 s (1e4 tau): the
    standard deviation within 5 %, the autocorrelation exp(-1) = 0.37 at tau
    and exp(-3) = 0.05 at 3 tau (within 0.04), the mean within 0.3 dB."""
    s = "1,2,0,3,100000"
    x = [r[1] for r in dump(["--medium-shadowing=4,30"], [s])[s]]
    sd = math.sqrt(mean([v * v for v in x]) - mean(x) ** 2)
    a1, a3 = autocorr(x, 10), autocorr(x, 30)
    print(f"     sigma {sd:.3f} dB, mean {mean(x):+.3f} dB, autocorrelation {a1:.3f} at tau, {a3:.3f} at 3 tau")
    check(abs(sd - 4) < 0.2, f"sigma {sd}")
    check(abs(mean(x)) < 0.3, f"mean {mean(x)}")
    check(abs(a1 - math.exp(-1)) < 0.04 and abs(a3 - math.exp(-3)) < 0.04, f"autocorrelation {a1} {a3}")
    # Between grid points (an offset of 1/3 of a grid step): the same spread
    s2 = "1,2,1.25,3,100000"
    y = [r[1] for r in dump(["--medium-shadowing=4,30"], [s2])[s2]]
    sd2 = math.sqrt(mean([v * v for v in y]) - mean(y) ** 2)
    check(abs(sd2 - 4) < 0.2, f"sigma between grid points {sd2}")


def test_fading():
    """Rician K = 10, coherence time 0.4 s, a sample every 0.1 s over 1e4 s
    (2.5e4 coherence times): mean power 1 (within 2 %), K from the moments
    E[P^2] / E[P]^2 = 1 + (2K + 1) / (K + 1)^2 (within 20 %), the share of
    samples more than 3 dB and 6 dB below the mean against the Rician
    distribution, the power's autocorrelation at the coherence time against
    (rho^2 + 2 K rho) / (1 + 2 K), rho = exp(-1). Then K = 0 (Rayleigh):
    P(power < 0.1) = 1 - exp(-0.1)."""
    for k in (10, 0):
        s = "3,4,0,0.1,100000"
        p = [10 ** (r[2] / 10) for r in dump([f"--medium-fading={k},0.4"], [s])[s]]
        m1, m2 = mean(p), mean([v * v for v in p])
        ratio = m2 / m1 ** 2
        # Solve 1 + (2K + 1) / (K + 1)^2 = ratio for K
        kk = 0.0
        for _ in range(200):
            f = 1 + (2 * kk + 1) / (kk + 1) ** 2 - ratio
            kk += f * 5 if kk + f * 5 >= 0 else -kk / 2
        rho = math.exp(-1)
        want_ac = (rho * rho + 2 * k * rho) / (1 + 2 * k)
        ac = autocorr(p, 4)
        print(f"     K {k}: mean power {m1:.4f}, K from moments {kk:.2f}, power autocorrelation at Tc {ac:.3f} "
              f"(theory {want_ac:.3f})")
        check(abs(m1 - 1) < 0.02, f"K {k}: mean power {m1}")
        check(abs(ac - want_ac) < 0.04, f"K {k}: autocorrelation {ac}, theory {want_ac}")
        if k:
            check(abs(kk - k) < 0.2 * k, f"K from moments {kk}")
            for db in (3, 6):
                share = sum(v < 10 ** (-db / 10) for v in p) / len(p)
                want = rician_cdf(k, 10 ** (-db / 10))
                # Samples 0.1 s apart are correlated: about 4 per coherence
                # time, so the effective count is a quarter
                sigma = math.sqrt(want * (1 - want) / (len(p) / 4))
                print(f"       {db} dB below the mean: {share:.4f} of the samples, Rician {want:.4f}")
                check(abs(share - want) < 4 * sigma + 1e-4, f"share {db} dB below: {share}, Rician {want}")
        else:
            share = sum(v < 0.1 for v in p) / len(p)
            want = 1 - math.exp(-0.1)
            print(f"       Rayleigh: power below 0.1: {share:.4f}, theory {want:.4f}")
            check(abs(share - want) < 0.01, f"Rayleigh share {share}")


def test_determinism_order():
    """The same seed gives the same samples; the samples asked in a shuffled
    order are the same, bit for bit; another seed gives others."""
    opts = ["--medium-fading=10,0.4", "--medium-shadowing=4,30", "--medium-burst=20,2,20"]
    a = dump(opts, ["1,2,0,0.37,3000"], "7")["1,2,0,0.37,3000"]
    b = dump(opts, ["1,2,0,0.37,3000"], "7")["1,2,0,0.37,3000"]
    c = dump(opts, ["1,2,0,0.37,3000,shuffle"], "7")["1,2,0,0.37,3000,shuffle"]
    d = dump(opts, ["1,2,0,0.37,3000"], "8")["1,2,0,0.37,3000"]
    check(a == b, "same seed, different samples")
    check(a == c, "shuffled queries, different samples")
    check(a != d, "another seed, same samples")
    # A link's samples do not depend on other links' queries in between
    e = dump(opts, ["5,6,0,0.37,3000", "1,2,0,0.37,3000"], "7")["1,2,0,0.37,3000"]
    check(a == e, "another link's queries changed the samples")


def test_reciprocity():
    """Reciprocal (the default): 1 -> 2 and 2 -> 1 vary the same; with
    --medium-reciprocal=off, independently (correlation near 0)."""
    opts = ["--medium-fading=10,0.4", "--medium-shadowing=4,30", "--medium-burst=20,2,20"]
    r = dump(opts, ["1,2,0,1,2000", "2,1,0,1,2000"])
    check(r["1,2,0,1,2000"] == r["2,1,0,1,2000"], "reciprocal: the two directions differ")
    r = dump(opts + ["--medium-reciprocal=off"], ["1,2,0,1,20000", "2,1,0,1,20000"])
    c = corr([x[1] for x in r["1,2,0,1,20000"]], [x[1] for x in r["2,1,0,1,20000"]])
    print(f"     not reciprocal: shadowing correlation of the two directions {c:.3f}")
    check(abs(c) < 0.1, f"not reciprocal: correlation {c}")


def test_node_share():
    """--medium-node-share=0.6: two links sharing node 1 correlate by 0.3 in
    their shadowing; links without a common node, near 0; sigma unchanged."""
    opts = ["--medium-shadowing=4,30", "--medium-node-share=0.6"]
    specs = ["1,2,0,3,60000", "1,3,0,3,60000", "4,5,0,3,60000"]
    r = dump(opts, specs)
    x, y, z = ([v[1] for v in r[s]] for s in specs)
    c_shared, c_other = corr(x, y), corr(x, z)
    sd = math.sqrt(mean([v * v for v in x]) - mean(x) ** 2)
    print(f"     correlation with a common node {c_shared:.3f} (0.30), without {c_other:.3f}, sigma {sd:.2f} dB")
    check(abs(c_shared - 0.3) < 0.06 and abs(c_other) < 0.06, f"correlations {c_shared} {c_other}")
    check(abs(sd - 4) < 0.25, f"sigma {sd}")


def runs(bad):
    out, cur, n = {0: [], 1: []}, bad[0], 0
    for v in bad:
        if v == cur:
            n += 1
        else:
            out[cur].append(n)
            cur, n = v, 1
    return out


def test_bursts():
    """Good 20 s, bad 2 s, a sample every 0.05 s over 2e4 s: the share of time
    in the bad state 2 / 22 = 0.091 (within 0.02), the mean sojourns 2 s
    (within 15 %) and 20 s (within 20 %), exponential (coefficient of
    variation near 1). Per link, two links of one receiver are independent;
    per receiver (rx), they are in the bad state together; per node, a link
    is bad when either end is (share 1 - (1 - p)^2), and two links sharing
    a node are bad together at least while that node is."""
    specs = ["1,2,0,0.05,400000", "1,3,0,0.05,400000"]
    r = dump(["--medium-burst=20,2,20"], specs)
    bad = [v[3] for v in r[specs[0]]]
    share = mean(bad)
    rr = runs(bad)
    mb, mg = mean(rr[1][1:-1]) * 0.05, mean(rr[0][1:-1]) * 0.05
    cv = math.sqrt(mean([(x * 0.05) ** 2 for x in rr[1]]) - (mean(rr[1]) * 0.05) ** 2) / (mean(rr[1]) * 0.05)
    other = [v[3] for v in r[specs[1]]]
    both = mean([a & b for a, b in zip(bad, other)])
    print(f"     bad share {share:.4f}, mean bad {mb:.2f} s ({len(rr[1])} bursts), good {mg:.1f} s, "
          f"CV of bad sojourns {cv:.2f}; both links bad {both:.4f} (independent: {share * mean(other):.4f})")
    check(abs(share - 2 / 22) < 0.02, f"bad share {share}")
    check(abs(mb - 2) < 0.3 and abs(mg - 20) < 4, f"sojourns {mb} {mg}")
    check(0.8 < cv < 1.2, f"CV {cv}")
    check(both < 0.03, f"per link: both bad {both}")
    check(all(v[2] == 0 and v[1] == 0 for v in r[specs[0]][:100]), "bursts alone: no fading, no shadowing")
    r = dump(["--medium-burst=20,2,20,rx"], specs)
    check([v[3] for v in r[specs[0]]] == [v[3] for v in r[specs[1]]], "per receiver: links of 1 differ")
    # Per node: a link is bad when either end is; 1-2 and 3-1 share node 1
    specs = ["1,2,0,0.05,400000", "3,1,0,0.05,400000", "2,1,0,0.05,400000"]
    r = dump(["--medium-burst=20,2,20,node"], specs)
    x, y = [v[3] for v in r[specs[0]]], [v[3] for v in r[specs[1]]]
    p = 2 / 22
    both = mean([a & b for a, b in zip(x, y)])
    print(f"     per node: bad share {mean(x):.4f} (1 - (1 - p)^2 = {1 - (1 - p) ** 2:.4f}), "
          f"links sharing a node both bad {both:.4f} (at least p = {p:.4f})")
    check(abs(mean(x) - (1 - (1 - p) ** 2)) < 0.03, f"per node: bad share {mean(x)}")
    check(abs(both - (p + p * p * (1 - p))) < 0.03, f"per node: both bad {both}")
    check(r[specs[0]] == r[specs[2]], "per node: the two directions differ")


# ---- the medium

PAY = b"p" * 100
BITS100 = 8 * (2 + 100 + 4)


def test_layered_average():
    """Node 1 sends a 100-byte frame every second for 3000 s to 2, 3 and 4 at
    mean levels -90, -95 and -100 dBm, --medium-rx-model=layered (fading
    K = 10, 0.4 s). The loss rate at each equals the layered curve averaged
    over the Rician distribution (within 4.5 sigma; frames 2.5 coherence
    times apart, so nearly independent), and is within a factor 2.1 of the
    empirical curve's (the fit, §12.4). The medium log has the variation columns: rx_dbm =
    mean + shadowing + fading."""
    levels = {2: -90, 3: -95, 4: -100}
    n = 3000
    r = {1: Radio(1, {1000 + k * 1000000: [("tx", {"payload": PAY})] for k in range(n)})}
    r.update({k: Radio(k) for k in levels})
    d, out, rows = medium_run(r, [(1, k, v) for k, v in levels.items()], opts=["--medium-rx-model=layered"])
    with open(os.path.join(ARGS.logdir, "medium.log")) as f:
        full = [ln.split() for ln in f]
    check(all(len(x) == 11 for x in full), "medium log: 11 columns with variation")
    check(all(abs(float(x[5]) - (float(x[7]) + float(x[8]) + float(x[9]))) < 0.02 for x in full),
          "rx_dbm is not mean + shadowing + fading")
    check("fading Rician K 10, coherence time 0.4 s" in out, "default fading not printed")
    for k, lvl in levels.items():
        xv = [m for t, m in got(r[k], "xv")]
        check(len(xv) == n, f"node {k}: {len(xv)} verdicts")
        lost = sum(1 for m in xv if not m["ok"])
        want = per_layered_avg(lvl, BITS100)
        emp = per_empirical(lvl, BITS100)
        sigma = math.sqrt(n * want * (1 - want))
        print(f"     {lvl} dBm: {lost}/{n} lost, layered average {n * want:.1f} (sigma {sigma:.1f}), "
              f"empirical curve {n * emp:.1f}")
        check(abs(lost - n * want) < 4.5 * sigma + 1, f"{lvl} dBm: {lost} lost, expected {n * want:.1f}")
        check(1 / 2.1 < want / emp < 2.1, f"{lvl} dBm: layered {want} against empirical {emp}")


def test_log_matches_dump():
    """The variation the medium applies to a frame (its log line) is the one
    the dump gives for that link at the frame's start."""
    n = 12
    opts = ["--medium-fading=10,0.4", "--medium-shadowing=4,30", "--medium-burst=5,1,20"]
    r = {1: Radio(1, {1000 + k * 700000: [("tx", {"payload": PAY})] for k in range(n)}), 2: Radio(2)}
    medium_run(r, [(1, 2, -70)], opts=opts, seed="3")
    with open(os.path.join(ARGS.logdir, "medium.log")) as f:
        full = [ln.split() for ln in f]
    air = (10 + 2 + 100 + 4) * 8 * 1000000 // 50000
    specs = [f"2,1,{(int(x[0]) - air) / 1e6:.6f},1,1" for x in full]
    dd = dump(opts, specs, "3")
    for x, s in zip(full, specs):
        t, sh, fa, bad = dd[s][0]
        check(t == int(x[0]) - air, f"start {t}")
        check(abs(float(x[8]) - sh) < 0.006 and abs(float(x[9]) - fa) < 0.006 and int(x[10]) == bad,
              f"log {x[7:]} against dump {dd[s][0]}")
    print(f"     {len(full)} frames: the log's shadowing, fading and burst state equal the dump's")


def test_run_determinism():
    """A run with variation: the same seed gives the same medium log, another
    seed another."""
    def one(seed):
        r = {1: Radio(1, {1000 + k * 50000: [("tx", {})] for k in range(400)}), 2: Radio(2)}
        medium_run(r, [(1, 2, -100)], opts=["--medium-rx-model=layered", "--medium-burst=2,0.5,10"], seed=seed)
        with open(os.path.join(ARGS.logdir, "medium.log")) as f:
            return f.read()
    a, b, c = one("4"), one("4"), one("5")
    check(a == b, "same seed, different medium logs")
    check(a != c, "another seed, same medium log")


def p_combined(p, n, i, x0i, bi, pnorm, x0=9.0, b=0.9):
    """The layered model's bit error rate (§12.5)."""
    pm, nm, im = (10 ** (v / 10) for v in (p, n, i))
    sir = p - i
    ieq = pm / 10 ** ((x0 + (sir - x0i) * b / bi) / 10)
    d = max(nm, ieq) if pnorm == 0 else (nm ** pnorm + ieq ** pnorm) ** (1 / pnorm)
    return min(0.5, 0.5 * math.exp(-(10 * math.log10(pm / d) - x0) / b))


def test_combination():
    """Noise and interference near sensitivity, mode 3, the layered curve
    without fading (--medium-fading=off). The wanted frame 3 dB above the
    curve's 10 % point of its 45-byte PSDU (so noise alone loses about 0.1 %),
    an interferer from just after its SHR to its end at a SIR where
    interference alone loses about 10 %: with the default (sum) the loss rate
    is the formula's, well above max's; with --medium-combine=max, the
    formula of the larger error rate."""
    M3, M3_SHR, M3_PSDU, M3_AIR = tc.M3, tc.M3_SHR, tc.M3_PSDU, tc.M3_AIR
    bits = 8 * (2 + len(M3_PSDU) + 4)
    # 10 % point of the noise curve for these bits, then 3 dB above
    ber10 = -math.expm1(math.log(0.9) / bits)
    snr10 = 9.0 - 0.9 * math.log(ber10 / 0.5)
    p_a = NOISE_DBM + snr10 + 3
    sir = 3.8
    m3_int = dict(M3, payload=b"i" * 120, at=M3_SHR + 153)
    share = (M3_AIR - M3_SHR - 153) / (M3_AIR - M3_SHR)
    n = 600
    for pnorm, opts in ((1, []), (0, ["--medium-combine=max"])):
        rx, xv = tc.received_share(p_a, p_a - sir, n, dict(M3, payload=M3_PSDU), dict(m3_int), 30000,
                                   ["--medium-rx-model=layered", "--medium-fading=off", *opts])
        ber_n = p_combined(p_a, NOISE_DBM, -300, -2.85, 0.9, pnorm)
        ber_i = p_combined(p_a, NOISE_DBM, p_a - sir, -2.85, 0.9, pnorm)
        want = (1 - ber_n) ** (bits * (1 - share)) * (1 - ber_i) ** (bits * share)
        sigma = math.sqrt(n * want * (1 - want))
        name = "sum" if pnorm else "max"
        print(f"     {name}: wanted {p_a:.1f} dBm, SIR {sir} dB: {rx * 100:.1f} % received (formula {want * 100:.1f} %)")
        check(abs(rx * n - want * n) < 4.5 * sigma + 1, f"{name}: {rx}, formula {want}")
        if pnorm:
            sum_rx = rx
        else:
            check(rx > sum_rx + 0.05, f"max {rx} not above sum {sum_rx}")


def test_options():
    """Option errors; refused with the legacy medium; without any option the
    log keeps its 7 columns and the server prints nothing of this module."""
    for args, msg in ((["--vtime=1", "--medium=legacy", "--medium-fading=10,0.4"], "need the central medium"),
                      (["--vtime=1", "--medium-fading=10"], "expected K,TC_S"),
                      (["--vtime=1", "--medium-rx-model=x"], "empirical or layered"),
                      (["--vtime=1", "--medium-burst=1,2,3,all"], "link, rx or node"),
                      (["--vtime=1", "--medium-node-share=2"], "expected a number"),
                      (["--vtime=1", "--medium-combine=0.1"], "expected a number")):
        srv = Server(args)
        check(srv.wait() != 0 and msg in srv.output(), f"{args} accepted, or no \"{msg}\"")
    r = {1: Radio(1, {1000: [("tx", {})]}), 2: Radio(2)}
    d, out, rows = medium_run(r, [(1, 2, -80)])
    with open(os.path.join(ARGS.logdir, "medium.log")) as f:
        check(all(len(ln.split()) == 7 for ln in f), "default: not 7 columns")
    check("link variation" not in out and "receiver curve" not in out, "default: channel lines printed")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("server", help="wssimserver binary (copied before use)")
    ap.add_argument("--sock", default="/tmp/wssim-chan.sock")
    ap.add_argument("--logdir", default=None)
    ap.add_argument("-k", default="", help="run only tests whose name contains this")
    ARGS = ap.parse_args()
    if ARGS.sock == "/tmp/sim_socket":
        sys.exit("refusing the default socket: it may belong to a real run")
    ARGS.logdir = ARGS.logdir or tempfile.mkdtemp(prefix="chan_")
    os.makedirs(ARGS.logdir, exist_ok=True)
    ARGS.copy = os.path.join(ARGS.logdir, "wssimserver-chan")
    shutil.copy2(ARGS.server, ARGS.copy)
    ARGS.bin = ARGS.copy
    tc.ARGS = ARGS
    tests = [
        ("shadowing", test_shadowing),
        ("fading", test_fading),
        ("determinism_order", test_determinism_order),
        ("reciprocity", test_reciprocity),
        ("node_share", test_node_share),
        ("bursts", test_bursts),
        ("layered_average", test_layered_average),
        ("log_matches_dump", test_log_matches_dump),
        ("run_determinism", test_run_determinism),
        ("combination", test_combination),
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
