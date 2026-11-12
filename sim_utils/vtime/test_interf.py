#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests of the central medium's interference and spectrum features
(docs/MEDIUM.md §13) with fake nodes: external interferers, the
adjacent-channel table, the smooth capture model and the presync
interference curve, and the sniffing extras (adjacent channels, summed
energy, false detections).

    python3 sim_utils/vtime/test_interf.py build-vt/wssimserver [--sock /tmp/wssim-intf.sock] [--logdir DIR] [-k NAME]

Same harness as test_conductor.py and test_sniff.py (a copy of the server on
its own socket). Exit status 0 when every test passes.

Frames of the fake nodes, with the defaults: 50 kbit/s (mode-1a profile),
SHR 10 bytes (1600 us), 36 bytes on air (5760 us), 208 counted bits.
"""
import argparse
import math
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_conductor as tc  # noqa: E402
from test_conductor import (Server, Radio, check, got, NOISE_DBM, PAYLOAD, AIRTIME, SHR_US, BITS,  # noqa: E402
                            M3, M3_PSDU, M3_AIR, medium_run, sinr_db)
from test_sniff import Sniffer, sniff_run, results, DWELL  # noqa: E402
from fake_node import Driver, TestError, RADIO_IDLE, SNIFF_LOCK  # noqa: E402

SRC_NONE = 0xFFFF


def mw(dbm):
    return 10 ** (dbm / 10)


def dbm(x):
    return 10 * math.log10(x)


def energy(*levels):
    """Noise plus these powers, dBm."""
    return dbm(mw(NOISE_DBM) + sum(mw(x) for x in levels))


def mlog_lines():
    with open(os.path.join(tc.ARGS.logdir, "medium.log")) as f:
        return [ln.split() for ln in f]


def ilog_path():
    return os.path.join(tc.ARGS.logdir, "interferer.log")


def ilog():
    with open(ilog_path()) as f:
        return [(int(a), int(b), n, c) for a, b, n, c in (ln.split() for ln in f)]


def binom_ok(k, n, p, z=4.0):
    """k successes of n are consistent with p (z standard deviations)."""
    sigma = math.sqrt(n * p * (1 - p))
    return abs(k - n * p) <= max(z * sigma, 1.0)


def start_fails(args, msg):
    """The server refuses args (at parsing, or at its start with one node)."""
    srv = Server(args)
    if srv.p.poll() is None:
        d = Driver(tc.ARGS.sock, {1: tc.Script(1)})
        tc.Script.TABLE = {1: {0: ([], tc.NEVER)}}
        try:
            d.connect()
            d.run(timeout=5)
        except (TestError, OSError):
            pass
    status = srv.wait()
    check(status != 0 and msg in srv.output(), f"{args} accepted, or no \"{msg}\": {srv.output()[-400:]}")


def test_intf_options():
    """Errors of the options, and what the server prints at the start."""
    for args, msg in (
            (["--vtime=1", "--medium=legacy", "--interferer=chan=0,level=-80"], "need the central"),
            (["--vtime=1", "--interferer=level=-80"], "chan= is required"),
            (["--vtime=1", "--interferer=chan=0"], "needs level=, at= or pos="),
            (["--vtime=1", "--interferer=chan=0,level=-80,pattern=periodic:10:20"], "0 < ON < PERIOD"),
            (["--vtime=1", "--interferer=chan=0,level=-80,pattern=burst:1ms"], "pattern: expected"),
            (["--vtime=1", "--interferer=chan=0,level=-80,foo=1"], "unknown key"),
            (["--vtime=1", "--interferer=chan=0,level=-80,hop=on"], "hop=on needs"),
            (["--vtime=1", "--interferer=chan=0,level=-80,pre=100"], "pre= needs kind=fsk"),
            (["--vtime=1", "--interferer=chan=0,level=-80,pattern=ge:1x:2"], "bad time"),
            (["--vtime=1", "--interferer=chan=0,level=-80,pos=1/2"], "needs node positions"),
            (["--vtime=1", "--interferer-file=/nonexistent/x"], "--interferer-file"),
            (["--vtime=1", "--medium-adjacent-table=3:10/x"], "expected a number"),
            (["--vtime=1", "--medium-capture-model=foo"], "step|smooth"),
            (["--vtime=1", "--medium-capture-smooth=zz=1"], "unknown key"),
            (["--vtime=1", "--medium-capture-smooth=s=0"], "above 0"),
            (["--vtime=1", "--medium-int-curve-presync=1"], "X0,B"),
            (["--vtime=1", "--sniff-false=2"], "probability per window"),
            (["--vtime=1", "--sniff-false=-1/s"], "RATE/s"),
            (["--vtime=1", "--sniff-adjacent=x"], "off, on or detect"),
            (["--vtime=1", "--sniff-energy-us=0"], "--sniff-energy-us"),
            # options without effect (also with the calibrated preset, which sets none of them)
            (["--vtime=1", "--medium-capture-smooth=m50=1"], "needs --medium-capture-model=smooth"),
            (["--vtime=1", "--medium-capture-model=1a:smooth", "--medium-capture-smooth=3:m50=1"],
             "needs --medium-capture-model=smooth"),
            (["--vtime=1", "--sniff-energy-us=50"], "--sniff-energy-us needs --sniff-energy"),
            (["--vtime=1", "--interferer-log=/dev/null"], "--interferer-log needs"),
            (["--vtime=1", "--sniff-adjacent=on", "--medium-adjacent-db=off"], "--sniff-adjacent needs"),
            (["--vtime=1", "--medium-preset=calibrated", "--medium-capture-model=step", "--medium-capture-smooth=m50=1"],
             "needs --medium-capture-model=smooth")):
        start_fails(args, msg)
    # A file of specs, comments and blank lines
    spec = os.path.join(tc.ARGS.logdir, "intf.txt")
    with open(spec, "w") as f:
        f.write("# two interferers\n\nname=lora,chan=3,level=-90,pattern=burst:50ms:5s  # LoRa-like\n"
                "  name=wide,chan=0-4,level=-100\n")
    for opts, lines in (
            ([f"--interferer-file={spec}"],
             ["interferer lora: noise, 1 channel, burst 50000/5000000 us", "interferer wide: noise, 5 channels, constant"]),
            (["--medium-adjacent-table=10/45", "--medium-capture-model=3:smooth"],
             ["PHY 1a: adjacent-channel table (dB, 1 to 2 channels away): 10.0/45.0",
              "PHY 3: smooth capture: margin 50 % at 1.0 dB, scale 0.49 dB",
              "PHY 3: interference already on air at the SHR's end: x0 -2.72 dB, b 0.54 dB"]),
            (["--medium-capture-model=smooth", "--medium-int-curve-presync=same"], ["PHY 1a: smooth capture"]),
            (["--medium-int-curve-presync=1a:-5,1"],
             ["PHY 1a: interference already on air at the SHR's end: x0 -5.00 dB, b 1.00 dB"])):
        srv = Server(["--vtime=1", "--vtime-end=0.001"] + opts)
        d = Driver(tc.ARGS.sock, {1: tc.Script(1)})
        tc.Script.TABLE = {1: {0: ([], tc.NEVER)}}
        d.connect()
        d.run()
        out = srv.output()
        check(srv.wait() == 0, f"{opts}: server exit: {out[-500:]}")
        for ln in lines:
            check(ln in out, f"{opts}: no \"{ln}\" in {out[:2500]}")
        if "--medium-int-curve-presync=same" in opts:
            check("already on air" not in out, f"{opts}: presync curve printed")
        if "--medium-int-curve-presync=1a:-5,1" in opts:
            check("PHY 3: interference already" not in out and "smooth" not in out, f"{opts}: {out[:2000]}")


def test_intf_cca():
    """CCA energy (§13.1): a constant noise-like interferer on its own
    channel(s) only, an FSK one also on the next channel (10 dB less, the
    medium's table of its rate) and not two away, adj= for its own table,
    per-node levels (at=, level=off), a wideband interferer, and the
    summary line."""
    cases = [   # (spec, {(node, chan): expected interferer power or None})
        ("chan=3,level=-90", {(1, 3): -90, (1, 2): None, (1, 4): None, (2, 3): -90}),
        ("chan=3,level=-90,kind=fsk,rate=50000", {(1, 3): -90, (1, 2): -100, (1, 4): -100, (1, 5): None}),
        ("chan=3,level=-90,kind=fsk,adj=20/30", {(1, 3): -90, (1, 2): -110, (1, 5): -120, (1, 6): None}),
        ("chan=3,level=-90,adj=off/30", {(1, 4): None, (1, 5): -120}),
        ("chan=3,level=-90,at=2:-70", {(1, 3): -90, (2, 3): -70}),
        ("chan=3,level=off,at=2-3:-80", {(1, 3): None, (2, 3): -80, (3, 3): -80}),
        ("chan=1-3,level=-95", {(1, 1): -95, (1, 2): -95, (1, 3): -95, (1, 4): None, (1, 0): None}),
        ("chan=1/3,level=-95,kind=fsk,rate=50000", {(1, 2): dbm(2 * mw(-105)), (1, 3): -95, (1, 4): -105}),
    ]
    for spec, want in cases:
        acts = {}
        for (n, c) in want:
            acts.setdefault(n, []).append(("sense", c))
        r = {n: Radio(n, {1000: acts.get(n, [])}) for n in (1, 2, 3)}
        d, out, rows = medium_run(r, [], opts=[f"--interferer={spec}"])
        for (n, c), lvl in want.items():
            got_db = [x for t, ch, x in r[n].sensed if ch == c][0]
            exp = NOISE_DBM if lvl is None else energy(lvl)
            check(abs(got_db - exp) < 0.01, f"{spec}: CCA at node {n} chan {c}: {got_db:.2f}, expected {exp:.2f}")
        n_on = sum(1 for v in want.values() if v is not None)
        check(f"medium: an external interferer on at {n_on} of the channel-energy queries" in out,
              f"{spec}: summary {out[-800:]}")


def test_intf_periodic():
    """A periodic interferer (on 2000 us every 10000 us from 1000 us): CCA
    at the edges (end exclusive), the interferer log; a phase whose on-time
    wraps (9000 + 2000 > 10000) starts with the interval [0, 1000)."""
    times = [999, 1000, 2999, 3000, 11000, 12999, 13000]
    r = {1: Radio(1, {t: [("sense", 0)] for t in times})}
    d, out, rows = medium_run(r, [], opts=["--interferer=name=p,chan=0,level=-80,pattern=periodic:10ms:2ms:1ms",
                                           f"--interferer-log={ilog_path()}"])
    on = [abs(x - energy(-80)) < 0.01 for t, c, x in r[1].sensed]
    check(on == [False, True, True, False, True, True, False], f"periodic CCA {r[1].sensed}")
    check(ilog()[:2] == [(1000, 3000, "p", "0"), (11000, 13000, "p", "0")], f"log {ilog()[:3]}")
    check("interferer p: 2 bursts" in out, f"summary {out[-600:]}")
    r = {1: Radio(1, {t: [("sense", 0)] for t in (0, 999, 1000, 9000)})}
    d, out, rows = medium_run(r, [], opts=["--interferer=name=p,chan=0,level=-80,pattern=periodic:10000:2000:9000",
                                           f"--interferer-log={ilog_path()}"])
    on = [abs(x - energy(-80)) < 0.01 for t, c, x in r[1].sensed]
    check(on == [True, True, False, True] and ilog()[:2] == [(0, 1000, "p", "0"), (9000, 11000, "p", "0")],
          f"wrapped phase: {r[1].sensed} {ilog()[:3]}")


def pattern_run(spec, seed="1", horizon=200000000):
    """Runs a node that senses once at horizon, so that the interferer's
    schedule is generated up to it; returns the interferer log and output."""
    r = {1: Radio(1, {horizon: [("sense", 0)]})}
    d, out, rows = medium_run(r, [], opts=[f"--interferer={spec}", f"--interferer-log={ilog_path()}"], seed=seed)
    return [x for x in ilog() if x[0] < horizon], out


def duty(log, horizon):
    return sum(min(b, horizon) - a for a, b, n, c in log) / horizon


def test_intf_patterns():
    """Random patterns over 200 s: bursts of 5 ms with gaps of 45 ms on
    average (duty 10 %), Gilbert-Elliott with mean on 20 ms and off 80 ms
    (20 %); the same seed gives the same schedule, another seed another one;
    a hopping interferer uses its channels evenly; the summary line gives
    the measured duty."""
    h = 200000000
    log, out = pattern_run("name=b,chan=0,level=-80,pattern=burst:5ms:45ms", horizon=h)
    check(all(b - a == 5000 for a, b, n, c in log) and abs(duty(log, h) - 0.10) < 0.01 and
          abs(len(log) - 4000) < 4 * math.sqrt(4000), f"burst: {len(log)} bursts, duty {duty(log, h):.4f}")
    on = sum(min(b, h) - a for a, b, n, c in log)
    check(f"interferer b: {len(log)} bursts, on {100.0 * on / h:.3f} % of 200.000 s" in out,
          f"burst summary {out[-500:]}")
    log2, _ = pattern_run("name=b,chan=0,level=-80,pattern=burst:5ms:45ms", horizon=h)
    log3, _ = pattern_run("name=b,chan=0,level=-80,pattern=burst:5ms:45ms", seed="2", horizon=h)
    check(log2 == log and log3 != log, "burst: determinism by seed")
    log, out = pattern_run("name=g,chan=0,level=-80,pattern=ge:20ms:80ms", horizon=h)
    check(abs(duty(log, h) - 0.20) < 0.025 and abs(len(log) - 2000) < 5 * math.sqrt(2000),
          f"ge: {len(log)} bursts, duty {duty(log, h):.4f}")
    log, out = pattern_run("name=hop,chan=0-4,level=-80,hop=on,pattern=burst:1ms:9ms", horizon=h // 4)
    counts = [sum(1 for x in log if x[3] == str(c)) for c in range(5)]
    check(all(binom_ok(k, len(log), 0.2) for k in counts), f"hop: channels {counts}")


def frames_run(spec, p, n=200, period=20000, wanted=None, opts=(), seed="1"):
    """Node 1 sends n frames to 2 at p dBm with an interferer; returns the
    verdicts and the medium log lines of 1's frames."""
    w = dict(wanted or {})
    r = {1: Radio(1, {10000 + k * period: [("tx", dict(w))] for k in range(n)}), 2: Radio(2)}
    d, out, rows = medium_run(r, [(1, 2, p)], opts=[f"--interferer={spec}", *opts], seed=seed)
    xv = [m for t, m in got(r[2], "xv")]
    check(len(xv) == n, f"{spec}: {len(xv)} verdicts, not {n}")
    return xv, [ln for ln in mlog_lines() if ln[1] == "1"], out


def test_intf_reception():
    """A constant interferer over frames (§13.1): noise-like at E adds to
    the noise (the noise curve of P / (N + E)); FSK at E is interference (the
    measured curve of the SIR, mode 1a x0 -2.40 dB, b 0.90 dB). Each rate
    is checked against the formula (200 frames, 4 standard deviations); the
    verdict's SINR, the outcome (interference) and the log's eighth column
    (the interferer's power). A noise-like interferer above the frame
    refuses the lock (below_lock)."""
    p = -60
    # Noise-like: SINR about 10 dB gives about 50 % for 208 bits
    for e in (-70.0, -72.0):
        xv, lines, out = frames_run(f"chan=0,level={e}", p)
        sinr = sinr_db(p, e)
        want = (1 - min(0.5, 0.5 * math.exp(-0.5 * sinr))) ** BITS
        k = sum(m["ok"] for m in xv)
        check(binom_ok(k, len(xv), want), f"noise at {e}: {k}/{len(xv)}, formula {want:.3f}")
        check(all(abs(m["sinr_db"] - sinr) < 0.01 for m in xv), f"noise at {e}: SINR {xv[0]}")
        check(all(ln[4] in ("ok", "interference") and ln[7] == f"{e:.2f}" for ln in lines), f"log {lines[:2]}")
        check(all(m["cause"] == 2 for m in xv if not m["ok"]), "cause interference")
        print(f"     noise-like at {e} dBm: {k}/{len(xv)} received (formula {want * 100:.0f} %)")
    # FSK-like: SIR about 2.1 dB gives about 50 %
    for e in (-62.1, -63.5):
        xv, lines, out = frames_run(f"chan=0,level={e},kind=fsk,rate=50000", p)
        ber = max(0.5 * math.exp(-0.5 * (p - NOISE_DBM)), 0.5 * math.exp(-((p - e) + 2.40) / 0.90))
        want = (1 - min(0.5, ber)) ** BITS
        k = sum(m["ok"] for m in xv)
        check(binom_ok(k, len(xv), want), f"fsk at {e}: {k}/{len(xv)}, formula {want:.3f}")
        print(f"     FSK at {e} dBm: {k}/{len(xv)} received (formula {want * 100:.0f} %)")
    # Above the frame: no lock
    xv_r = {1: Radio(1, {10000: [("tx", {})]}), 2: Radio(2)}
    d, out, rows = medium_run(xv_r, [(1, 2, p)], opts=["--interferer=chan=0,level=-55"])
    check(not got(xv_r[2], "xx") and [x[4] for x in rows] == ["below_lock"] and mlog_lines()[0][7] == "-55.00",
          f"lock refused: {rows}")
    # An interferer at another node or on another channel: the log says "-"
    xv, lines, out = frames_run("chan=0,level=off,at=1:-50", p, n=5)
    check(all(m["ok"] for m in xv) and all(ln[7] == "-" for ln in lines), f"other node: {xv} {lines}")


def test_intf_partial():
    """A periodic interferer over part of a frame: on 3000 us every
    20000 us from 3000 us, frames from 1000 us every 20000 us (1000-6760 us;
    counted bits from 2600 us): the segments with and without the
    interferer, the verdict's lowest SINR and the formula; a strong one
    (-40 dBm) loses every frame; frames in the off time are received with
    the log's "-"."""
    p = -60
    e = -68.0
    r = {1: Radio(1, {1000 + k * 20000: [("tx", {})] for k in range(200)}), 2: Radio(2)}
    d, out, rows = medium_run(r, [(1, 2, p)], opts=[f"--interferer=chan=0,level={e},pattern=periodic:20ms:3ms:3ms"])
    xv = [m for t, m in got(r[2], "xv")]
    share = 3000 / (AIRTIME - SHR_US)
    ber = 0.5 * math.exp(-0.5 * sinr_db(p, e))
    want = (1 - min(0.5, ber)) ** (BITS * share) * (1 - 0.5 * math.exp(-0.5 * sinr_db(p))) ** (BITS * (1 - share))
    k = sum(m["ok"] for m in xv)
    check(binom_ok(k, len(xv), want) and all(abs(m["sinr_db"] - sinr_db(p, e)) < 0.01 for m in xv),
          f"partial: {k}/{len(xv)}, formula {want:.3f}; {xv[0]}")
    print(f"     partial overlap: {k}/{len(xv)} received (formula {want * 100:.0f} %)")
    r = {1: Radio(1, {1000 + k * 20000: [("tx", {})] for k in range(5)}), 2: Radio(2)}
    d, out, rows = medium_run(r, [(1, 2, p)], opts=["--interferer=chan=0,level=-40,pattern=periodic:20ms:3ms:3ms"])
    check(not any(m["ok"] for t, m in got(r[2], "xv")), "strong partial interferer")
    r = {1: Radio(1, {10000 + k * 20000: [("tx", {})] for k in range(5)}), 2: Radio(2)}
    d, out, rows = medium_run(r, [(1, 2, p)], opts=["--interferer=chan=0,level=-40,pattern=periodic:20ms:3ms:3ms"])
    check(all(m["ok"] for t, m in got(r[2], "xv")) and all(ln[7] == "-" for ln in mlog_lines()),
          f"off time: {got(r[2], 'xv')} {mlog_lines()[:2]}")


def test_intf_adjacent_table():
    """--medium-adjacent-table (§13.2): node 1 sends on channel 0 at
    -50 dBm; CCA at node 2 on channels 1-3. Default: 10 dB on the next
    channel, nothing further. 10/40: -90 dBm two channels away.
    --medium-adjacent-db after the table sets its first entry; the table
    after --medium-adjacent-db replaces it. A per-PHY prefix for another
    profile changes nothing. In reception: a frame two channels away at
    -30 dBm with 10/40 interferes at -70 dBm."""
    cases = [
        ([], {1: -60, 2: None, 3: None}),
        (["--medium-adjacent-table=10/40"], {1: -60, 2: -90, 3: None}),
        (["--medium-adjacent-table=10/40", "--medium-adjacent-db=20"], {1: -70, 2: -90, 3: None}),
        (["--medium-adjacent-db=20", "--medium-adjacent-table=10/40/off/50"], {1: -60, 2: -90, 3: None}),
        (["--medium-adjacent-table=off/40"], {1: None, 2: -90}),
        (["--medium-adjacent-table=3:10/40"], {1: -60, 2: None}),
        (["--medium-model=em4", "--medium-adjacent-table=1a:10/40"], {1: -60, 2: -90}),
    ]
    for opts, want in cases:
        r = {1: Radio(1, {1000: [("tx", {})]}), 2: Radio(2, {0: [("radio", RADIO_IDLE, 0)],
                                                             2000: [("sense", c) for c in want]})}
        d, out, rows = medium_run(r, [(1, 2, -50)], opts=opts)
        for t, c, x in r[2].sensed:
            exp = NOISE_DBM if want[c] is None else energy(want[c])
            check(abs(x - exp) < 0.01, f"{opts}: CCA on chan {c}: {x:.2f}, expected {exp:.2f}")
    r = {1: Radio(1, {1000: [("tx", {})]}), 2: Radio(2), 3: Radio(3, {2000: [("tx", {"chan": 2})]})}
    d, out, rows = medium_run(r, [(1, 2, -60), (3, 2, -30)], opts=["--medium-adjacent-table=10/40"])
    xv = [m for t, m in got(r[2], "xv")]
    check(len(xv) == 1 and abs(xv[0]["sinr_db"] - sinr_db(-60, -70)) < 0.01, f"reception 2 channels away: {xv}")


# --- smooth capture (§13.3), the model in Python --------------------------

SMOOTH = {50000: dict(m50=0, s=0.85, brk=2.5, brk_s=0.5, early=12.55, slope=3.94, w=4.64, rise=-4, fall=1, sfd=False),
          150000: dict(m50=0.99, s=0.49, brk=2.5, brk_s=0.5, early=38.0, slope=0, w=1.59, rise=-4.53, fall=0.75,
                       sfd=True)}


def logistic(x):
    return 1 / (1 + math.exp(-x))


def smooth_p(rate, shr_bytes, offset_us, margin):
    """P(capture), P(break) of a frame margin dB stronger starting
    offset_us after the received frame's start (its own SHR)."""
    x = SMOOTH[rate]
    d = offset_us * rate / 1e6
    shr_us = shr_bytes * 8 * 1000000 // rate
    pre_us = shr_us - 16 * 1000000 // rate
    tp, tshr = pre_us * rate / 1e6, shr_us * rate / 1e6
    if d >= tshr + (x["fall"] if x["sfd"] else 0) + 8 * x["w"]:
        return 0, 0
    e = min(max(x["early"] + x["slope"] * max(margin - 5, 0), 0), tp)
    early = 1 - logistic((d - e) / x["w"])
    sfd = logistic((d - tp - x["rise"]) / x["w"]) * (1 - logistic((d - tshr - x["fall"]) / x["w"])) if x["sfd"] else 0
    w = max(early, sfd)
    p_c = w * logistic((margin - x["m50"]) / x["s"])
    p_b = (1 - w) * logistic((margin - x["brk"]) / x["brk_s"]) if d < tshr else 0
    return p_c, p_b


def capture_trials(offset, margin, wanted, air, n=200, opts=(), seed="1"):
    """n pairs: 1's frame at 2 at -70 dBm, 3's frame offset us later at
    -70 + margin. Returns the counts of (captured, broken, neither) and the
    medium log."""
    period = 30000
    r = {1: Radio(1, {10000 + k * period: [("tx", dict(wanted))] for k in range(n)}), 2: Radio(2),
         3: Radio(3, {10000 + offset + k * period: [("tx", dict(wanted))] for k in range(n)})}
    d, out, rows = medium_run(r, [(1, 2, -70), (3, 2, -70 + margin)],
                              opts=["--medium-capture-model=smooth", *opts], seed=seed)
    cap = sum(1 for x in rows if x[1] == 1 and x[4] == "captured")
    # A broken search: 1's failed verdict at 3's start
    brk = sum(1 for t, m in got(r[2], "xv") if (t - 10000) % period == offset and offset < air)
    return cap, brk, n - cap - brk, rows, out


def test_intf_smooth_capture():
    """--medium-capture-model=smooth: capture and break rates against the
    model's probabilities (200 pairs per case, 4 standard deviations), in
    mode 3 (early window edge at 38 bits = 253 us, SFD window 613-753 us)
    and mode 1a (early window growing with the margin), and determinism by
    seed. The default (step) model is the one of test_conductor.py."""
    m3 = dict(M3, payload=M3_PSDU)
    cases = [   # (rate, wanted fields, SHR bytes, airtime, offset, margin)
        (150000, m3, 14, M3_AIR, 100, 1.0),
        (150000, m3, 14, M3_AIR, 100, 3.0),
        (150000, m3, 14, M3_AIR, 250, 10.0),
        (150000, m3, 14, M3_AIR, 400, 10.0),
        (150000, m3, 14, M3_AIR, 600, 10.0),
        (150000, m3, 14, M3_AIR, 750, 10.0),
        (150000, m3, 14, M3_AIR, 400, 2.5),
        (50000, {}, 10, AIRTIME, 400, 5.5),
        (50000, {}, 10, AIRTIME, 600, 11.0),
    ]
    for rate, w, shr, air, offset, margin in cases:
        p_c, p_b = smooth_p(rate, shr, offset, margin)
        cap, brk, none, rows, out = capture_trials(offset, margin, w, air)
        n = cap + brk + none
        check(binom_ok(cap, n, p_c) and binom_ok(brk, n, p_b),
              f"{rate} +{offset} us, +{margin} dB: captured {cap}, broken {brk} of {n}; "
              f"model {p_c:.3f}, {p_b:.3f}")
        print(f"     {rate // 1000} kbit/s, +{offset} us, +{margin} dB: captured {cap}/{n} (model {p_c * 100:.0f} %), "
              f"broken {brk}/{n} (model {p_b * 100:.0f} %)")
    a = capture_trials(250, 10.0, m3, M3_AIR, n=50)[3]
    b = capture_trials(250, 10.0, m3, M3_AIR, n=50)[3]
    c = capture_trials(250, 10.0, m3, M3_AIR, n=50, seed="2")[3]
    check(a == b and a != c, "smooth capture: determinism by seed")


def test_intf_presync():
    """The presync curve (§13.3): node 3's frame starts 100 us after 1's,
    1 dB weaker (no capture): it was on air at the end of 1's SHR, so with
    --medium-int-curve-presync=3:-11.2,1.5 it interferes by that curve
    (formula, 200 frames); with the default (main curve) nearly every
    frame is lost. An interferer that starts after the SHR's end keeps the
    main curve even with the option (the measured locked-frame case)."""
    m3 = dict(M3, payload=M3_PSDU)
    bits = 8 * (2 + len(M3_PSDU) + 4)
    for opts, x0, b in ((["--medium-int-curve-presync=3:-11.2,1.5"], -11.2, 1.5), ([], -2.85, 0.9)):
        n = 200
        r = {1: Radio(1, {10000 + k * 30000: [("tx", dict(m3))] for k in range(n)}), 2: Radio(2),
             3: Radio(3, {10100 + k * 30000: [("tx", dict(m3))] for k in range(n)})}
        d, out, rows = medium_run(r, [(1, 2, -60), (3, 2, -61)], opts=opts)
        xv = [m for t, m in got(r[2], "xv") if m["frame_id"] % 2 == 1]
        k = sum(m["ok"] for m in xv)
        # 3's frame ends 100 us after 1's: it covers all of 1's counted bits
        want = (1 - min(0.5, 0.5 * math.exp(-(1 - x0) / b))) ** bits
        check(len(xv) == n and binom_ok(k, n, want), f"{opts}: {k}/{n}, formula {want:.3f}")
        print(f"     presync {opts or 'default'}: {k}/{n} received (formula {want * 100:.0f} %)")
    # After the SHR: the main curve (same as without the option)
    n = 100
    res = []
    for opts in ([], ["--medium-int-curve-presync=3:-11.2,1.5"]):
        r = {1: Radio(1, {10000 + k * 30000: [("tx", dict(m3))] for k in range(n)}), 2: Radio(2),
             3: Radio(3, {10000 + 900 + k * 30000: [("tx", dict(m3))] for k in range(n)})}
        d, out, rows = medium_run(r, [(1, 2, -60), (3, 2, -63)], opts=opts)
        res.append([m["ok"] for t, m in got(r[2], "xv")])
    check(res[0] == res[1], "presync option changed a frame interfered after its SHR")


# --- sniffing (§13.4) ------------------------------------------------------

def test_intf_sniff_external():
    """An FSK interferer's bursts send a preamble (64 bits at 50 kbit/s =
    1280 us): a sniff detects it after one dwell (frame_id 0, src 0xFFFF,
    the interferer's level), never locks; a noise-like interferer is not
    detected but is in the energy at the window's end, and lowers a
    frame's detection SINR."""
    spec = "chan=0,level=-70,kind=fsk,rate=50000,pattern=periodic:100ms:10ms:5ms"
    r = {2: Sniffer(2, {4000: [("sniff", 0, 4000, SNIFF_LOCK)], 7000 + 100000: [("sniff", 0, 2000)]})}
    d, out, rows, sn = sniff_run(r, [], opts=[f"--interferer={spec}"])
    res = r[2].results
    check(len(res) == 2 and (res[0][0], res[0][1]["detected"], res[0][1]["frame_id"], res[0][1]["src"],
                             res[0][1]["locked"]) == (5320, 1, 0, SRC_NONE, 0) and
          abs(res[0][1]["rssi_dbm"] + 70) < 0.01, f"fsk burst: {res}")
    # The second window starts after the burst's preamble (105000-106280):
    # not detected, the energy at its end is the interferer's
    check(res[1][1]["detected"] == 0 and abs(res[1][1]["rssi_dbm"] - energy(-70)) < 0.01, f"after preamble: {res}")
    check("1 of an external interferer" in out, f"summary {out[-600:]}")
    r = {1: Sniffer(1, {1000: [("tx", {})]}), 2: Sniffer(2, {1000: [("sniff", 0, 2000)]})}
    d, out, rows, sn = sniff_run(r, [(1, 2, -100)], opts=["--interferer=chan=0,level=-95"])
    res = r[2].results
    # SINR of 1's frame: -100 against -95 and the noise: about 2^-16 per try
    check(not res[0][1]["detected"] or abs(res[0][1]["sinr_db"] - sinr_db(-100, -95)) < 0.01, f"noise: {res}")
    r = {2: Sniffer(2, {1000: [("sniff", 0, 2000)]})}
    d, out, rows, sn = sniff_run(r, [], opts=["--interferer=chan=0,level=-95"])
    check(r[2].results[0][1]["detected"] == 0 and abs(r[2].results[0][1]["rssi_dbm"] - energy(-95)) < 0.01,
          f"noise energy: {r[2].results}")


def test_intf_sniff_streams():
    """An external interferer's preamble draws from its own hash, not from the
    node's detection stream: adding one, detected now and then in windows of
    its own, leaves the detections of the emulated frames in the other
    windows unchanged (100 one-dwell windows on a frame at the margin, and
    between them 100 windows on the interferer's preamble, 5 ms later)."""
    n = 100
    p = round(NOISE_DBM + 4.93, 2)
    acts1 = {10000 * (k + 1): [("tx", {})] for k in range(n)}
    acts2 = {}
    for k in range(n):
        acts2[10000 * (k + 1)] = [("sniff", 0, DWELL)]
        acts2[10000 * (k + 1) + 5000] = [("sniff", 0, 1500)]
    seqs, extra = [], 0
    for opts in ([], [f"--interferer=chan=0,level={p},kind=fsk,rate=50000,pattern=periodic:10ms:1500us:5000us"]):
        r = {1: Sniffer(1, acts1), 2: Sniffer(2, acts2)}
        d, out, rows, sn = sniff_run(r, [(1, 2, p)], opts=opts)
        seqs.append([x["outcome"] == "detected" and x["src"] == 1 for x in sn if x["t_req"] % 10000 == 0])
        extra = sum(1 for x in sn if x["outcome"] == "detected" and x["frame_id"] == 0)
    print(f"     frames detected {sum(seqs[0])}/{len(seqs[0])}; interferer detected {extra} times")
    check(len(seqs[0]) == n and seqs[0] == seqs[1] and 0 < extra < n and 0 < sum(seqs[0]) < n, f"{seqs}, {extra}")


def test_intf_sniff_energy():
    """--sniff-energy (the preamble detector off: a table curve at 0): two
    frames at -95 and -95.5 dBm start at 1000 us; a single one is below
    -93 dBm, their sum above. A sniff from 1100 us detects at 1100 + 200 us
    (--sniff-energy-us=200) on behalf of the stronger one, and locks onto it
    with SNIFF_LOCK. A frame alone: no energy detection. A constant noise
    interferer above the threshold: detected at the window's start + 200 us
    with frame_id 0. A frame already on air at the window's start: detected
    at the window's start + 200 us."""
    opts = ["--sniff-energy=-93", "--sniff-energy-us=200", "--sniff-curve=0:0,100:0"]
    r = {3: Sniffer(3, {1000: [("tx", {})]}), 4: Sniffer(4, {1000: [("tx", {})]}),
         2: Sniffer(2, {1100: [("sniff", 0, 3000, SNIFF_LOCK)]})}
    d, out, rows, sn = sniff_run(r, [(3, 2, -95), (4, 2, -95.5)], opts=opts)
    res = r[2].results
    check(len(res) == 1 and (res[0][0], res[0][1]["detected"], res[0][1]["src"], res[0][1]["locked"]) ==
          (1300, 1, 3, 1) and abs(res[0][1]["rssi_dbm"] + 95) < 0.01, f"summed energy: {res}")
    check([t for t, m in got(r[2], "xx")] == [1300], f"summed energy: lock {r[2].rx}")
    check("1 by energy" in out, "summary")
    r = {3: Sniffer(3, {1000: [("tx", {})]}), 2: Sniffer(2, {500: [("sniff", 0, 3000)]})}
    d, out, rows, sn = sniff_run(r, [(3, 2, -95)], opts=opts)
    check(results(r[2]) == [(3500, 0, 0, 0, 3500)], f"one frame: {r[2].results}")
    r = {2: Sniffer(2, {500: [("sniff", 0, 3000)]})}
    d, out, rows, sn = sniff_run(r, [], opts=opts + ["--interferer=chan=0,level=-90"])
    x = r[2].results[0][1]
    check(results(r[2]) == [(700, 1, 0, 0, 700)] and x["src"] == SRC_NONE and abs(x["rssi_dbm"] - energy(-90)) < 0.01,
          f"interferer energy: {r[2].results}")
    # A frame already on air at the window's start
    r = {3: Sniffer(3, {400: [("tx", {})]}), 2: Sniffer(2, {500: [("sniff", 0, 5400)]})}
    d, out, rows, sn = sniff_run(r, [(3, 2, -80)], opts=opts)
    check(results(r[2]) == [(700, 1, 0, 1, 700)], f"frame on air: {r[2].results}")


def test_intf_sniff_false():
    """--sniff-false: 400 empty windows of 1000 us. P = 0.25 per window:
    about 100 false detections (frame_id 0, src 0xFFFF), spread over the
    window; 100/s: P = 1 - exp(-0.1) per window. The receiver stays on
    (the radio is not turned off by the result). Same seed, same times."""
    acts = {2000 * (k + 1): [("sniff", 0, 1000)] for k in range(400)}
    for opt, p in (("0.25", 0.25), ("100/s", 1 - math.exp(-0.1))):
        r = {2: Sniffer(2, dict(acts))}
        d, out, rows, sn = sniff_run(r, [], opts=[f"--sniff-false={opt}"])
        det = [(t, x) for t, x in r[2].results if x["detected"]]
        offs = [x["t_us"] % 2000 for t, x in det]
        check(binom_ok(len(det), 400, p) and all(x["frame_id"] == 0 and x["src"] == SRC_NONE for t, x in det) and
              all(0 < o <= 1000 for o in offs), f"{opt}: {len(det)} false detections, offsets {offs[:10]}")
        check(f"{len(det)} false (noise)" in out, f"{opt}: summary")
        if opt == "0.25":
            m = sum(offs) / len(offs)
            check(abs(m - 500) < 4 * 289 / math.sqrt(len(offs)), f"offsets mean {m:.0f}")
            first = [t for t, x in det]
        print(f"     --sniff-false={opt}: {len(det)}/400 windows (expected {400 * p:.0f})")
    r = {2: Sniffer(2, dict(acts))}
    sniff_run(r, [], opts=["--sniff-false=0.25"])
    check([t for t, x in r[2].results if x["detected"]] == first, "false detections: determinism")


def test_intf_sniff_adjacent():
    """--sniff-adjacent: 1's frame on channel 1 at -60 dBm (from 1000 us)
    and 3's on channel 0 at -75 dBm (from 1100 us). off (default): 3's
    frame detected with its SINR against the noise only; on: against 1's
    leak (-70 dBm), and the energy at the window's end includes it; detect:
    1's leak itself is detected (first dwell at 1320 us), not locked."""
    def run(mode, lock=0):
        r = {1: Sniffer(1, {1000: [("tx", {"chan": 1})]}), 3: Sniffer(3, {1100: [("tx", {})]}),
             2: Sniffer(2, {1000: [("sniff", 0, 2000, lock)]})}
        opts = [] if mode is None else [f"--sniff-adjacent={mode}"]
        d, out, rows, sn = sniff_run(r, [(1, 2, -60), (3, 2, -75)], opts=opts + ["--sniff-curve=-100:1,100:1"])
        return r[2].results, out
    res, out = run(None)
    check(res[0][1]["src"] == 3 and abs(res[0][1]["sinr_db"] - sinr_db(-75)) < 0.01, f"off: {res}")
    res, out = run("on")
    check(res[0][1]["src"] == 3 and abs(res[0][1]["sinr_db"] - sinr_db(-75, -70)) < 0.01, f"on: {res}")
    res, out = run("detect", SNIFF_LOCK)
    check((res[0][0], res[0][1]["src"], res[0][1]["locked"]) == (1320, 1, 0) and abs(res[0][1]["rssi_dbm"] + 70) < 0.01
          and "1 of a frame on another channel" in out, f"detect: {res}")
    # Energy at the end of an empty window: the leak of a frame still on air
    r = {1: Sniffer(1, {1000: [("tx", {"chan": 1})]}), 2: Sniffer(2, {1000: [("sniff", 0, 2000)]})}
    d, out, rows, sn = sniff_run(r, [(1, 2, -60)], opts=["--sniff-adjacent=on"])
    check(r[2].results[0][1]["detected"] == 0 and abs(r[2].results[0][1]["rssi_dbm"] - energy(-70)) < 0.01,
          f"end energy: {r[2].results}")


def test_intf_positions():
    """pos= with --positions (§13.1): free-space path loss and the node's
    antenna gain, without shadowing: CCA energy at node 1 (10 m away) and
    node 2 (sqrt(200) m, gain 3 dBi); node 3 has no position and gets
    level=. With shadowing (4 dB, the default): the same values for the same
    seed, other values for another seed, and their spread around the mean."""
    pos = os.path.join(tc.ARGS.logdir, "nodes.pos")
    with open(pos, "w") as f:
        f.write("1 0 0 0\n2 10 0 0 0 3\n")
    f915 = 20 * math.log10(915) - 27.55
    spec = "--interferer=chan=0,pos=0/10,eirp=10,level=-95"
    r = {n: Radio(n, {1000: [("sense", 0)]}) for n in (1, 2, 3)}
    d, out, rows = medium_run(r, [], opts=[f"--positions={pos}", "--prop-model=freespace", "--prop-shadow=0", spec])
    want = {1: 10 - (20 * math.log10(10) + f915), 2: 10 + 3 - (20 * math.log10(math.sqrt(200)) + f915), 3: -95}
    for n, lvl in want.items():
        check(abs(r[n].sensed[0][2] - energy(lvl)) < 0.01, f"node {n}: {r[n].sensed}, expected {energy(lvl):.2f}")
    print(f"     levels: node 1 {want[1]:.2f} dBm, node 2 {want[2]:.2f} dBm, node 3 (no position) -95 dBm")
    got_s = {}
    for seed in ("1", "1", "2"):
        r = {n: Radio(n, {1000: [("sense", 0)]}) for n in (1, 2)}
        medium_run(r, [], opts=[f"--positions={pos}", "--prop-model=freespace", spec], seed=seed)
        got_s.setdefault(seed, []).append(tuple(round(r[n].sensed[0][2], 3) for n in (1, 2)))
    check(got_s["1"][0] == got_s["1"][1] and got_s["1"][0] != got_s["2"][0], f"shadowing by seed: {got_s}")
    # Spread: 40 interferers at the same distance from node 1, 4 dB shadowing
    lv = []
    for k in range(40):
        a = 2 * math.pi * k / 40
        r = {1: Radio(1, {1000: [("sense", 0)]})}
        medium_run(r, [], opts=[f"--positions={pos}", "--prop-model=freespace",
                                f"--interferer=chan=0,pos={10 * math.cos(a):.4f}/{10 * math.sin(a):.4f},eirp=10"])
        e = r[1].sensed[0][2]
        lv.append(dbm(mw(e) - mw(NOISE_DBM)) - want[1])
    m = sum(lv) / len(lv)
    sd = math.sqrt(sum((x - m) ** 2 for x in lv) / (len(lv) - 1))
    check(abs(m) < 4 * 4 / math.sqrt(40) and 2.5 < sd < 5.8, f"shadowing of 40 positions: mean {m:.2f} dB, sd {sd:.2f} dB")
    print(f"     shadowing over 40 positions: mean {m:+.2f} dB, standard deviation {sd:.2f} dB (4 dB set)")


def test_intf_smooth_continuous():
    """A continuous FSK interferer with --medium-capture-model=smooth: on air
    when the frame's SHR ends, so the presync curve applies (§13.3). Against
    the kits' continuous PN9 interferer (COCHANNEL_RESULTS.md §4.3, 50-byte
    frames, 200 per point; the kits' W/I axis is about 1 dB below the packet
    SIR, §3.2, so SIR = W/I + 1 dB): W/I -0.9 dB 49/200, +0.35 dB 309/400,
    +1.4 dB 195/200. The smooth model is within 20 points at each, the step
    model (the locked-frame curve) is more than 30 points off at the two
    upper points."""
    m3 = dict(M3, payload=M3_PSDU)
    for wi, kit in ((-0.9, 49 / 200), (0.35, 309 / 400), (1.4, 195 / 200)):
        res = {}
        for name, opts in (("smooth", ["--medium-capture-model=smooth"]), ("step", [])):
            e = -60 - (wi + 1)
            xv, lines, out = frames_run(f"chan=0,level={e:.2f},kind=fsk", -60, n=200, wanted=m3, opts=opts)
            res[name] = sum(m["ok"] for m in xv) / len(xv)
        check(abs(res["smooth"] - kit) < 0.2, f"W/I {wi}: smooth {res['smooth']:.2f}, kit {kit:.2f}")
        if wi > 0:
            check(abs(res["step"] - kit) > 0.3, f"W/I {wi}: step {res['step']:.2f}, kit {kit:.2f}")
        print(f"     W/I {wi:+.2f} dB: kit {kit * 100:.0f} %, smooth {res['smooth'] * 100:.0f} %, "
              f"step {res['step'] * 100:.0f} %")


def test_intf_long_sniff():
    """A sniff of one hour with a busy noise-like interferer (a 2 ms burst
    every 10 ms on average: about 360 000 intervals in the window) and the
    energy detector at a threshold it never reaches: the window ends with
    nothing detected, in a bounded time (the intervals are added a step at
    a time and dropped once past). The summary covers the run up to its
    end (3600.001 s: the run ends with the sniff), not its last medium event."""
    t0 = time.time()
    r = {2: Sniffer(2, {1000: [("sniff", 0, 3600 * 1000000)]})}
    d, out, rows, sn = sniff_run(r, [], opts=["--interferer=name=busy,chan=0,level=-100,pattern=burst:2ms:8ms",
                                              "--sniff-energy=-60", "--vtime-end=3700"])
    wall = time.time() - t0
    check(len(r[2].results) == 1 and r[2].results[0][1]["detected"] == 0 and r[2].results[0][0] == 3600001000,
          f"long sniff: {r[2].results}")
    check(wall < 60, f"long sniff took {wall:.1f} s")
    check("on 19.99" in out and "of 3600.001 s" in out, f"summary at the run's end: {out[-500:]}")
    print(f"     one-hour sniff with a busy interferer: {wall:.1f} s of wall time")


def test_intf_parsing():
    """--interferer-file lines of any length; a channel given twice is
    refused."""
    spec = os.path.join(tc.ARGS.logdir, "long.txt")
    with open(spec, "w") as f:
        f.write("# " + "x" * 10000 + "\n" + "name=l,chan=" + "/".join(str(c) for c in range(300)) + ",level=-90\n")
    srv = Server(["--vtime=1", "--vtime-end=0.001", f"--interferer-file={spec}"])
    d = Driver(tc.ARGS.sock, {1: tc.Script(1)})
    tc.Script.TABLE = {1: {0: ([], tc.NEVER)}}
    d.connect()
    d.run()
    out = srv.output()
    check(srv.wait() == 0 and "interferer l: noise, 300 channels" in out, f"long line: {out[-400:]}")
    start_fails(["--vtime=1", "--interferer=chan=1-3/2,level=-80"], "channel 2 given twice")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("server")
    ap.add_argument("--sock", default="/tmp/wssim-intf.sock")
    ap.add_argument("--logdir", default=None)
    ap.add_argument("-k", default="")
    args = ap.parse_args()
    if args.sock == "/tmp/sim_socket":
        sys.exit("refusing the default socket: it may belong to a real run")
    args.logdir = args.logdir or tempfile.mkdtemp(prefix="intf_")
    os.makedirs(args.logdir, exist_ok=True)
    args.copy = os.path.join(args.logdir, "wssimserver-intf")
    shutil.copy2(args.server, args.copy)
    args.bin = args.copy
    tc.ARGS = args
    tests = [(name[5:], fn) for name, fn in globals().items() if name.startswith("test_intf") and callable(fn)]
    failed = 0
    for name, fn in tests:
        if args.k not in name:
            continue
        t0 = time.time()
        try:
            fn()
            print(f"PASS {name} ({time.time() - t0:.1f} s)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {e}")
    print(f"logs in {args.logdir}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
