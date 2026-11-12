#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests of propagation from node positions (docs/MEDIUM.md §11) with fake nodes.

    python3 sim_utils/vtime/test_propagation.py build-vt/wssimserver [--sock /tmp/prop_sock] [--logdir DIR]

The model's values are read from --prop-dump (the server is stopped once its
socket exists: the dump is written before it listens) and from what the
medium delivers to fake radios. Exit status 0 when every test passes.
"""
import argparse
import math
import os
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_conductor as tc  # noqa: E402
from test_conductor import Radio, Server, check, got, medium_run, PAYLOAD  # noqa: E402

ARGS = None
HERE = os.path.dirname(os.path.abspath(__file__))
GEN_BUILDING = os.path.join(HERE, "..", "..", "radio_if", "scale", "gen_building.py")


def write(name, text):
    path = os.path.join(ARGS.logdir, name)
    with open(path, "w") as f:
        f.write(text)
    return path


def build_only(opts, seed="1", nodes=1):
    """Start the server with opts, let it build the model, stop it. Returns
    (output, {(rx, tx): row}) with the --prop-dump rows."""
    dump = os.path.join(ARGS.logdir, "dump.txt")
    if os.path.exists(dump):
        os.unlink(dump)
    srv = Server([f"--vtime={nodes}", f"--prop-dump={dump}", *opts], {"WSSIM_SEED": seed})
    srv.p.terminate()           # our own process: the model is built before the socket exists
    srv.wait()
    rows = {}
    with open(dump) as f:
        for ln in f:
            if ln.startswith("#"):
                continue
            a = ln.split()
            rx, tx = int(a[0]), int(a[1])
            rows[(rx, tx)] = {"d": float(a[2]), "floors": int(a[3]), "walls": int(a[4]), "pl": float(a[5]),
                              "sh": float(a[6]), "gains": float(a[7]), "dbm": float(a[8]), "heard": int(a[9]),
                              "given": int(a[10])}
    return srv.output(), rows


def refused(opts, msg):
    srv = Server(["--vtime=1", *opts])
    st = srv.wait()
    check(st != 0 and msg in srv.output(), f"{opts} accepted, or no \"{msg}\": {srv.output()[-500:]}")


# --- tests -------------------------------------------------------------------

def test_options():
    pos = write("opt.pos", "0 0 0 0\n1 10 0 0\n")
    refused(["--prop-model=indoor"], "need --positions")
    refused(["--prop-dump=/dev/null"], "need --positions")
    refused([f"--positions={pos}", "--prop-model=bogus"], "indoor, logdist or freespace")
    refused([f"--positions={pos}", "--prop-floor-db=10,5"], "must not decrease")
    refused([f"--positions={pos}", "--prop-walls=8"], "SPACING")
    refused(["--medium=legacy", "--rssi-noise=2"], "need the central medium")
    refused([f"--positions={write('bad1.pos', '0 0 0\n')}"], "expected ID X Y Z")
    refused([f"--positions={write('bad2.pos', '0 0 0 0\n0 1 1 1\n')}"], "given twice")
    refused([f"--positions={write('bad3.pos', '5000 0 0 0\n')}"], "bad node ID")
    refused([f"--positions={write('bad4.pos', '0 0 0 0 1.5\n')}"], "FLOOR must be an integer")
    refused([f"--positions={write('bad5.pos', '1.5 0 0 0\n')}"], "bad node ID")
    for i, bad in enumerate(("0 nan 0 0", "0 0 inf 0", "0 0 0 -inf", "0 0 0 0 0 nan", "0 0 0 0 1e12", "0 1e9 0 0",
                             "0 0 0 0 0 1 7", "0 0x 0 0")):
        refused([f"--positions={write(f'bad6_{i}.pos', bad + chr(10))}"], "--positions")
    refused(["--prop-dump-node=3"], "need")
    # Lines of any length: a long comment, and a long comment after a node
    out, r = build_only([f"--positions={write('long.pos', '#' + 'x' * 5000 + chr(10) + '0 0 0 0 # ' + 'y' * 3000 + chr(10) + '1 10 0 0' + chr(10))}"])
    check(set(r) == {(0, 1), (1, 0)}, f"long lines: {sorted(r)}")


def fspl1(f):
    return 20 * math.log10(f) - 27.55


def test_formulas():
    """Each model against hand computation, shadowing off."""
    pos = write("f.pos", "# id x y z floor gain\n"
                         "0 0 0 0 0\n"
                         "1 20 0 0 0\n"
                         "2 3 4 3.5 1\n"          # 1 floor above node 0
                         "3 0 0 17.5 5 2.5\n"     # 5 floors above, antenna gain 2.5 dBi
                         "4 0.3 0 0\n")           # no FLOOR: Z / 3.5 rounded = 0; 0.3 m from node 0
    sh0 = ["--prop-shadow=0", "--prop-cutoff=-250"]
    # indoor (ITU-R P.1238 office, 900 MHz): 20 log f - 28 + 33 log d + Lf(n)
    out, r = build_only([f"--positions={pos}", *sh0])
    pl0 = 20 * math.log10(915) - 28
    lf = {0: 0, 1: 9, 2: 19, 3: 24, 4: 29, 5: 34}       # the last step (5 dB) repeated beyond 3 floors
    for (rx, tx), x in r.items():
        dd = max(x["d"], 1.0)
        want = pl0 + 33 * math.log10(dd) + lf[x["floors"]]
        check(abs(x["pl"] - want) < 0.01, f"indoor {rx}<-{tx}: {x['pl']} != {want}")
        g = 2.5 if 3 in (rx, tx) else 0
        check(abs(x["dbm"] - (20 + g - want)) < 0.011, f"indoor dbm {rx}<-{tx}: {x} (default --prop-tx-dbm 20)")
    check(r[(2, 0)]["floors"] == 1 and abs(r[(2, 0)]["d"] - math.sqrt(9 + 16 + 3.5 ** 2)) < 0.01, f"{r[(2, 0)]}")
    check(r[(3, 0)]["floors"] == 5 and r[(4, 0)]["floors"] == 0, f"floors {r[(3, 0)]} {r[(4, 0)]}")
    check(abs(r[(4, 0)]["pl"] - pl0) < 0.01, "min distance 1 m not applied")
    check(abs(r[(1, 0)]["pl"] - (pl0 + 33 * math.log10(20))) < 0.01, f"{r[(1, 0)]}")
    # logdist with a per-metre wall loss, a wall grid and one floor value (per floor)
    out, r = build_only([f"--positions={pos}", *sh0, "--prop-model=logdist", "--prop-wall-db-per-m=0.5",
                         "--prop-walls=8,5:3", "--prop-floor-db=10", "--prop-freq=868", "--prop-tx-dbm=10"])
    x = r[(1, 0)]       # 20 m along x: walls at 4, 12 m -> rounding 0/8=0, 20/8=2.5 -> 3 (floor(2.5+.5)=3): 3 walls
    want = fspl1(868) + 30 * math.log10(20) + 0.5 * 20 + 3 * 3
    check(x["walls"] == 3 and abs(x["pl"] - want) < 0.01, f"logdist+walls {x} want {want}")
    x = r[(2, 0)]       # dx 3 (0 walls at 8 m), dy 4 (5 m grid: round(0.8)=1 wall), 1 floor
    want = fspl1(868) + 30 * math.log10(math.sqrt(9 + 16 + 12.25)) + 0.5 * 5 + 1 * 3 + 10
    check(x["walls"] == 1 and abs(x["pl"] - want) < 0.01, f"logdist 2<-0 {x} want {want}")
    check(abs(r[(3, 0)]["pl"] - (fspl1(868) + 30 * math.log10(17.5) + 50)) < 0.01, f"5 floors x 10 dB {r[(3, 0)]}")
    check(abs(r[(1, 0)]["dbm"] - (10 - r[(1, 0)]["pl"])) < 0.011, "--prop-tx-dbm")
    # free space; then --prop-pl0 and --prop-exp
    out, r = build_only([f"--positions={pos}", *sh0, "--prop-model=freespace"])
    check(abs(r[(1, 0)]["pl"] - (fspl1(915) + 20 * math.log10(20))) < 0.01, f"freespace {r[(1, 0)]}")
    check(abs(r[(3, 0)]["pl"] - (fspl1(915) + 20 * math.log10(17.5))) < 0.01, "freespace has no floor loss")
    out, r = build_only([f"--positions={pos}", *sh0, "--prop-pl0=40", "--prop-exp=2.5",
                         "--prop-floor-db=12.9,18.7,24.4,27", "--prop-gain=1"])
    check(abs(r[(1, 0)]["pl"] - (40 + 25 * math.log10(20))) < 0.01, f"pl0/exp {r[(1, 0)]}")
    check(abs(r[(3, 0)]["pl"] - (40 + 25 * math.log10(17.5) + 27 + 2.6)) < 0.01, f"floor table {r[(3, 0)]}")
    check(r[(1, 0)]["gains"] == 2 and r[(3, 0)]["gains"] == 3.5, "gains: default per node, column for node 3")


def grid_positions(name, n, spacing=10.0):
    side = int(math.ceil(math.sqrt(n)))
    return write(name, "".join(f"{i} {(i % side) * spacing} {(i // side) * spacing} 0 0\n" for i in range(n)))


def test_shadowing():
    """Reproducible by seed; standard deviations; reciprocal by default;
    the per-direction and per-node parts."""
    pos = grid_positions("sh.pos", 300)
    o = [f"--positions={pos}", "--prop-cutoff=-250"]
    out1, r1 = build_only(o, seed="7")
    out2, r2 = build_only(o, seed="7")
    out3, r3 = build_only(o, seed="8")
    check(r1 == r2, "same seed, different links")
    check(sum(r1[k]["sh"] != r3[k]["sh"] for k in r1) > 0.99 * len(r1), "another seed gives the same shadowing")
    pairs = [(a, b) for (a, b) in r1 if a < b]
    check(len(pairs) == 300 * 299 // 2, f"{len(pairs)} pairs")
    sh = [r1[p]["sh"] for p in pairs]
    sd, mean = statistics.pstdev(sh), statistics.fmean(sh)
    check(abs(sd - 4) < 0.1 and abs(mean) < 0.1, f"shadowing sd {sd:.3f} mean {mean:.3f} (4 dB, 0)")
    check(all(r1[(a, b)]["dbm"] == r1[(b, a)]["dbm"] for a, b in pairs), "default not reciprocal")
    check("0 asymmetric" in out1, "asymmetric count")
    # Per direction: the two directions differ with sd sqrt(2) * 2 dB; total sd sqrt(4^2 + 2^2)
    out, r = build_only(o + ["--prop-shadow-dir=2"], seed="7")
    diff = [r[(a, b)]["sh"] - r[(b, a)]["sh"] for a, b in pairs]
    tot = [r[(a, b)]["sh"] for a, b in pairs]
    check(abs(statistics.pstdev(diff) - 2 * math.sqrt(2)) < 0.1, f"direction sd {statistics.pstdev(diff):.3f}")
    check(abs(statistics.pstdev(tot) - math.sqrt(20)) < 0.12, f"total sd {statistics.pstdev(tot):.3f}")
    check(f"{len(pairs)} asymmetric" in out, "asymmetric count with --prop-shadow-dir")
    # Per node: links sharing a node are correlated (rho = s_n^2 / (s_l^2 + 2 s_n^2) = 9/34 with 3 dB)
    out, r = build_only(o + ["--prop-shadow-node=3"], seed="7")
    tot = [r[(a, b)]["sh"] for a, b in pairs]
    check(abs(statistics.pstdev(tot) - math.sqrt(16 + 18)) < 0.15, f"node sd {statistics.pstdev(tot):.3f}")
    xs = [r[(0, b)]["sh"] for b in range(2, 300)]
    ys = [r[(1, b)]["sh"] for b in range(2, 300)]
    zs = [r[(b, 0)]["sh"] for b in range(2, 300)]
    # node 0's own term: the mean of its links is that term (plus noise of sd ~ sqrt(16+9)/sqrt(298))
    check(abs(statistics.fmean(xs) - statistics.fmean(zs)) < 1e-9, "node term not in both directions")
    corr = statistics.correlation(xs, ys)
    check(abs(corr - 9 / 34) < 0.15, f"links 0-b and 1-b: correlation {corr:.2f}, expected {9 / 34:.2f}")


def test_graph_cutoff():
    """Who hears whom from the mean level: node 3 at 20 km does not hear nodes
    0 and 1 (no lock, no line in the medium log); with a lower cutoff it
    does; -g adds a pair below the cutoff, with the model's value."""
    pos = write("g.pos", "1 0 0 0 0\n2 10 0 0 0\n3 20000 0 0 0\n")
    sh0 = "--prop-shadow=0"
    out, r = build_only([f"--positions={pos}", sh0])
    pl = 20 * math.log10(915) - 28 + 33 * math.log10(20000)
    check(set(r) == {(1, 2), (2, 1)}, f"graph {sorted(r)}: 3 is at {14 - pl:.1f} dBm, below -135")
    out, r = build_only([f"--positions={pos}", sh0, "--prop-cutoff=-160"])
    check(len(r) == 6 and abs(r[(3, 1)]["dbm"] - (20 - pl)) < 0.01, f"cutoff -160: {sorted(r)}")
    out, r = build_only([f"--positions={pos}", sh0, "--prop-margin=0", "--prop-cutoff=-130"])
    check(set(r) == {(1, 2), (2, 1)}, "margin 0")
    # Medium: a frame from 1 reaches 2 at the model's level, not 3; -g 1,3 (no value) adds 3 with the model's value
    p12 = 14 - (20 * math.log10(915) - 28 + 33)
    for extra, want3 in (([], None), ([(1, 3, None)], 14 - pl)):
        rad = {1: Radio(1, {1000: [("tx", {"tx_dbm": 14})]}), 2: Radio(2), 3: Radio(3)}
        d, out, rows = medium_run(rad, extra, opts=[f"--positions={pos}", sh0])
        x2 = got(rad[2], "xx")
        check(len(x2) == 1 and abs(x2[0][1]["rx_dbm"] - p12) < 0.01, f"at 2: {x2}, want {p12:.2f}")
        at3 = [x for x in rows if x[2] == 3]
        if want3 is None:
            check(not at3, f"3 in the medium log: {at3}")
        else:
            check(len(at3) == 1 and at3[0][4] == "below_lock" and abs(at3[0][5] - want3) < 0.01, f"at 3: {at3}")


def test_tx_power_and_override():
    """The frame's TX power shifts the model's level; a -g value overrides the
    model for that pair (both directions), --medium-tx-ref applies to it only;
    --vtime-link replaces the model's value."""
    pos = write("o.pos", "1 0 0 0 0\n2 10 0 0 0\n3 0 10 0 0\n")
    sh0 = "--prop-shadow=0"
    p = 14 - (20 * math.log10(915) - 28 + 33)
    rad = {1: Radio(1, {1000: [("tx", {"tx_dbm": 20})]}), 2: Radio(2), 3: Radio(3)}
    d, out, rows = medium_run(rad, [(1, 3, -50)], opts=[f"--positions={pos}", sh0, "--medium-tx-ref=0"])
    x2, x3 = got(rad[2], "xx"), got(rad[3], "xx")
    check(abs(x2[0][1]["rx_dbm"] - (p + 6)) < 0.01, f"at 2: {x2}, want {p + 6:.2f} (20 dBm, model at 14)")
    check(abs(x3[0][1]["rx_dbm"] - (-50 + 20)) < 0.01, f"at 3: {x3}: -g -50 at --medium-tx-ref 0, sent at 20")
    check("2 directions set by -g" in out, "override count")
    # Reverse direction of the -g link, and the dump
    out, r = build_only([f"--positions={pos}", sh0, "-g", "1,3:-50"])
    check(r[(3, 1)]["given"] == 1 and r[(1, 3)]["given"] == 1 and r[(2, 1)]["given"] == 0, "given flags")
    # --vtime-link: from 3 ms, the 1-2 link is -90 dBm, the value as given whatever
    # the TX power (sent at 14 dBm, nominal 20: the model's value would give -96)
    rad = {1: Radio(1, {1000: [("tx", {"tx_dbm": 14})], 10000: [("tx", {"tx_dbm": 14})]}), 2: Radio(2)}
    d, out, rows = medium_run(rad, [], opts=[f"--positions={pos}", sh0, "--vtime-link=1:2:0.003:-90"])
    lv = [m["rx_dbm"] for t, m in got(rad[2], "xx")]
    check(len(lv) == 2 and abs(lv[0] - p) < 0.01 and lv[1] == -90, f"--vtime-link: {lv}")
    # --vtime-link on a positioned pair below the cutoff: the pair joins the graph, both ways
    far = write("o2.pos", "1 0 0 0 0\n2 20000 0 0 0\n")
    rad = {1: Radio(1, {1000: [("tx", {})], 10000: [("tx", {})]}),
           2: Radio(2, {20000: [("tx", {})]})}
    d, out, rows = medium_run(rad, [], opts=[f"--positions={far}", sh0, "--vtime-link=1:2:0.003:-70"])
    check([(t, m["rx_dbm"]) for t, m in got(rad[2], "xx")] == [(10000, -70.0)], f"at 2: {rad[2].rx}")
    check([(t, m["rx_dbm"]) for t, m in got(rad[1], "xx")] == [(20000, -70.0)], f"at 1: {rad[1].rx}")


def test_gen_building():
    """gen_building.py --positions with its own model's settings gives the
    same links as its topology file (rounded to 0.1 dB)."""
    topo, posf = os.path.join(ARGS.logdir, "b.topo"), os.path.join(ARGS.logdir, "b.pos")
    subprocess.run([sys.executable, GEN_BUILDING, "--nodes", "300", "--floors", "2", "-o", topo, "--positions", posf],
                   check=True, stderr=subprocess.DEVNULL)
    want = {}
    with open(topo) as f:
        for ln in f:
            ab, v = ln.strip().split(":")
            a, b = map(int, ab.split(","))
            want[(a, b)] = float(v)
    out, r = build_only([f"--positions={posf}", "--prop-tx-dbm=14", "--prop-model=logdist", "--prop-pl0=31.7", "--prop-exp=3",
                         "--prop-wall-db-per-m=1.8", "--prop-floor-db=18", "--prop-shadow=0",
                         "--prop-margin=0", "--prop-cutoff=-100"])
    got_ = {(b, a): x["dbm"] for (a, b), x in r.items() if b < a}
    bad = [(k, want.get(k), got_.get(k)) for k in set(want) | set(got_)
           if want.get(k) is None or got_.get(k) is None or abs(want[k] - got_[k]) > 0.056]
    check(not bad, f"{len(bad)} links differ, e.g. {bad[:5]}")
    check(len(want) > 1000, f"only {len(want)} links")


def test_dump_node():
    """--prop-dump-node: only the links of one node."""
    pos = grid_positions("dn.pos", 20)
    out, r = build_only([f"--positions={pos}", "--prop-dump-node=3"])
    check(r and all(3 in k for k in r) and len(r) == 2 * 19, f"{len(r)} rows: {sorted(r)[:5]}")


def test_legacy_map():
    """--positions with --medium=legacy: the RSSI map in shared memory (what
    the legacy radios read) holds the model's level at --prop-tx-dbm."""
    pos = write("lg.pos", "1 0 0 0 0\n2 10 0 0 0\n")
    shm = "/dev/shm/wssimnodesinfos." + ARGS.sock.replace("/", "_")
    srv = Server(["--vtime=2", "--medium=legacy", f"--positions={pos}", "--prop-shadow=0", "--prop-tx-dbm=10"])
    try:
        with open(shm, "rb") as f:
            def link(rx, tx):
                f.seek((rx * 4096 + tx) * 4)
                return struct.unpack("=f", f.read(4))[0]
            v12, v21, v13 = link(2, 1), link(1, 2), link(3, 1)
    finally:
        srv.p.terminate()
        srv.wait()
    want = 10 - (20 * math.log10(915) - 28 + 33)
    check(abs(v12 - want) < 0.01 and v12 == v21 and v13 == 0, f"map {v12} {v21} {v13}, want {want:.2f}")


def test_reported_rssi_positions():
    """--rssi-noise with --positions: the delivered RSSI is the model's level
    plus the error; the medium log keeps the model's level. And the "xd" of
    a detection reports the same value as that frame's "xx"."""
    pos = write("rp.pos", "1 0 0 0 0\n2 10 0 0 0\n3 0 10 0 0\n")
    p = 20 - (20 * math.log10(915) - 28 + 33)
    # These radios send at the frame header's default of 0 dBm: 20 dB below the nominal
    rad = {1: Radio(1, period=(1000, 20000, 50)), 2: Radio(2), 3: Radio(3)}
    d, out, rows = medium_run(rad, [], opts=[f"--positions={pos}", "--prop-shadow=0", "--rssi-noise=3"])
    check(all(abs(x[5] - (p - 20)) < 0.01 for x in rows), "medium log not at the model's level")
    e = [m["rx_dbm"] - (p - 20) for t, m in got(rad[2], "xx")]
    check(len(e) == 50 and statistics.pstdev(e) > 1.5 and max(map(abs, e)) < 15, f"errors {e[:5]}")
    # Sniff detection with a lock: "xd" and "xx" of the frame carry the same reported value
    from test_sniff import Sniffer, SNIFF_LOCK
    r = {1: Sniffer(1, {1000: [("lp", 100000, {"tx_dbm": 20})]}),
         2: Sniffer(2, {50000: [("sniff", 0, 2000, SNIFF_LOCK)]})}
    d, out, rows = medium_run(r, [], opts=[f"--positions={pos}", "--prop-shadow=0", "--rssi-noise=3"])
    xd = [x for t, x in r[2].results if x["detected"]]
    xx = got(r[2], "xx")
    check(len(xd) == 1 and len(xx) == 1 and xd[0]["rssi_dbm"] == xx[0][1]["rx_dbm"] != p and
          abs(xd[0]["rssi_dbm"] - p) < 15, f"xd {xd}, xx {xx}, true {p:.2f}")
    check(any(abs(x[5] - p) < 0.01 and x[2] == 2 and x[4] == "ok" for x in rows), f"medium log {rows}")


def test_start_time():
    """4000 nodes (20 floors of 200): the model is built in well under a
    few seconds, O(N^2) once."""
    posf = os.path.join(ARGS.logdir, "b4000.pos")
    subprocess.run([sys.executable, GEN_BUILDING, "--nodes", "4000", "-o", os.devnull, "--positions", posf],
                   check=True, stderr=subprocess.DEVNULL)
    # Our own start: Server() waits only about 2 s for the socket
    if os.path.exists(ARGS.sock):
        os.unlink(ARGS.sock)
    logp = os.path.join(ARGS.logdir, "start4000.log")
    t0 = time.monotonic()
    with open(logp, "w") as logf:
        p = subprocess.Popen([ARGS.bin, "--medium-preset=basic", "--vtime=4000", f"--positions={posf}", ARGS.sock], stdout=logf,
                             stderr=subprocess.STDOUT, env={k: v for k, v in os.environ.items() if k != "WSSIM_SEED"})
        while not os.path.exists(ARGS.sock) and p.poll() is None and time.monotonic() - t0 < 60:
            time.sleep(0.05)
        t = time.monotonic() - t0
        p.terminate()
        p.wait(30)
    out = open(logp).read()
    line = [ln for ln in out.splitlines() if "pairs in the graph" in ln]
    check(line, f"no summary: {out[-500:]}")
    build_s = float(line[0].rsplit(";", 1)[1].split()[-2])
    print(f"    4000 nodes: model {build_s:.2f} s, server ready after {t:.2f} s")
    check(build_s < 5, f"model too slow: {build_s} s")
    check(t < 60, f"server not ready after {t:.1f} s")


def test_reported_rssi():
    """--rssi-offset / --rssi-noise change the RSSI delivered ("xx", "xv"),
    not the power the medium uses (its log); a fixed offset per receiver;
    noise per frame; reproducible by seed."""
    links = [(1, 2, -60), (1, 3, -60)]

    def run(opts, seed="1", n=200):
        rad = {1: Radio(1, period=(1000, 20000, n)), 2: Radio(2), 3: Radio(3)}
        d, out, rows = medium_run(rad, links, opts=opts, seed=seed)
        res = {}
        for nid in (2, 3):
            xx = {m["frame_id"]: m["rx_dbm"] for t, m in got(rad[nid], "xx")}
            xv = {m["frame_id"]: m["rx_dbm"] for t, m in got(rad[nid], "xv")}
            check(xx and xx == xv, f"node {nid}: xx and xv report different values")
            res[nid] = xx
        check(all(x[5] == -60 for x in rows), "the medium log has the true power")
        return res

    base = run([], n=5)
    check(all(v == -60 for r in base.values() for v in r.values()), f"no option: {base}")
    a = run(["--rssi-offset=3"], n=5)
    off2, off3 = set(a[2].values()), set(a[3].values())
    check(len(off2) == 1 and len(off3) == 1 and off2 != off3 and -60 not in off2,
          f"offset per receiver: {off2} {off3}")
    b = run(["--rssi-noise=2"])
    e = [v + 60 for v in b[2].values()]
    check(abs(statistics.pstdev(e) - 2) < 0.3 and abs(statistics.fmean(e)) < 0.4,
          f"noise sd {statistics.pstdev(e):.2f} mean {statistics.fmean(e):.2f} over {len(e)} frames")
    check(run(["--rssi-noise=2"]) == b, "not reproducible")
    check(run(["--rssi-noise=2"], seed="2") != b, "seed has no effect")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("server", help="wssimserver binary (copied before use)")
    ap.add_argument("--sock", default="/tmp/prop_sock")
    ap.add_argument("--logdir", default=None)
    ap.add_argument("-k", default="", help="run only tests whose name contains this")
    ARGS = ap.parse_args()
    if ARGS.sock == "/tmp/sim_socket":
        sys.exit("refusing the default socket: it may belong to a real run")
    ARGS.logdir = ARGS.logdir or tempfile.mkdtemp(prefix="prop_")
    os.makedirs(ARGS.logdir, exist_ok=True)
    ARGS.copy = os.path.join(ARGS.logdir, "wssimserver-prop")
    shutil.copy2(ARGS.server, ARGS.copy)
    ARGS.bin = ARGS.copy
    tc.ARGS = ARGS
    tests = [
        ("options", test_options),
        ("formulas", test_formulas),
        ("shadowing", test_shadowing),
        ("graph_cutoff", test_graph_cutoff),
        ("tx_power_and_override", test_tx_power_and_override),
        ("gen_building", test_gen_building),
        ("dump_node", test_dump_node),
        ("legacy_map", test_legacy_map),
        ("reported_rssi_positions", test_reported_rssi_positions),
        ("start_time", test_start_time),
        ("reported_rssi", test_reported_rssi),
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
