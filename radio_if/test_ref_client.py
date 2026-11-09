#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests of the stack-neutral radio interface with the reference client.

    python3 radio_if/test_ref_client.py build-vt/wssimserver [--logdir DIR] [-k NAME]

Each test starts a copy of wssimserver on its own socket (so its own shared
memory names: a real run on the same PC is not disturbed) and one
ref_client.py process per node, then checks the nodes' logs, the server's
medium log and power report. Exit status 0 when every test passes. About
5 s in all.
"""
import argparse
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLIENT = os.path.join(HERE, "ref_client.py")
C_SRC = os.path.join(HERE, "c_example", "rif_node.c")
PROFILE = os.path.join(ROOT, "examples/power/efr32fg25-lfn.ini")
NOISE_DBM = -119.93
ARGS = None


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


def sock_path(name):
    """A short socket path: UNIX socket paths are limited to 107 bytes."""
    return os.path.join(ARGS.sockdir, name)


class Run:
    """A server and its nodes. nodes: {id: [client options]}; links: [(a, b, rssi)].
    Options starting with "@c" run the C example client instead of the Python one."""

    def __init__(self, name, nodes, links, end_s, opts=(), seed="1"):
        self.dir = os.path.join(ARGS.logdir, name)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir)
        self.sock = sock_path(name)
        self.mlog = os.path.join(self.dir, "medium.log")
        self.power = os.path.join(self.dir, "power.json")
        g = sum((["-g", f"{a},{b}:{r}"] for a, b, r in links), [])
        cmd = [ARGS.bin, f"--vtime={len(nodes)}", f"--vtime-end={end_s}", f"--medium-log={self.mlog}",
               f"--power={PROFILE}", f"--power-out={self.power}", *opts, *g, self.sock]
        env = dict(os.environ, WSSIM_SEED=seed)
        self.srv_log = os.path.join(self.dir, "server.log")
        t0 = time.monotonic()
        with open(self.srv_log, "w") as lf:
            srv = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)
            for _ in range(500):
                if os.path.exists(self.sock) or srv.poll() is not None:
                    break
                time.sleep(0.01)
            procs = {}
            for nid, nopts in nodes.items():
                out = open(os.path.join(self.dir, f"node{nid}.log"), "w")
                cmd = [ARGS.cbin] if nopts[:1] == ["@c"] else [sys.executable, CLIENT]
                nopts = nopts[1:] if nopts[:1] == ["@c"] else nopts
                procs[nid] = (subprocess.Popen([*cmd, "--sock", self.sock, "--id", str(nid), *map(str, nopts)],
                                               stdout=out, stderr=subprocess.STDOUT), out)
            try:
                self.status = srv.wait(60)
            except subprocess.TimeoutExpired:
                srv.kill()
                srv.wait()
                raise Fail("server did not end")
            for nid, (p, out) in procs.items():
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
                out.close()
                check(p.returncode == 0, f"node {nid} exit {p.returncode}: {self.text(f'node{nid}.log')[-1500:]}")
        self.wall = time.monotonic() - t0
        self.out = self.text("server.log")
        check(self.status == 0, f"server exit {self.status}: {self.out[-1500:]}")
        self.events = {nid: [ln.split() for ln in self.text(f"node{nid}.log").splitlines()] for nid in nodes}
        with open(self.mlog) as f:
            self.medium = [ln.split() for ln in f]
        with open(self.power) as f:
            self.report = json.load(f)

    def text(self, name):
        with open(os.path.join(self.dir, name)) as f:
            return f.read()

    def ev(self, nid, kind):
        """[(t, {k: v})] of node nid's events of this kind."""
        return [(int(e[0]), dict(kv.split("=", 1) for kv in e[3:])) for e in self.events[nid] if e[2] == kind]

    def outcomes(self):
        """{(src, dst, outcome): count} from the medium log."""
        c = {}
        for t, src, dst, fid, outcome, *_ in self.medium:
            k = (int(src), int(dst), outcome)
            c[k] = c.get(k, 0) + 1
        return c

    def node_power(self, nid):
        for n in self.report["nodes"]:
            if n["node"] == nid:
                return n
        raise Fail(f"node {nid} not in the power report")


def airtime(psdu_len=16, shr=8, phr=2, fcs=2, rate=50000):
    return (shr + phr + psdu_len + fcs) * 8 * 10**6 // rate


AIR = airtime()     # 4480 µs


# --- Tests --------------------------------------------------------------------

def test_exchange():
    """Three nodes that all hear each other: in each of 4 windows node 1 sends
    at 2 ms, node 2 at 10 ms, node 3 at 18 ms. Every frame reaches the two
    others; the radios are off between the windows, and the power report
    shows it."""
    nodes = {1: ["--tx", 2], 2: ["--tx", 10], 3: ["--tx", 18]}
    r = Run("exchange", nodes, [(1, 2, -70), (1, 3, -70), (2, 3, -75)], 4.5)
    for nid in nodes:
        ok = r.ev(nid, "rx_ok")
        check(len(ok) == 8 and len(r.ev(nid, "tx")) == 4, f"node {nid}: {len(ok)} received, "
              f"{len(r.ev(nid, 'tx'))} sent: {r.events[nid]}")
        check(not r.ev(nid, "rx_lost") and not r.ev(nid, "anomaly") and not r.ev(nid, "cca_busy"),
              f"node {nid}: losses or anomalies")
        # a frame is received at its end: TX start + airtime
        for t, kv in ok:
            src = int(kv["src"])
            off = {1: 2000, 2: 10000, 3: 18000}[src]
            check((t - off - AIR) % 1000000 == 0, f"node {nid}: frame from {src} received at {t}")
        check(r.ev(nid, "end")[0][1]["why"] == "end_of_the_run", "end")
    check(r.outcomes() == {(s, d, "ok"): 4 for s in nodes for d in nodes if s != d}, f"medium {r.outcomes()}")
    check("reached 4.030000 s" in r.out, "virtual time did not reach the last window's close")
    # Power: each node sent 4 frames, received 8, listened inside the windows
    # only, and slept deep with its radio off the rest of the time
    for nid in nodes:
        p = r.node_power(nid)
        radio = p["radio"]["states"]
        tx_us = radio["tx"]["time_us"]
        check([lv["dbm"] for lv in radio["tx"]["levels"]] == [10], f"node {nid}: TX levels {radio['tx']}")
        check(abs(tx_us - 4 * AIR) < 1e-6 * 4 * AIR + 1, f"node {nid}: TX {tx_us} us")
        check(abs(radio["receive"]["time_us"] - 8 * AIR) < 1, f"node {nid}: RX {radio['receive']['time_us']} us")
        on = sum(radio[s]["time_us"] for s in ("listen", "receive", "cca")) + tx_us
        check(abs(on - 4 * 30000) < 1, f"node {nid}: radio on {on} us, expected 4 windows of 30 ms")
        check(radio["off"]["time_us"] > 4.3e6 and p["mcu"]["states"]["deep_sleep"]["time_us"] > 4.3e6,
              f"node {nid}: off {radio['off']['time_us']} us, deep sleep {p['mcu']['states']['deep_sleep']}")
        c = p["check"]
        check(abs(c["mcu_time_us"] - 4.5e6) < 1 and abs(c["radio_time_us"] - 4.5e6) < 1,
              f"node {nid}: the state times do not add up: {c}")
    print(f"     wall {r.wall:.2f} s for 4.5 virtual s; average current node 1 "
          f"{r.node_power(1)['avg_current_ma'] * 1000:.1f} uA, node 2 {r.node_power(2)['avg_current_ma'] * 1000:.1f} uA")
    return r


def test_determinism():
    """The same seed gives identical node logs and medium log."""
    a = [Run(f"det{i}", {1: ["--tx", 2], 2: ["--tx", 2.5]}, [(1, 2, -90)], 3.5, seed="7") for i in (1, 2)]
    for name in ("node1.log", "node2.log", "medium.log"):
        check(a[0].text(name) == a[1].text(name), f"{name} differs between two runs with seed 7")


def test_collision():
    """Hidden terminals: 2 and 3 do not hear each other and both send at 10 ms
    to 1. 1 locks onto 2's frame (same µs: lower ID first), 3's frame is
    interference, and every frame is lost at 1; their CCAs found the channel
    clear."""
    nodes = {1: [], 2: ["--tx", 10], 3: ["--tx", 10]}
    r = Run("collision", nodes, [(1, 2, -70), (1, 3, -70)], 4.5)
    lost = r.ev(1, "rx_lost")
    check(len(lost) == 4 and all(kv["src"] == "2" and kv["cause"] == "interference" for t, kv in lost),
          f"node 1: {r.events[1]}")
    check(not r.ev(1, "rx_ok"), "node 1 received a frame")
    check(r.outcomes() == {(2, 1, "interference"): 4, (3, 1, "busy"): 4}, f"medium {r.outcomes()}")
    check(not r.ev(2, "cca_busy") and not r.ev(3, "cca_busy"), "a hidden terminal's CCA was busy")


def test_capture():
    """Same as the collision, but 3 is 20 dB stronger and starts 1 ms after 2,
    inside 2's preamble and sync word (8 bytes, 1.28 ms): 3 captures node 1."""
    nodes = {1: [], 2: ["--tx", 10], 3: ["--tx", 11]}
    r = Run("capture", nodes, [(1, 2, -80), (1, 3, -60)], 1.5)
    check([kv["src"] for t, kv in r.ev(1, "rx_start")] == ["2", "3"], f"node 1: {r.events[1]}")
    check([kv["src"] for t, kv in r.ev(1, "rx_ok")] == ["3"], f"node 1: {r.events[1]}")
    check(r.outcomes() == {(2, 1, "captured"): 1, (3, 1, "ok"): 1}, f"medium {r.outcomes()}")
    check(r.ev(1, "verdict_ignored") == [], "a verdict for the captured frame")


def test_half_duplex():
    """1 and 2 send at the same µs without CCA: 2 has locked onto 1's frame
    when its own timer fires (its wake at that µs comes after 1's), so it
    drops it (no verdict); 1 is sending when 2's frame starts. Nothing is
    received."""
    nodes = {1: ["--tx", 2, "--no-cca"], 2: ["--tx", 2, "--no-cca"]}
    r = Run("half_duplex", nodes, [(1, 2, -60)], 1.5)
    check(len(r.ev(2, "rx_aborted")) == 1 and not r.ev(1, "rx_ok") and not r.ev(2, "rx_ok"),
          f"logs {r.events}")
    check(r.outcomes() == {(1, 2, "tx_abort"): 1, (2, 1, "not_listening"): 1}, f"medium {r.outcomes()}")


def test_cca_busy():
    """The same with CCA: 2's CCA finds 1's frame on air (-60 dBm, above
    -85 dBm), so 2 does not send and receives 1's frame."""
    nodes = {1: ["--tx", 2], 2: ["--tx", 2]}
    r = Run("cca_busy", nodes, [(1, 2, -60)], 1.5)
    busy = r.ev(2, "cca_busy")
    check(len(busy) == 1 and abs(float(busy[0][1]["dbm"]) + 60) < 0.1, f"node 2: {r.events[2]}")
    check(len(r.ev(2, "rx_ok")) == 1 and not r.ev(2, "tx"), f"node 2: {r.events[2]}")
    check(r.outcomes() == {(1, 2, "ok"): 1}, f"medium {r.outcomes()}")
    check(r.node_power(2)["radio"]["states"]["cca"]["count"] == 1, f"CCA in the power report: {r.node_power(2)['radio']}")


def test_window_closed():
    """2's window is at another phase: 1's frames find its radio off."""
    nodes = {1: ["--tx", 2], 2: ["--phase-ms", 100]}
    r = Run("window_closed", nodes, [(1, 2, -60)], 2.5)
    check(not r.ev(2, "rx_start"), f"node 2: {r.events[2]}")
    check(r.outcomes() == {(1, 2, "not_listening"): 2}, f"medium {r.outcomes()}")


def test_drift_power_up():
    """Node 2 runs 100 ppm fast and powers up at 0.5 s: its windows start one
    period after its power-up on its own clock (about 1.5 s, then every
    0.9999 s of virtual time). Node 1's windows are at 1.5 s, 2.5 s...: its
    frames still fall in node 2's windows."""
    nodes = {1: ["--tx", 2, "--phase-ms", 500], 2: []}
    r = Run("drift", nodes, [(1, 2, -60)], 3.5, opts=["--drift=2:100", "--vtime-power-up=2:0.5"])
    opens = [t for t, kv in r.ev(2, "window_open")]
    # local = e + floor(e × ppb / 10^9): 999 901 + 99 = 1 000 000 at e = 999 901
    check(opens == [1499901, 2499801, 3499701], f"node 2 windows at {opens}")
    check(r.ev(2, "start")[0][1]["start_us"] == "500000", "start_us")
    check(len(r.ev(2, "rx_ok")) == 2, f"node 2: {r.events[2]}")


def test_c_client():
    """The C example client (radio_if/c_example, written from the document
    alone) with the Python one in one run. Node 1 (Python) sends at 2 ms in
    its 30 ms window each second; node 2 (C) listens all the time and sends at
    10 ms; node 3 (C) has its radio off, turns the receiver on 1 ms before
    each frame for its CCA and sends at 20 ms. 1 and 2 receive every frame of
    the others in their windows; 3 receives nothing; the power report has
    3's CCAs inside its short listens."""
    nodes = {1: ["--tx", 2], 2: ["@c", "--listen", "--offset-ms", 1010], 3: ["@c", "--offset-ms", 1020]}
    r = Run("c_client", nodes, [(1, 2, -70), (1, 3, -70), (2, 3, -75)], 3.5)
    srcs = lambda nid: sorted(int(kv["src"]) for t, kv in r.ev(nid, "rx_ok"))
    check(srcs(1) == [2, 2, 2, 3, 3, 3], f"node 1: {r.events[1]}")
    check(srcs(2) == [1, 1, 1, 3, 3, 3], f"node 2: {r.events[2]}")
    check(not r.ev(3, "rx_start") and len(r.ev(3, "tx")) == 3, f"node 3: {r.events[3]}")
    for nid in nodes:
        check(not r.ev(nid, "rx_lost") and not r.ev(nid, "anomaly") and not r.ev(nid, "cca_busy"),
              f"node {nid}: losses or anomalies")
    # frames at their offsets, received at their end; the PSDUs are intact
    for t, kv in r.ev(1, "rx_ok"):
        off = {2: 10000, 3: 20000}[int(kv["src"])]
        check((t - off - AIR) % 1000000 == 0 and kv["psdu"].startswith(f"C{kv['src']}S"), f"node 1: {t} {kv}")
    check(r.outcomes() == {(1, 2, "ok"): 3, (1, 3, "not_listening"): 3, (2, 1, "ok"): 3, (2, 3, "not_listening"): 3,
                           (3, 1, "ok"): 3, (3, 2, "ok"): 3}, f"medium {r.outcomes()}")
    p = r.node_power(3)["radio"]["states"]
    check(p["cca"]["count"] == 3 and abs(p["listen"]["time_us"] + p["cca"]["time_us"] - 3 * 1000) < 1 and
          abs(p["tx"]["time_us"] - 3 * AIR) < 1 and p["receive"]["time_us"] == 0, f"node 3 radio: {p}")
    check(r.node_power(2)["radio"]["states"]["receive"]["time_us"] == 6 * AIR, "node 2 receive time")
    return r


def test_sniff_lp():
    """Preamble sampling (version 2, §6.7). Node 1 sends two frames with a
    105 ms wake-up preamble ("xl") at 250 ms and 650 ms; nodes 2 and 3 sniff
    for 1 ms every 100 ms (at 0 and 37 ms in the period) with the radio off in
    between. Each receiver detects each preamble one dwell (16 bits at
    50 kbit/s, 320 us) into the first sniff that falls in it, is locked onto
    the frame, receives it, and finds the channel empty in its other sniffs.
    The server applies the sniff states for the clients (they report none of
    them), and the power report has exactly those times."""
    pre = 105000
    nodes = {1: ["--lp-tx", 250, "--lp-tx", 650, "--lp-preamble-ms", pre / 1000],
             2: ["--sniff-period-ms", 100, "--sniff-us", 1000],
             3: ["--sniff-period-ms", 100, "--sniff-us", 1000, "--sniff-phase-ms", 37]}
    r = Run("sniff_lp", nodes, [(1, 2, -70), (1, 3, -75), (2, 3, -75)], 0.95)
    check(r.ev(1, "start")[0][1]["can_sniff"] == "1", "the welcome did not offer sniffing")
    check([t for t, kv in r.ev(1, "lp_tx")] == [250000, 650000] and
          all(kv["total_us"] == str(pre + AIR) for t, kv in r.ev(1, "lp_tx")), f"node 1: {r.events[1]}")
    ends = [250000 + pre + AIR, 650000 + pre + AIR]
    detect = {2: [300320, 700320], 3: [337320, 737320]}
    for nid in (2, 3):
        d = r.ev(nid, "sniff_detect")
        check([int(kv["t_us"]) for t, kv in d] == detect[nid] == [t for t, kv in d] and
              all(kv["locked"] == "1" and kv["src"] == "1" for t, kv in d), f"node {nid} detections: {d}")
        st = r.ev(nid, "rx_start")
        check([t for t, kv in st] == detect[nid] and all(kv["via"] == "sniff" and kv["lp"] == "1" for t, kv in st),
              f"node {nid}: {st}")
        ok = r.ev(nid, "rx_ok")
        check([t for t, kv in ok] == ends and [kv["psdu"] for t, kv in ok] == ["L1S1", "L1S2"], f"node {nid}: {ok}")
        none = r.ev(nid, "sniff_none")
        check(len(r.ev(nid, "sniff")) == 10 and len(none) == 8 and
              all(abs(float(kv["rssi_dbm"]) - NOISE_DBM) < 0.01 for t, kv in none), f"node {nid}: {r.events[nid]}")
        check(not r.ev(nid, "anomaly") and not r.ev(nid, "rx_lost"), f"node {nid}: anomalies")
        radio = r.node_power(nid)["radio"]["states"]
        rx_us = sum(e - t for t, e in zip(detect[nid], ends))
        check(radio["receive"]["time_us"] == rx_us and radio["listen"]["time_us"] == 8 * 1000 + 2 * 320 and
              radio["tx"]["time_us"] == 0, f"node {nid} radio: {radio}")
        c = r.node_power(nid)["check"]
        check(abs(c["radio_time_us"] - 0.95e6) < 1, f"node {nid}: the state times do not add up: {c}")
    check(abs(r.node_power(1)["radio"]["states"]["tx"]["time_us"] - 2 * (pre + AIR)) < 1, "node 1 TX time")
    # the receivers' radios were off at each frame's start (not_listening),
    # then the sniff's lock received it (ok)
    check(r.outcomes() == {(1, 2, "not_listening"): 2, (1, 3, "not_listening"): 2, (1, 2, "ok"): 2, (1, 3, "ok"): 2},
          f"medium {r.outcomes()}")
    check("20 sniffs requested, 20 sniff results delivered" in r.out and "radio interface:" not in r.out,
          f"server log: {r.out[-1500:]}")


def test_sniff_frame_starts():
    """A frame with an ordinary 8-byte preamble that starts inside a sniff:
    node 1 sends at 1002 ms; node 2 sniffs for 1 ms every 100 ms from 1.5 ms,
    so its sniff at 1001.5 ms is on when the frame starts. The server put the
    client's radio in LISTEN for the sniff, so the medium locks it at the
    frame's start ("xx" at 1002 ms); the sniff then reports that frame as
    already being received (locked = 2). No other sniff detects anything."""
    nodes = {1: ["--tx", 2, "--no-cca"], 2: ["--sniff-period-ms", 100, "--sniff-us", 1000, "--sniff-phase-ms", 1.5]}
    r = Run("sniff_frame_starts", nodes, [(1, 2, -70)], 1.45)
    check([t for t, kv in r.ev(2, "rx_start")] == [1002000] and "via" not in r.ev(2, "rx_start")[0][1],
          f"node 2: {r.events[2]}")
    d = r.ev(2, "sniff_detect")
    check([(t, kv["locked"]) for t, kv in d] == [(1002320, "2")], f"node 2: {d}")
    check([t for t, kv in r.ev(2, "rx_ok")] == [1002000 + AIR] and len(r.ev(2, "sniff_none")) == 14,
          f"node 2: {r.events[2]}")
    check(r.outcomes() == {(1, 2, "ok"): 1}, f"medium {r.outcomes()}")
    radio = r.node_power(2)["radio"]["states"]
    check(radio["receive"]["time_us"] == AIR and radio["listen"]["time_us"] == 14 * 1000 + 500, f"node 2: {radio}")


def test_c_sniff():
    """The C example client with interface version 2, written from the
    document alone, next to the Python one. Node 1 (C) sends two frames with a
    105 ms wake-up preamble ("xl") at 250 and 650 ms, its radio off in between
    and on 1 ms before each frame for its CCA; node 5 (Python) sends one at
    450 ms. Node 2 (C) and node 3 (Python) sniff 1 ms every 100 ms with the
    lock flag (at 0 and 37 ms in the period), their radio off in between: each
    detects every preamble one dwell into its first sniff inside it, is locked
    onto the frame and receives it; the server applies the sniff states (the
    clients report none). Node 4 (C, version 1) listens all the time: it locks
    onto each frame at its start and receives it, without the LONG_PREAMBLE
    flag, which version 1 does not have (it would stop on it)."""
    pre = 105000
    nodes = {1: ["@c", "--if-version", 2, "--lp-preamble-ms", pre / 1000, "--period-ms", 400, "--offset-ms", 250,
                 "--frames", 2],
             2: ["@c", "--if-version", 2, "--sniff-period-ms", 100, "--sniff-us", 1000, "--frames", 0],
             3: ["--sniff-period-ms", 100, "--sniff-us", 1000, "--sniff-phase-ms", 37],
             4: ["@c", "--listen", "--frames", 0],
             5: ["--lp-tx", 450, "--lp-preamble-ms", pre / 1000]}
    links = [(1, 2, -70), (1, 3, -75), (1, 4, -72), (5, 2, -70), (5, 3, -72), (5, 4, -74), (1, 5, -80)]
    r = Run("c_sniff", nodes, links, 0.95)
    starts = {1: [250000, 650000], 5: [450000]}
    ends = {s: [t + pre + AIR for t in ts] for s, ts in starts.items()}
    check(r.ev(1, "start")[0][1]["version"] == "2" and r.ev(1, "start")[0][1]["can_sniff"] == "1" and
          r.ev(4, "start")[0][1]["version"] == "1" and r.ev(4, "start")[0][1]["can_sniff"] == "0",
          "welcomes: version or SNIFF wrong")
    check([(t, kv["airtime_us"]) for t, kv in r.ev(1, "tx")] == [(t, str(pre + AIR)) for t in starts[1]],
          f"node 1: {r.events[1]}")
    detect = {2: [300320, 500320, 700320], 3: [337320, 537320, 737320]}
    src_of = {300320: 1, 500320: 5, 700320: 1, 337320: 1, 537320: 5, 737320: 1}
    for nid in (2, 3):
        d = r.ev(nid, "sniff_detect")
        check([(t, int(kv["t_us"]), kv["locked"], int(kv["src"])) for t, kv in d] ==
              [(t, t, "1", src_of[t]) for t in detect[nid]], f"node {nid} detections: {d}")
        st = r.ev(nid, "rx_start")
        check([t for t, kv in st] == detect[nid] and all(kv["via"] == "sniff" and kv["lp"] == "1" for t, kv in st),
              f"node {nid}: {st}")
        ok = r.ev(nid, "rx_ok")
        check([(t, int(kv["src"])) for t, kv in ok] == [(ends[1][0], 1), (ends[5][0], 5), (ends[1][1], 1)] and
              [kv["psdu"] for t, kv in ok] == ["C1S1", "L5S1", "C1S2"], f"node {nid}: {ok}")
        none = r.ev(nid, "sniff_none")
        check(len(r.ev(nid, "sniff")) == 10 and len(none) == 7 and
              all(abs(float(kv["rssi_dbm"]) - NOISE_DBM) < 0.01 for t, kv in none), f"node {nid}: {r.events[nid]}")
        check(not r.ev(nid, "anomaly") and not r.ev(nid, "rx_lost"), f"node {nid}: anomalies")
        radio = r.node_power(nid)["radio"]["states"]
        rx_us = sum(e - t for t, e in zip(detect[nid], sorted(ends[1] + ends[5])))
        check(radio["receive"]["time_us"] == rx_us and radio["listen"]["time_us"] == 7 * 1000 + 3 * 320 and
              radio["tx"]["time_us"] == 0, f"node {nid} radio: {radio}")
        check(abs(r.node_power(nid)["check"]["radio_time_us"] - 0.95e6) < 1, f"node {nid}: state times")
    # the version-1 listener: every frame from its start, flag LONG_PREAMBLE cleared
    st = r.ev(4, "rx_start")
    check([(t, kv["lp"], kv["via"]) for t, kv in st] == [(250000, "0", "listen"), (450000, "0", "listen"),
                                                         (650000, "0", "listen")] and
          [t for t, kv in r.ev(4, "rx_ok")] == sorted(ends[1] + ends[5]), f"node 4: {r.events[4]}")
    p1 = r.node_power(1)["radio"]["states"]
    check(abs(p1["tx"]["time_us"] - 2 * (pre + AIR)) < 1 and p1["cca"]["count"] == 2, f"node 1 radio: {p1}")
    check(abs(r.node_power(5)["radio"]["states"]["tx"]["time_us"] - (pre + AIR)) < 1, "node 5 TX time")
    oc = r.outcomes()
    check(all(o in ("ok", "not_listening") for (s_, d_, o) in oc) and
          sum(v for (s_, d_, o), v in oc.items() if o == "ok") == 9, f"medium {oc}")
    check("20 sniffs requested, 20 sniff results delivered" in r.out and "radio interface:" not in r.out,
          f"server log: {r.out[-1500:]}")


# --- Interface rules, with raw messages ------------------------------------

def raw_server(name, n, opts=(), links=("1,2:-60",), end="0.01"):
    d = os.path.join(ARGS.logdir, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    sock = sock_path(name)
    lf = open(os.path.join(d, "server.log"), "w")
    g = sum((["-g", x] for x in links), [])
    srv = subprocess.Popen([ARGS.bin, f"--vtime={n}", f"--vtime-end={end}", *opts, *g, sock],
                           stdout=lf, stderr=subprocess.STDOUT)
    for _ in range(500):
        if os.path.exists(sock) or srv.poll() is not None:
            break
        time.sleep(0.01)
    return srv, sock, os.path.join(d, "server.log"), lf


def refused(sock, flags, version):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.connect(sock)
    s.send(struct.pack("<2sHHH", b"xh", 1, flags, version))
    s.settimeout(5)
    try:
        return s.recv(64) == b""
    finally:
        s.close()


def test_hello_refused():
    """Hellos the server refuses: an unknown interface version, unknown flags,
    the interface without virtual time, and with the legacy medium."""
    srv, sock, log, lf = raw_server("hello", 1)
    check(refused(sock, 3, 3), "interface version 3 accepted")
    check(refused(sock, 3, 0), "interface version 0 accepted")
    check(refused(sock, 3 | 0x80, 1), "unknown flag accepted")
    check(refused(sock, 2, 1), "interface without virtual time accepted")
    srv.terminate()
    srv.wait()
    lf.close()
    # a later instance (--vtime-start) is checked as the first one
    srv, sock, log, lf = raw_server("hello_later", 1, ["--vtime-start=1:0.005"])
    first = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    first.connect(sock)
    first.send(struct.pack("<2sHHH", b"xh", 1, 3, 1))
    time.sleep(0.2)
    check(refused(sock, 2, 1), "a later instance with the interface but without virtual time accepted")
    first.close()
    srv.terminate()
    srv.wait()
    lf.close()
    srv, sock, log, lf = raw_server("hello_legacy", 1, ["--medium=legacy"])
    check(refused(sock, 3, 1), "interface with the legacy medium accepted")
    srv.terminate()
    srv.wait()
    lf.close()
    with open(log) as f:
        check("needs virtual time and the central medium" in f.read(), "no reason in the server log")


def test_tx_rules():
    """A frame sent while the reported state is not tx, and one sent before the
    end of the previous one, are not sent; a report that leaves tx while the
    frame is on air is applied but counted; the server says so."""
    sys.path.insert(0, HERE)
    import ref_client as rc
    srv, sock, log, lf = raw_server("tx_rules", 2)
    nodes = {}
    for nid in (1, 2):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        s.connect(sock)
        s.send(rc.HELLO.pack(b"xh", nid, 3, 1))
        nodes[nid] = s
    w1 = nodes[1].recv(64)
    check(rc.WELCOME.unpack(w1)[1] == 1, "the welcome's version is not the interface version")
    frame = rc.FRAME.pack(b"xx", 4, 0, 0, 0, 8, 2, 2, 0, 0, 0, 50000, 0, 0.0) + b"abcd"
    nodes[1].send(frame)                                    # not in tx: refused
    nodes[1].send(rc.REPORT.pack(b"xr", rc.TX, 0, 0, 0, 0))
    nodes[1].send(frame)                                    # sent
    nodes[1].send(frame)                                    # overlaps the previous: refused
    nodes[1].send(rc.REPORT.pack(b"xr", rc.LISTEN, 0, 0, 0, 0))   # its frame is still on air
    nodes[1].send(rc.IDLE.pack(b"xi", 0, 0, 0, rc.NEVER))
    nodes[2].recv(64)
    nodes[2].send(rc.REPORT.pack(b"xr", rc.LISTEN, 0, 0, 0, 0))
    nodes[2].send(rc.IDLE.pack(b"xi", 0, 0, 0, rc.NEVER))
    # node 2 was listening after node 1's frame started: it hears nothing
    for s in nodes.values():
        s.settimeout(10)
        check(s.recv(64)[:2] == b"xq", "no end of run")
        s.close()
    srv.wait(10)
    lf.close()
    with open(log) as f:
        out = f.read()
    check("1 frames sent while the reported radio state was not tx" in out and
          "1 frames sent before the end of the node's previous frame" in out and
          "1 radio reports leaving tx before the end of the node's frame" in out and
          "1 frames sent," in out, f"server log: {out[-1500:]}")


def test_sniff_version():
    """Capability discovery: a version-2 client's welcome has SNIFF and its
    "xn" is answered; a version-1 client's welcome does not, and its "xn" and
    "xl" are dropped and counted. The TX of an "xl" lasts its preamble plus
    the frame: leaving TX after the frame's airtime alone is counted."""
    sys.path.insert(0, HERE)
    import ref_client as rc
    srv, sock, log, lf = raw_server("sniff_version", 2)
    nodes = {}
    for nid, ver in ((1, 1), (2, 2)):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        s.connect(sock)
        s.send(rc.HELLO.pack(b"xh", nid, 3, ver))
        nodes[nid] = s
    w = rc.WELCOME.unpack(nodes[1].recv(64))
    check(w[1] == 1 and not w[2] & rc.WELCOME_SNIFF, f"version-1 welcome {w}")
    frame = rc.FRAME.pack(b"xx", 4, 0, 0, 0, 8, 2, 2, 0, 0, 0, 50000, 0, 0.0) + b"abcd"
    nodes[1].send(rc.SNIFF.pack(b"xn", 0, 2000, 7, rc.SNIFF_LOCK, rc.OFF, 0))
    nodes[1].send(rc.REPORT.pack(b"xr", rc.TX, 0, 0, 0, 0))
    nodes[1].send(rc.LP_FRAME.pack(b"xl", 0, 1000) + frame)
    nodes[1].send(rc.REPORT.pack(b"xr", rc.OFF, 0, 0, 0, 0))
    nodes[1].send(rc.IDLE.pack(b"xi", 0, 0, 0, rc.NEVER))
    w = rc.WELCOME.unpack(nodes[2].recv(64))
    check(w[1] == 2 and w[2] & rc.WELCOME_SNIFF, f"version-2 welcome {w}")
    nodes[2].send(rc.SNIFF.pack(b"xn", 0, 2000, 9, rc.SNIFF_LOCK, rc.OFF, 0))
    nodes[2].send(rc.IDLE.pack(b"xi", 0, 0, 0, rc.NEVER))
    nodes[2].settimeout(10)
    a = rc.ADVANCE.unpack(nodes[2].recv(64))
    d = rc.SNIFF_RESULT.unpack(nodes[2].recv(64))
    check(a[1] == 1 and a[4] == 2000 and d[1] == 0 and d[3] == 9 and d[7] == 2000, f"wake {a}, result {d}")
    # an "xl": 1000 us of preamble, then 2560 us of frame
    nodes[2].send(rc.REPORT.pack(b"xr", rc.TX, 0, 0, 0, 0))
    nodes[2].send(rc.LP_FRAME.pack(b"xl", 0, 1000) + frame)
    nodes[2].send(rc.IDLE.pack(b"xi", 0, 0, 0, 2000 + 2560))
    a = rc.ADVANCE.unpack(nodes[2].recv(64))
    nodes[2].send(rc.REPORT.pack(b"xr", rc.OFF, 0, 0, 0, 0))     # 1000 us early
    nodes[2].send(rc.IDLE.pack(b"xi", 0, 0, 0, rc.NEVER))
    for s in nodes.values():
        s.settimeout(10)
        check(s.recv(64)[:2] == b"xq", "no end of run")
        s.close()
    srv.wait(10)
    lf.close()
    with open(log) as f:
        out = f.read()
    check("node 1: 2 sniff messages" in out and "1 frames sent," in out and
          "node 2: 1 radio reports leaving tx" in out, f"server log: {out[-1500:]}")


class RawNode:
    """A node driven message by message, for the interface's rules."""

    def __init__(self, rc, sock, nid, version=2):
        self.rc, self.nid = rc, nid
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.s.connect(sock)
        self.s.settimeout(10)
        self.s.send(rc.HELLO.pack(b"xh", nid, 3, version))

    def welcome(self):
        return self.rc.WELCOME.unpack(self.s.recv(64))

    def send(self, *msgs):
        for m in msgs:
            self.s.send(m)

    def report(self, state, chan=0, dbm=0):
        self.s.send(self.rc.REPORT.pack(b"xr", state, dbm, 0, 0, chan))

    def sniff(self, chan, us, sid, end_state, lock=True):
        self.s.send(self.rc.SNIFF.pack(b"xn", chan, us, sid, self.rc.SNIFF_LOCK if lock else 0, end_state, 0))

    def idle(self, t):
        self.s.send(self.rc.IDLE.pack(b"xi", 0, 0, 0, t))

    def wake(self):
        """(now, [(magic, message)])"""
        a = self.rc.ADVANCE.unpack(self.s.recv(64))
        check(a[0] == b"xa", f"node {self.nid}: expected a wake, got {a}")
        msgs = [self.s.recv(8192) for _ in range(a[1])]
        return a[4], [(m[:2], m) for m in msgs]

    def quit(self):
        m = self.s.recv(64)
        self.s.close()
        return m[:2] == b"xq"


def frame(rc, n, chan=0):
    return rc.FRAME.pack(b"xx", n, chan, 0, 0, 8, 2, 2, 0, 0, 0, 50000, 0, 0.0) + b"p" * n


def test_sniff_xn_while_receiving():
    """§6.7: "xn" while holding a frame. Node 3 sends a 100-byte frame at 0
    (17920 us on air); nodes 1 and 2, listening, get it at 0 and report
    RECEIVE. Node 2 sends "xn" at once, inside the frame's preamble: the frame
    it held is lost ("left"), and the sniff detects the same frame one dwell
    later (320 us) and locks onto it again: its "xx" comes again with the same
    frame ID, then its verdict. Node 1 sends "xn" (2000 us) at 5000 us, after
    the preamble: the frame is lost ("left"), no verdict ever comes, and the
    sniff, which cannot detect a frame past its preamble, ends at 7000 with
    nothing detected and the frame's power as the channel's energy. Node 3
    itself sends "xn" (5000 us) just after its frame: its own frame fills the
    window, so its "xd" comes at 5000 with nothing detected and the server
    leaves its radio in TX, which it leaves at the frame's end: its TX time is
    the whole frame, with no rule broken."""
    sys.path.insert(0, HERE)
    import ref_client as rc
    air = (8 + 2 + 100 + 2) * 8 * 10**6 // 50000
    d_ = os.path.join(ARGS.logdir, "sniff_xn_rx")
    mlog, pout = os.path.join(d_, "medium.log"), os.path.join(d_, "power.json")
    srv, sock, log, lf = raw_server("sniff_xn_rx", 3, ["--medium-log=" + mlog, "--power=" + PROFILE,
                                                       "--power-out=" + pout], ["3,1:-60", "3,2:-60"], "0.03")
    n1, n2, n3 = RawNode(rc, sock, 1), RawNode(rc, sock, 2), RawNode(rc, sock, 3)
    for n in (n1, n2):
        n.welcome()
        n.report(rc.LISTEN)
        n.idle(rc.NEVER)
    n3.welcome()
    n3.report(rc.TX)
    n3.send(frame(rc, 100))
    n3.sniff(0, 5000, 30, rc.OFF)                      # during its own frame
    n3.idle(air)
    t, m = n1.wake()
    check(t == 0 and [k for k, _ in m] == [b"xx"], f"node 1 at {t}: {m}")
    n1.report(rc.RECEIVE)
    n1.idle(5000)
    t, m = n2.wake()
    check(t == 0 and [k for k, _ in m] == [b"xx"], f"node 2 at {t}: {m}")
    fid = rc.FRAME.unpack_from(m[0][1])[12]
    n2.report(rc.RECEIVE)
    n2.sniff(0, 2000, 20, rc.OFF)                      # holding the frame, in its preamble
    n2.idle(rc.NEVER)
    t, m = n2.wake()
    d = rc.SNIFF_RESULT.unpack(m[0][1]) if m else None
    check(t == 320 and [k for k, _ in m] == [b"xd", b"xx"] and d[1] == 1 and d[2] == rc.LOCKED and d[4] == fid and
          rc.FRAME.unpack_from(m[1][1])[12] == fid, f"node 2 at {t}: {m}")
    n2.idle(rc.NEVER)
    t, m = n1.wake()
    check(t == 5000 and not m, f"node 1 at {t}: {m}")
    n1.sniff(0, 2000, 10, rc.OFF)                      # holding the frame, after its preamble
    n1.idle(rc.NEVER)
    t, m = n3.wake()
    d = rc.SNIFF_RESULT.unpack(m[0][1]) if m else None
    check(t == 5000 and d and d[1] == 0 and d[3] == 30 and d[7] == 5000, f"node 3 at {t}: {d}")
    n3.idle(air)
    t, m = n1.wake()
    d = rc.SNIFF_RESULT.unpack(m[0][1]) if m else None
    check(t == 7000 and len(m) == 1 and d[1] == 0 and d[3] == 10 and abs(d[8] + 60) < 0.01,
          f"node 1 at {t}: {d}")
    n1.idle(rc.NEVER)
    t, m = n2.wake()
    v = rc.VERDICT.unpack(m[0][1]) if m else None
    check(t == air and v and v[1] == 1 and v[3] == fid, f"node 2 at {t}: {v}")
    n2.report(rc.OFF)
    n2.idle(rc.NEVER)
    t, m = n3.wake()
    check(t == air and not m, f"node 3 at {t}: {m}")
    n3.report(rc.OFF)
    n3.idle(rc.NEVER)
    check(n1.quit() and n2.quit() and n3.quit(), "no end of run; node 1 got more than its sniff result")
    srv.wait(10)
    lf.close()
    with open(log) as f:
        out = f.read()
    with open(mlog) as f:
        med = sorted((x[1], x[2], x[4]) for x in (ln.split() for ln in f))
    check(med == [("3", "1", "left"), ("3", "2", "left"), ("3", "2", "ok")], f"medium log {med}")
    with open(pout) as f:
        pw = {n["node"]: n["radio"]["states"] for n in json.load(f)["nodes"]}
    check(pw[1]["receive"]["time_us"] == 5000 and pw[1]["listen"]["time_us"] == 2000 and
          pw[2]["receive"]["time_us"] == air - 320 and pw[2]["listen"]["time_us"] == 320 and
          pw[3]["tx"]["time_us"] == air and pw[3]["listen"]["time_us"] == 0, f"power {pw}")
    check("radio interface:" not in out, f"server log: {out[-1500:]}")


def test_sniff_report_in_window():
    """§6.7: radio reports during a sniff window. Nodes 1, 3 and 4 sniff
    channel 0 for 10 ms from 0 with the lock flag. At 2 ms node 1 reports OFF
    and node 4 LISTEN on channel 1: their sniffs end with no "xd" (outcome
    "left"); node 3 reports LISTEN on channel 0, which keeps its sniff. Node 2
    sends a frame with a 20 ms wake-up preamble at 3 ms: node 3, listening,
    locks onto it at its start and reports RECEIVE (a report during the
    window that keeps the sniff); its sniff detects that frame one dwell later
    (locked = 2) and its verdict comes at the frame's end. Nodes 1 and 4 hear
    nothing more, and node 1's radio is charged listening for 2 ms only."""
    sys.path.insert(0, HERE)
    import ref_client as rc
    d_ = os.path.join(ARGS.logdir, "sniff_report")
    slog, mlog, pout = (os.path.join(d_, x) for x in ("sniff.log", "medium.log", "power.json"))
    srv, sock, log, lf = raw_server("sniff_report", 4, ["--sniff-log=" + slog, "--medium-log=" + mlog,
                                                        "--power=" + PROFILE, "--power-out=" + pout],
                                    ["2,1:-60", "2,3:-60", "2,4:-60"], "0.05")
    n = {i: RawNode(rc, sock, i) for i in (1, 2, 3, 4)}
    pre, end = 20000, 3000 + 20000 + AIR
    for i in (1, 2, 3, 4):
        n[i].welcome()
        if i == 2:
            n[i].idle(3000)
        else:
            n[i].sniff(0, 10000, i, rc.LISTEN if i == 1 else rc.OFF)
            n[i].idle(2000)
    for i, (st, ch) in ((1, (rc.OFF, 0)), (3, (rc.LISTEN, 0)), (4, (rc.LISTEN, 1))):
        t, m = n[i].wake()
        check(t == 2000 and not m, f"node {i} at {t}: {m}")
        n[i].report(st, ch)
        n[i].idle(rc.NEVER)
    t, m = n[2].wake()
    n[2].report(rc.TX)
    n[2].send(rc.LP_FRAME.pack(b"xl", 0, pre) + frame(rc, 16))
    n[2].idle(end)
    t, m = n[3].wake()
    f = rc.FRAME.unpack_from(m[0][1]) if m else None
    check(t == 3000 and len(m) == 1 and m[0][0] == b"xx" and f[9] == rc.FRAME_MEDIUM | rc.FRAME_LONG_PREAMBLE,
          f"node 3 at {t}: {m}")
    n[3].report(rc.RECEIVE)
    n[3].idle(rc.NEVER)
    t, m = n[3].wake()
    d = rc.SNIFF_RESULT.unpack(m[0][1]) if m else None
    check(t == 3320 and len(m) == 1 and d[1] == 1 and d[2] == rc.RECEIVING and d[3] == 3, f"node 3 at {t}: {d}")
    n[3].idle(rc.NEVER)
    t, m = n[2].wake()
    n[2].report(rc.OFF)
    n[2].idle(rc.NEVER)
    t, m = n[3].wake()
    v = rc.VERDICT.unpack(m[0][1]) if m else None
    check(t == end and v and v[1] == 1, f"node 3 at {t}: {v}")
    n[3].report(rc.OFF)
    n[3].idle(rc.NEVER)
    check(all(n[i].quit() for i in (1, 2, 3, 4)), "no end of run (or nodes 1, 4 woken)")
    srv.wait(10)
    lf.close()
    with open(log) as f:
        out = f.read()
    with open(slog) as f:
        sl = {int(x[1]): x[6] for x in (ln.split() for ln in f)}
    check(sl == {1: "left", 3: "detected", 4: "left"}, f"sniff log {sl}")
    with open(mlog) as f:
        med = sorted((x[2], x[4]) for x in (ln.split() for ln in f))
    check(med == [("1", "not_listening"), ("3", "ok"), ("4", "not_listening")], f"medium log {med}")
    with open(pout) as f:
        pw = {x["node"]: x["radio"]["states"] for x in json.load(f)["nodes"]}
    check(pw[1]["listen"]["time_us"] == 2000 and pw[3]["receive"]["time_us"] == end - 3000 and
          pw[3]["listen"]["time_us"] == 3000, f"power {pw}")
    check("radio interface:" not in out, f"server log: {out[-1500:]}")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("server", help="wssimserver binary of a virtual-time build (copied before use)")
    ap.add_argument("--logdir", default=None)
    ap.add_argument("-k", default="", help="run only tests whose name contains this")
    ARGS = ap.parse_args()
    ARGS.logdir = ARGS.logdir or tempfile.mkdtemp(prefix="radio_if_")
    os.makedirs(ARGS.logdir, exist_ok=True)
    ARGS.bin = os.path.join(ARGS.logdir, "wssimserver-radio-if")
    ARGS.sockdir = tempfile.mkdtemp(prefix="rif_", dir="/tmp")
    shutil.copy2(ARGS.server, ARGS.bin)
    ARGS.cbin = os.path.join(ARGS.logdir, "rif_node")
    subprocess.run(["cc", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", "-o", ARGS.cbin, C_SRC], check=True)
    tests = [(n[5:], f) for n, f in globals().items() if n.startswith("test_")]
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
    shutil.rmtree(ARGS.sockdir, ignore_errors=True)
    print(f"logs in {ARGS.logdir}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
