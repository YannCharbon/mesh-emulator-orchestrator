#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests of the central medium's preamble sniffing (docs/MEDIUM.md §10) with
fake nodes.

    python3 sim_utils/vtime/test_sniff.py build-vt/wssimserver [--sock /tmp/sniff_sock] [--logdir DIR]

Same harness as test_conductor.py (a copy of the server on its own socket).
Exit status 0 when every test passes.

Frames of the fake nodes, with the defaults: 50 kbit/s, SHR 10 bytes (1600
us, of which the last 16 bits, 320 us, are the SFD: the preamble is 1280 us),
36 bytes on air (5760 us). A dwell of 16 bits is 320 us.
"""
import argparse
import json
import math
import os
import shutil
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_conductor as tc  # noqa: E402
import test_power  # noqa: E402
from test_conductor import Server, check, got, NOISE_DBM, PAYLOAD, AIRTIME  # noqa: E402
from fake_node import (Behaviour, Driver, TestError, NEVER, RADIO_OFF, RADIO_LISTEN, RADIO_RECEIVE, RADIO_TX,  # noqa: E402
                       FRAME_MEDIUM, FRAME_LONG_PREAMBLE, SNIFF_LOCK, SNIFF_LOCKED, SNIFF_RECEIVING)

RADIO_REPORT = struct.Struct("=2sBbBBH")   # "xr": magic, state, tx_dbm, events, phy_mode, channel
PRE_US = 1280           # preamble of a default frame (SHR minus the SFD)
DWELL = 320             # 16 bits at 50 kbit/s


class Sniffer(Behaviour):
    """A sniff-MAC radio. ACTIONS: {time: [action, ...]} with ("tx", fields),
    ("lp", preamble_us, fields), ("sniff", chan, duration, flags),
    ("radio", state, chan), ("raw", message). Its radio is off unless it sniffs or receives:
    a sniff puts it in LISTEN on the channel, a result without detection
    turns it off, an "xx" puts it in RECEIVE and a verdict turns it off."""

    def __init__(self, nid, actions=None, report=False):
        super().__init__(nid)
        self.actions = actions or {}
        self.results = []
        self.n_sniff = 0
        self.report = report        # send "xr" OFF after a verdict (power runs)

    def run(self, t, frames, first=False):
        self.now = t
        self.out = []
        if first:
            self.radio = (RADIO_OFF, 0)
        for fr in frames:
            if fr[0] == "xv":
                self.radio = (RADIO_OFF, self.radio[1])
                if self.report:
                    self.raw(RADIO_REPORT.pack(b"xr", RADIO_OFF, 0, 0, 0, self.radio[1]))
            elif fr[0] == "xd":
                self.results.append((t, fr[1]))
                if not fr[1]["detected"]:
                    self.radio = (RADIO_OFF, self.radio[1])
            else:
                self.radio = (RADIO_RECEIVE, self.radio[1])
        for a in self.actions.get(t, []):
            if a[0] == "tx":
                self.send(PAYLOAD, **a[1])
                self.radio = (RADIO_OFF, self.radio[1])
            elif a[0] == "lp":
                self.send_lp(PAYLOAD, a[1], **a[2])
            elif a[0] == "sniff":
                self.n_sniff += 1
                self.radio = (RADIO_LISTEN, a[1])
                self.sniff(a[1], a[2], sniff_id=self.n_sniff, flags=a[3] if len(a) > 3 else 0)
            elif a[0] == "radio":
                self.radio = (a[1], a[2])
            elif a[0] == "raw":
                self.raw(a[1])
        later = [x for x in self.actions if x > t]
        return self.out, min(later) if later else NEVER


def sniff_run(radios, links, opts=(), seed="1"):
    """Like test_conductor.medium_run(), with --sniff-log. Returns the
    driver, the server output, the medium log and the sniff log (dicts)."""
    slog = os.path.join(tc.ARGS.logdir, "sniff.log")
    if os.path.exists(slog):
        os.unlink(slog)
    d, out, rows = tc.medium_run(radios, links, opts=[f"--sniff-log={slog}", *opts], seed=seed)
    keys = ("t_req", "node", "id", "chan", "t0", "t1", "outcome", "t", "frame_id", "src", "rssi", "sinr", "locked")
    sniffs = []
    with open(slog) as f:
        for ln in f:
            v = ln.split()
            sniffs.append({k: (x if k == "outcome" else float(x) if k in ("rssi", "sinr") else int(x))
                           for k, x in zip(keys, v)})
    return d, out, rows, sniffs


def sinr_db(p, *intf):
    return p - 10 * math.log10(10 ** (NOISE_DBM / 10) + sum(10 ** (i / 10) for i in intf))


def p_dwell(sinr, bits=16):
    ber = min(0.5, 0.5 * math.exp(-0.5 * sinr))
    return (1 - ber) ** bits


def results(r):
    return [(t, x["detected"], x["locked"], x["frame_id"], x["t_us"]) for t, x in r.results]


def test_sniff_detect():
    """1 sends at 1000 (preamble until 2280); 2 sniffs at 1100 for 2000 us,
    its radio off before: detected after one dwell, at 1420, at the frame's
    power; no lock asked, so no "xx". 3 sniffs from 3000 (after the
    preamble): nothing; the result at the window's end gives the energy then
    (1's frame still on air)."""
    r = {1: Sniffer(1, {1000: [("tx", {})]}), 2: Sniffer(2, {1100: [("sniff", 0, 2000)]}),
         3: Sniffer(3, {3000: [("sniff", 0, 2000)]})}
    d, out, rows, sn = sniff_run(r, [(1, 2, -60), (1, 3, -70)])
    check(results(r[2]) == [(1420, 1, 0, 1, 1420)], f"2: {r[2].results}")
    x = r[2].results[0][1]
    check(x["src"] == 1 and x["rssi_dbm"] == -60 and abs(x["sinr_db"] - sinr_db(-60)) < 0.01, f"2: {x}")
    check(not got(r[2], "xx"), "2 got the frame without a lock")
    check(results(r[3]) == [(5000, 0, 0, 0, 5000)], f"3: {r[3].results}")
    check(abs(r[3].results[0][1]["rssi_dbm"] - (-70)) < 0.01, f"3 energy {r[3].results[0][1]}")
    check([(s["node"], s["outcome"]) for s in sn] == [(2, "detected"), (3, "none")], f"sniff log {sn}")
    # The radios were off at the frame's start: the medium log says so
    check({(x[2], x[4]) for x in rows} == {(2, "not_listening"), (3, "not_listening")}, f"log {rows}")


def test_sniff_empty():
    """Nothing on the channel: no detection, the result at the window's end
    with the noise floor; no message before it."""
    r = {1: Sniffer(1, {500: [("sniff", 4, 1500)]})}
    d, out, rows, sn = sniff_run(r, [])
    check(results(r[1]) == [(2000, 0, 0, 0, 2000)], f"{r[1].results}")
    check(abs(r[1].results[0][1]["rssi_dbm"] - NOISE_DBM) < 0.01, f"{r[1].results}")
    check([t for t, n, f, o in d.log if n == 1] == [0, 500, 2000], f"wakes {d.log}")
    check("sniff none     1" in out, "summary")


def test_sniff_threshold():
    """--sniff-threshold=-90: a frame at -95 dBm is not detected, one at -85
    is. Without a threshold, -95 dBm is detected (SINR 25 dB) and -125 dBm
    is not (below the noise: probability 2^-16 per dwell)."""
    for opts, p, det in ((["--sniff-threshold=-90"], -95, 0), (["--sniff-threshold=-90"], -85, 1),
                         ([], -95, 1), ([], -125, 0)):
        r = {1: Sniffer(1, {1000: [("tx", {})]}), 2: Sniffer(2, {1000: [("sniff", 0, 1000)]})}
        d, out, rows, sn = sniff_run(r, [(1, 2, p)], opts=opts)
        want = [(1320, 1, 0, 1, 1320)] if det else [(2000, 0, 0, 0, 2000)]
        check(results(r[2]) == want, f"{opts} {p} dBm: {r[2].results}")


def test_sniff_edges():
    """A dwell must fit in both the window and the frame's preamble. The
    window is [3000, 4000). A preamble ending 319 us after the window opens:
    not detected; 320 us: detected at 3320. A frame starting 319 us before
    the window closes: not detected; 320 us: detected at 4000. With
    --sniff-detect=frame the whole frame counts: the first is detected."""
    for start, opts, want in ((3000 - PRE_US + 319, [], None), (3000 - PRE_US + 320, [], 3320),
                              (4000 - 319, [], None), (4000 - 320, [], 4000),
                              (3000 - PRE_US + 319, ["--sniff-detect=frame"], 3320)):
        r = {1: Sniffer(1, {start: [("tx", {})]}), 2: Sniffer(2, {3000: [("sniff", 0, 1000)]})}
        d, out, rows, sn = sniff_run(r, [(1, 2, -60)], opts=opts)
        exp = [(want, 1, 0, 1, want)] if want else [(4000, 0, 0, 0, 4000)]
        check(results(r[2]) == exp, f"frame at {start} {opts}: {r[2].results}")


def test_sniff_interferer():
    """1 and 3 do not hear each other; 2 sniffs while both send. Equal power:
    SINR 0 dB, no detection in any dwell. 3 at -100 dBm: detected at the
    first dwell, its SINR the formula's; the interferer starting during a
    dwell lowers that dwell's SINR (lowest SINR over the dwell)."""
    for p3, det in ((-80, 0), (-100, 1)):
        r = {1: Sniffer(1, {1000: [("tx", {})]}), 2: Sniffer(2, {1000: [("sniff", 0, 2000)]}),
             3: Sniffer(3, {1100: [("tx", {})]})}
        d, out, rows, sn = sniff_run(r, [(1, 2, -80), (3, 2, p3)])
        if det:
            check(results(r[2]) == [(1320, 1, 0, 1, 1320)], f"{p3}: {r[2].results}")
            s = r[2].results[0][1]["sinr_db"]
            check(abs(s - sinr_db(-80, p3)) < 0.01, f"sinr {s}, formula {sinr_db(-80, p3)}")
        else:
            check(results(r[2]) == [(3000, 0, 0, 0, 3000)], f"{p3}: {r[2].results}")
            check(sn[0]["outcome"] == "none", f"{sn}")


def test_sniff_channel():
    """A frame on channel 3: a sniff on channel 0 misses it, one on 3
    detects it."""
    for chan, det in ((0, 0), (3, 1)):
        r = {1: Sniffer(1, {1000: [("tx", {"chan": 3})]}), 2: Sniffer(2, {1000: [("sniff", chan, 1000)]})}
        d, out, rows, sn = sniff_run(r, [(1, 2, -60)])
        want = [(1320, 1, 0, 1, 1320)] if det else [(2000, 0, 0, 0, 2000)]
        check(results(r[2]) == want, f"channel {chan}: {r[2].results}")


def test_sniff_half_duplex():
    """2 sends at 1000 (on air until 6760) and sniffs in the same run: deaf
    until its frame ends, so 1's preamble (1500 to 2780) is not detected and
    the log shows the window from 6760. Second case: 2 sends during its
    sniff: the sniff ends with no result ("tx_abort" in the log)."""
    r = {1: Sniffer(1, {1500: [("tx", {})]}), 2: Sniffer(2, {1000: [("tx", {}), ("sniff", 0, 8000)]})}
    d, out, rows, sn = sniff_run(r, [(1, 2, -60)])
    check(results(r[2]) == [(9000, 0, 0, 0, 9000)], f"2: {r[2].results}")
    check(sn[0]["t0"] == 1000 + AIRTIME and sn[0]["t1"] == 9000, f"window {sn}")
    r = {1: Sniffer(1, {3000: [("tx", {})]}), 2: Sniffer(2, {1000: [("sniff", 0, 5000)], 2000: [("tx", {})]})}
    d, out, rows, sn = sniff_run(r, [(1, 2, -60)])
    check(not r[2].results, f"2: {r[2].results}")
    check([s["outcome"] for s in sn] == ["tx_abort"] and sn[0]["t"] == 2000, f"log {sn}")
    check(not [t for t, n, f, o in d.log if n == 2 and t not in (0, 1000, 2000)], f"wakes of 2 {d.log}")


def marginal(seed, n=200):
    """n one-dwell sniffs of a frame at the margin (SINR 4.93 dB: p 0.5)."""
    p = NOISE_DBM + 4.93
    acts1 = {10000 * (k + 1): [("tx", {})] for k in range(n)}
    acts2 = {10000 * (k + 1): [("sniff", 0, DWELL)] for k in range(n)}
    r = {1: Sniffer(1, acts1), 2: Sniffer(2, acts2)}
    d, out, rows, sn = sniff_run(r, [(1, 2, round(p, 2))], seed=seed)
    return r, sn, p_dwell(sinr_db(round(p, 2)))


def test_sniff_curve_determinism():
    """One dwell per sniff at the margin: the detection rate is the curve's
    probability (within 4 sigma); the same seed gives the same sniff log,
    another seed another one."""
    r, sn1, p = marginal("1")
    det = sum(x["detected"] for t, x in r[2].results)
    n = len(r[2].results)
    sigma = math.sqrt(n * p * (1 - p))
    print(f"     {det}/{n} detected, curve {n * p:.1f} (sigma {sigma:.1f})")
    check(n == 200 and abs(det - n * p) < 4 * sigma, f"{det}/{n}, expected {n * p:.1f}")
    r2, sn2, _ = marginal("1")
    r3, sn3, _ = marginal("2")
    check(sn1 == sn2, "same seed, different sniff logs")
    check(sn1 != sn3, "another seed, same sniff log")


def test_sniff_table_curve():
    """A table curve replaces the default: 0 below 10 dB and 1 from 10 dB:
    a frame at SINR 9 dB is never detected, one at 11 dB always (no draw)."""
    for sinr, det in ((9, 0), (11, 1)):
        r = {1: Sniffer(1, {1000: [("tx", {})]}), 2: Sniffer(2, {1000: [("sniff", 0, 1000)]})}
        d, out, rows, sn = sniff_run(r, [(1, 2, round(NOISE_DBM + sinr, 2))],
                                     opts=["--sniff-curve=9.99:0,10:1"])
        want = [(1320, 1, 0, 1, 1320)] if det else [(2000, 0, 0, 0, 2000)]
        check(results(r[2]) == want, f"SINR {sinr}: {r[2].results}")
        check("0 detection draws" in out, "a draw with a 0/1 curve")


def test_sniff_lock_long_preamble():
    """1 sends at 1000 a frame after 100 ms of preamble ("xl"): on air until
    1000 + 100000 + 5760. 4 listens from the start and locks at 1000 as
    usual. 2 sniffs at 50000 with SNIFF_LOCK: detected at 50320, locked, the
    "xx" in the same wake (long-preamble flag), the verdict at the end, ok.
    3 sniffs without the lock flag: detected, no frame. 5 turns its receiver
    on at 50000 without a sniff: no lock (today's rule: only at a frame's
    start)."""
    end = 1000 + 100000 + AIRTIME
    r = {1: Sniffer(1, {1000: [("lp", 100000, {})]}),
         2: Sniffer(2, {50000: [("sniff", 0, 2000, SNIFF_LOCK)]}),
         3: Sniffer(3, {50000: [("sniff", 0, 2000)]}),
         4: Sniffer(4, {0: [("radio", RADIO_LISTEN, 0)]}),
         5: Sniffer(5, {50000: [("radio", RADIO_LISTEN, 0)]})}
    d, out, rows, sn = sniff_run(r, [(1, n, -70) for n in (2, 3, 4, 5)])
    check(results(r[2]) == [(50320, 1, SNIFF_LOCKED, 1, 50320)], f"2: {r[2].results}")
    xx = got(r[2], "xx")
    check(len(xx) == 1 and xx[0][0] == 50320 and xx[0][1]["frame_id"] == 1 and xx[0][1]["src"] == 1 and
          xx[0][1]["flags"] == FRAME_MEDIUM | FRAME_LONG_PREAMBLE and xx[0][1]["rx_dbm"] == -70 and
          xx[0][1]["payload"] == PAYLOAD, f"2 frames {xx}")
    seq = [k for t, k, m in r[2].rx]
    check(seq == ["xd", "xx", "xv"], f"2 message order {seq}")
    xv = got(r[2], "xv")
    check(xv[0][0] == end and xv[0][1]["ok"] == 1, f"2 verdict {xv}")
    check(results(r[3]) == [(50320, 1, 0, 1, 50320)] and not got(r[3], "xx"), f"3: {r[3].rx}")
    check([t for t, m in got(r[4], "xx")] == [1000] and got(r[4], "xv")[0][1]["ok"] == 1, f"4: {r[4].rx}")
    check(not got(r[5], "xx"), f"5: {r[5].rx}")
    outcomes = {(x[2], x[4]) for x in rows}
    check(outcomes == {(2, "not_listening"), (3, "not_listening"), (4, "ok"), (5, "not_listening"), (2, "ok")},
          f"medium log {rows}")
    check("1 locks made by a sniff" in out, "summary")


def test_sniff_lock_rules():
    """The lock after a detection needs the SFD ahead and a free receiver.
    (a) --sniff-detect=frame, sniff during the payload: detected, not locked.
    (b) 2 listens and locks onto 1's frame at its start, then sniffs with
    SNIFF_LOCK: the detection says it is already receiving that frame, and no
    second "xx". (c) A long preamble with an interferer only during the
    preamble, before the late lock: the verdict counts the bits from the lock,
    so the frame is received (SINR of its own part only)."""
    r = {1: Sniffer(1, {1000: [("tx", {})]}), 2: Sniffer(2, {4000: [("sniff", 0, 1000, SNIFF_LOCK)]})}
    d, out, rows, sn = sniff_run(r, [(1, 2, -60)], opts=["--sniff-detect=frame"])
    check(results(r[2]) == [(4320, 1, 0, 1, 4320)] and not got(r[2], "xx"), f"(a) {r[2].rx}")

    r = {1: Sniffer(1, {1000: [("tx", {})]}),
         2: Sniffer(2, {0: [("radio", RADIO_LISTEN, 0)], 1100: [("sniff", 0, 1000, SNIFF_LOCK)]})}
    d, out, rows, sn = sniff_run(r, [(1, 2, -60)])
    check(results(r[2]) == [(1420, 1, SNIFF_RECEIVING, 1, 1420)], f"(b) {r[2].results}")
    check([t for t, m in got(r[2], "xx")] == [1000], f"(b) frames {got(r[2], 'xx')}")

    r = {1: Sniffer(1, {1000: [("lp", 20000, {})]}), 3: Sniffer(3, {2000: [("tx", {})]}),
         2: Sniffer(2, {12000: [("sniff", 0, 2000, SNIFF_LOCK)]})}
    d, out, rows, sn = sniff_run(r, [(1, 2, -80), (3, 2, -80)])
    check(results(r[2]) == [(12320, 1, SNIFF_LOCKED, 1, 12320)], f"(c) {r[2].results}")
    xv = got(r[2], "xv")
    check(len(xv) == 1 and xv[0][1]["ok"] == 1 and abs(xv[0][1]["sinr_db"] - sinr_db(-80)) < 0.01, f"(c) {xv}")


def test_sniff_power():
    """The ledger counts a sniff as listening: 2 sniffs 2000 us with nothing
    on air (then off), and 3000 us with SNIFF_LOCK on 1's long-preamble
    frame: listening until the detection, receiving until the verdict, after
    which the radio reports itself off ("xr"). The radio reports nothing
    else: the conductor records the states the sniff implies."""
    prof = os.path.join(tc.ARGS.logdir, "sniff_profile.conf")
    with open(prof, "w") as f:
        f.write(test_power.PROFILE.replace("receive_ma = 5", "receive_ma = 6"))
    pout = os.path.join(tc.ARGS.logdir, "sniff_power.json")
    r = {1: Sniffer(1, {100000: [("lp", 10000, {})]}),
         2: Sniffer(2, {10000: [("sniff", 0, 2000)], 105000: [("sniff", 0, 3000, SNIFF_LOCK)]}, report=True)}
    d, out, rows, sn = sniff_run(r, [(1, 2, -60)], opts=[f"--power={prof}", f"--power-out={pout}", "--vtime-end=1"])
    check(results(r[2]) == [(12000, 0, 0, 0, 12000), (105320, 1, SNIFF_LOCKED, 1, 105320)], f"{r[2].results}")
    end = 100000 + 10000 + AIRTIME
    n = [x for x in json.load(open(pout))["nodes"] if x["node"] == 2][0]
    st = n["radio"]["states"]
    check(st["listen"]["time_us"] == 2000 + 320, f"listen {st['listen']}")
    check(st["receive"]["time_us"] == end - 105320, f"receive {st['receive']}")


def test_sniff_power_half_duplex():
    """2 reports TX, sends at 1000 (on air until 6760) and sniffs in the same
    run until 9000, nothing on air: the ledger keeps the TX time and counts
    listening only from the frame's end (the receiver is deaf until then),
    less the profile's TX-to-listen transition (20 us)."""
    prof = os.path.join(tc.ARGS.logdir, "sniff_profile_hd.conf")
    with open(prof, "w") as f:
        f.write(test_power.PROFILE)
    pout = os.path.join(tc.ARGS.logdir, "sniff_power_hd.json")
    r = {2: Sniffer(2, {1000: [("raw", RADIO_REPORT.pack(b"xr", RADIO_TX, 14, 0, 0, 0)), ("tx", {}),
                               ("sniff", 0, 8000)]})}
    d, out, rows, sn = sniff_run(r, [], opts=[f"--power={prof}", f"--power-out={pout}", "--vtime-end=1"])
    check(results(r[2]) == [(9000, 0, 0, 0, 9000)], f"{r[2].results}")
    n = [x for x in json.load(open(pout))["nodes"] if x["node"] == 2][0]
    st = n["radio"]["states"]
    check(st["tx"]["time_us"] == AIRTIME, f"tx {st['tx']}")
    check(st["listen"]["time_us"] == 9000 - (1000 + AIRTIME) - 20, f"listen {st['listen']}")


def test_sniff_settle():
    """--sniff-settle-us=200: a sniff at 1100 detects from 1300 (first dwell
    ends at 1620); a window shorter than the settling time detects nothing.
    The ledger counts the settling time as listening."""
    prof = os.path.join(tc.ARGS.logdir, "sniff_profile_st.conf")
    with open(prof, "w") as f:
        f.write(test_power.PROFILE)
    pout = os.path.join(tc.ARGS.logdir, "sniff_power_st.json")
    r = {1: Sniffer(1, {1000: [("tx", {})]}), 2: Sniffer(2, {1100: [("sniff", 0, 2000)]}),
         3: Sniffer(3, {1100: [("sniff", 0, 150)]})}
    d, out, rows, sn = sniff_run(r, [(1, 2, -60), (1, 3, -60)],
                                 opts=["--sniff-settle-us=200", f"--power={prof}", f"--power-out={pout}",
                                       "--vtime-end=1"])
    check(results(r[2]) == [(1620, 1, 0, 1, 1620)], f"2: {r[2].results}")
    check(results(r[3]) == [(1250, 0, 0, 0, 1250)], f"3: {r[3].results}")
    check([(s["node"], s["t0"]) for s in sn] == [(3, 1250), (2, 1300)], f"log {sn}")
    n = {x["node"]: x for x in json.load(open(pout))["nodes"]}
    check(n[3]["radio"]["states"]["listen"]["time_us"] == 150, f"3 listen {n[3]['radio']['states']['listen']}")


def test_sniff_lock_ended_frame():
    """The lock threshold after a detection counts only the frames still on
    air. 3's strong frame (-50 dBm) ends at 6760 during 2's window; 1's long
    preamble (-80 dBm) is detected at 7140 (the dwell from 6500 had 3's frame
    on air) and must be locked onto: 3's frame has left the air."""
    r = {1: Sniffer(1, {1000: [("lp", 20000, {})]}), 3: Sniffer(3, {1000: [("tx", {})]}),
         2: Sniffer(2, {6500: [("sniff", 0, 2000, SNIFF_LOCK)]})}
    d, out, rows, sn = sniff_run(r, [(1, 2, -80), (3, 2, -50)])
    check(results(r[2]) == [(7140, 1, SNIFF_LOCKED, 1, 7140)], f"{r[2].results}")
    check(len(got(r[2], "xv")) == 1 and got(r[2], "xv")[0][1]["ok"] == 1, f"{r[2].rx}")


def test_sniff_busy_receiver():
    """A receiver that locked onto a frame at its start during the window
    detects no other preamble. With a lock threshold of -10 dB and a curve
    that always detects: 1's long preamble (-55 dBm) is on air from 0; 2
    sniffs at 1000 with SNIFF_LOCK, its radio in LISTEN; 3's frame starts at
    1010 and 2 locks onto it at its start (-60 dBm, SINR -5 dB). The sniff
    reports 3's frame, already being received, at 1330, not 1's at 1320."""
    r = {1: Sniffer(1, {0: [("lp", 50000, {})]}), 3: Sniffer(3, {1010: [("tx", {})]}),
         2: Sniffer(2, {1000: [("sniff", 0, 2000, SNIFF_LOCK)]})}
    d, out, rows, sn = sniff_run(r, [(1, 2, -55), (3, 2, -60)], opts=["--medium-lock-snr=-10", "--sniff-curve=0:1"])
    check(results(r[2]) == [(1330, 1, SNIFF_RECEIVING, 2, 1330)], f"{r[2].results}")
    check([t for t, m in got(r[2], "xx")] == [1010], f"frames {got(r[2], 'xx')}")


def test_sniff_many_frames():
    """Weak frames (-125 dBm, about 2^-16 per dwell) start one after another
    during a 30 ms window, then a strong one at 25000: detected at its first
    dwell (25320). Each frame joining the sniff must not add a chain of check
    events: the same result as with the strong frame alone. 2's radio is in
    LISTEN, so it also locks onto the strong frame at its start (25000) and
    gets its verdict at its end; no other wake."""
    acts = {k: Sniffer(k, {1000 + 1000 * k: [("tx", {})]}) for k in range(3, 23)}
    r = {1: Sniffer(1, {25000: [("tx", {})]}), 2: Sniffer(2, {500: [("sniff", 0, 30000)]}), **acts}
    d, out, rows, sn = sniff_run(r, [(1, 2, -60)] + [(k, 2, -125) for k in acts])
    x = r[2].results[0][1] if r[2].results else {}
    check(results(r[2]) == [(25320, 1, 0, 21, 25320)] and x["src"] == 1, f"{r[2].results}")
    wakes = [t for t, n, f, o in d.log if n == 2]
    check(wakes == [0, 500, 25000, 25320, 25000 + AIRTIME], f"wakes of 2 {wakes}")





def test_sniff_options():
    for args, msg in ((["--vtime=1", "--medium=legacy", "--sniff-dwell-bits=8"], "need the central medium"),
                      (["--vtime=1", "--sniff-detect=x"], "preamble or frame"),
                      (["--vtime=1", "--sniff-curve=3:0.5,1:1"], "must ascend"),
                      (["--vtime=1", "--sniff-curve=ber:0"], "ber:N"),
                      (["--vtime=1", "--sniff-curve=1:2"], "bad probability"),
                      (["--vtime=1", "--sniff-dwell-bits=2.5"], "whole number"),
                      (["--vtime=1", "--sniff-curve=nan:0.5"], "bad SINR"),
                      (["--vtime=1", "--sniff-settle-us=-1"], "--sniff-settle-us"),
                      (["--vtime=1", "--sniff-settle-us=2.5"], "whole number")):
        srv = Server(args)
        check(srv.wait() != 0 and msg in srv.output(), f"{args} accepted, or no \"{msg}\"")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("server")
    ap.add_argument("--sock", default="/tmp/sniff_sock")
    ap.add_argument("--logdir", default=None)
    ap.add_argument("-k", default="")
    args = ap.parse_args()
    if args.sock == "/tmp/sim_socket":
        sys.exit("refusing the default socket: it may belong to a real run")
    args.logdir = args.logdir or tempfile.mkdtemp(prefix="sniff_")
    os.makedirs(args.logdir, exist_ok=True)
    args.copy = os.path.join(args.logdir, "wssimserver-sniff")
    shutil.copy2(args.server, args.copy)
    args.bin = args.copy
    tc.ARGS = args
    tests = [(name[5:], fn) for name, fn in globals().items() if name.startswith("test_sniff") and callable(fn)]
    failed = 0
    import time
    for name, fn in tests:
        if args.k not in name:
            continue
        t0 = time.time()
        try:
            fn()
            print(f"PASS {name} ({time.time() - t0:.1f} s)")
        except TestError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
    print(f"logs in {args.logdir}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
