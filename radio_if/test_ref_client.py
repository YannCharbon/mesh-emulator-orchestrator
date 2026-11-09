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
    """A server and its nodes. nodes: {id: [client options]}; links: [(a, b, rssi)]."""

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
                procs[nid] = (subprocess.Popen([sys.executable, CLIENT, "--sock", self.sock, "--id", str(nid),
                                                *map(str, nopts)], stdout=out, stderr=subprocess.STDOUT), out)
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


# --- Interface rules, with raw messages ------------------------------------

def raw_server(name, n, opts=()):
    d = os.path.join(ARGS.logdir, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    sock = sock_path(name)
    lf = open(os.path.join(d, "server.log"), "w")
    srv = subprocess.Popen([ARGS.bin, f"--vtime={n}", "--vtime-end=0.01", *opts, "-g", "1,2:-60", sock],
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
    check(refused(sock, 3, 2), "interface version 2 accepted")
    check(refused(sock, 3 | 0x80, 1), "unknown flag accepted")
    check(refused(sock, 2, 1), "interface without virtual time accepted")
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
    end of the previous one, are not sent; the server says so."""
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
          "1 frames sent," in out, f"server log: {out[-1500:]}")


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
