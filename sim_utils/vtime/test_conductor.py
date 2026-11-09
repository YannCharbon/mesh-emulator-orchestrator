#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests of the wssimserver conductor (--vtime) with fake nodes.

    python3 sim_utils/vtime/test_conductor.py build/wssimserver [--sock /tmp/em3c_sock]

The server runs as a copy of the binary under another name, on its own socket
path (and so its own shared-memory names), so that the tests never touch a
wssimserver of a real run on the same PC. Exit status 0 when every test passes.
"""
import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_node import (Behaviour, Driver, Model, TestError, NEVER, HELLO, FRAME,  # noqa: E402
                       HELLO_VTIME, frame_msg, frame_unpack, shm_path, RADIO_OFF, RADIO_IDLE,
                       RADIO_LISTEN, RADIO_RECEIVE, WELCOME_MEDIUM, FRAME_MEDIUM)
import math  # noqa: E402
import socket  # noqa: E402

ARGS = None
# The conductor's own tests relay frames to every graph neighbour at the
# sender's time, as the legacy medium does; with the central medium (the
# default with --vtime) the fake nodes' radios, which report no state, would
# hear nothing.
LEGACY = "--medium=legacy"


class Server:
    def __init__(self, args, env=None):
        fd, self.log_path = tempfile.mkstemp(prefix="em3c_", suffix=".log", dir=ARGS.logdir)
        self.logf = os.fdopen(fd, "w")
        e = dict(os.environ)
        e.pop("WSSIM_SEED", None)
        e.update(env or {})
        if os.path.exists(ARGS.sock):
            os.unlink(ARGS.sock)
        self.p = subprocess.Popen([ARGS.bin] + args + [ARGS.sock], stdout=self.logf,
                                  stderr=subprocess.STDOUT, env=e)
        for _ in range(200):
            if os.path.exists(ARGS.sock):
                break
            if self.p.poll() is not None:
                break
            time.sleep(0.01)
        else:
            raise TestError("server socket did not appear")

    def wait(self, timeout=20):
        try:
            return self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()       # our own process only
            self.p.wait()
            raise TestError("server did not exit")
        finally:
            self.logf.close()

    def output(self):
        with open(self.log_path) as f:
            return f.read()


def graph_args(edges):
    return sum((["-g", f"{a},{b}"] for a, b in edges), [])


def graph_of(edges):
    g = {}
    for a, b in edges:
        g.setdefault(a, set()).add(b)
        g.setdefault(b, set()).add(a)
    return g


def check(cond, msg):
    if not cond:
        raise TestError(msg)


# --- Behaviours -------------------------------------------------------------

class Periodic(Behaviour):
    """A periodic sender. Some nodes react to frames:
    - a reply at the same time to some frames (same-time chains),
    - node 12 postpones its timer on every frame (deadline replaced later),
    - node 10 sets a short timer on every frame (deadline replaced earlier),
    - node 41 stops its timer after 20 ms (NEVER: only frames wake it).
    """
    PERIODS = {3: 2000, 7: 3000, 10: 2000, 12: 5000, 20: 3000, 41: 7000}

    def start(self):
        self.k = 0
        self.set_timer("tx", self.PERIODS[self.nid])
        if self.nid == 3:
            self.send("3:boot")         # a frame at time 0

    def on_timer(self, name):
        if name == "tx":
            self.k += 1
            self.send(f"{self.nid}:{self.now}:{self.k}")
            if not (self.nid == 41 and self.now > 20000):
                self.set_timer("tx", self.now + self.PERIODS[self.nid])

    def on_frame(self, src, payload):
        if not payload.startswith(b"r") and (sum(payload) + self.nid) % 4 == 0:
            self.send(f"r{self.nid}:{self.now}")
        if self.nid == 12 and "tx" in self.timers:
            self.timers["tx"] += 700
        if self.nid == 10:
            self.set_timer("early", self.now + 100)


class Script(Behaviour):
    """Timers and sends from a table: {time: (frames to send, next deadline)}."""
    TABLE = {}

    def run(self, t, frames, first=False):
        self.now = t
        out, nxt = self.TABLE.get(self.nid, {}).get(t, ([], NEVER))
        return [p.encode() for p in out], nxt


class PingPong(Behaviour):
    """Two nodes that answer each other at the same time, forever."""

    def start(self):
        if self.nid == 1:
            self.set_timer("go", 1000)

    def on_timer(self, name):
        self.send("ping")

    def on_frame(self, src, payload):
        self.send("pong")


class Forever(Behaviour):
    def start(self):
        self.set_timer("tx", 1000)

    def on_timer(self, name):
        self.send("x")
        self.set_timer("tx", self.now + 1000)


# --- Tests --------------------------------------------------------------------

PERIODIC_EDGES = [(3, 7), (7, 10), (10, 12), (12, 20), (20, 41), (3, 41), (7, 20)]


def run_periodic(seed_env=None):
    nodes = {n: Periodic(n) for n in Periodic.PERIODS}
    env = {"WSSIM_SEED": seed_env} if seed_env is not None else None
    srv = Server([f"--vtime={len(nodes)}", "--vtime-end=0.05", LEGACY] + graph_args(PERIODIC_EDGES), env)
    d = Driver(ARGS.sock, nodes)
    d.connect()
    shm = "/dev/shm/wssimcca." + ARGS.sock.replace("/", "_")
    shm_seen = os.path.exists(shm)
    log = d.run()
    status = srv.wait()
    return d, log, status, srv.output(), shm_seen, shm


def test_periodic_vs_model():
    d, log, status, out, shm_seen, shm = run_periodic()
    model_nodes = {n: Periodic(n) for n in Periodic.PERIODS}
    mlog, reason = Model(graph_of(PERIODIC_EDGES), model_nodes, end_us=50000).run()
    check(status == 0, f"server exit {status}")
    check(reason == "end time reached", reason)
    check(len(log) > 100, f"only {len(log)} wakes")
    for i, (a, b) in enumerate(zip(log, mlog)):
        check(a == b, f"wake {i} differs from the model:\n  server: {a}\n  model:  {b}")
    check(len(log) == len(mlog), f"{len(log)} wakes, model {len(mlog)}")
    check(d.quit == set(Periodic.PERIODS), f"xq to {sorted(d.quit)}")
    check(d.seed == 1, f"default seed {d.seed}")
    check(all(t <= 50000 for t, *_ in log), "wake after the end time")
    check("end time reached" in out, "no end reason in the log")
    check(f"{len(log) - len(Periodic.PERIODS)} wakes" in out, "wake count in the summary")
    check("wall s per simulated s" in out, "no speed summary")
    check(shm_seen, f"{shm} missing during the run")
    check(not os.path.exists(shm), f"{shm} left after the run")
    # Some properties of the scenario itself, so that it tests what it claims.
    times = [t for t, *_ in log]
    check(times == sorted(times), "time went backwards")
    check(any(log[i][0] == log[i + 1][0] and log[i][1] > log[i + 1][1] for i in range(len(log) - 1)),
          "no same-time wake of a lower node ID (frame at the same time)")
    check(any(f for _t, n, f, _o in log if n == 41), "no frame to 41")
    check(any(n == 10 and not o and not f for _t, n, f, o in log), "node 10's early timer never fired")
    return log, out


def test_determinism(first_log):
    _, log2, status, _, _, _ = run_periodic()
    check(status == 0, f"server exit {status}")
    check(log2 == first_log, "two runs give different event logs")


def test_seed():
    d, _, status, _, _, _ = run_periodic(seed_env="42")
    check(status == 0 and d.seed == 42, f"seed {d.seed}")


def test_ties():
    # Nodes 1, 2, 3 all due at 1000; node 3 sends at 1000 to 1 and 2.
    Script.TABLE = {
        1: {0: ([], 1000)},
        2: {0: ([], 1000)},
        3: {0: ([], 1000), 1000: (["from3"], NEVER)},
    }
    nodes = {n: Script(n) for n in (1, 2, 3)}
    srv = Server(["--vtime=3", "--vtime-end=1", LEGACY] + graph_args([(1, 3), (2, 3)]))
    d = Driver(ARGS.sock, nodes)
    d.connect()
    log = d.run()
    check(srv.wait() == 0, "server exit")
    got = [(t, n, f) for t, n, f, _ in log]
    want = [(0, 1, ()), (0, 2, ()), (0, 3, ()),
            (1000, 1, ()), (1000, 2, ()), (1000, 3, ()),
            (1000, 1, ((3, b"from3"),)), (1000, 2, ((3, b"from3"),))]
    check(got == want, f"order {got}")
    check("no more events" in srv.output(), "end reason")


def test_replacement():
    # Node 1 is due at 5000; a frame from 2 at 1000 wakes it, and it then
    # reports 3000: woken at 3000, never at 5000; then NEVER.
    # Node 2, woken by a frame from 3 at 500, reports the same deadline
    # again (1000): it is woken once at 1000, not twice.
    Script.TABLE = {
        1: {0: ([], 5000), 1000: ([], 3000), 3000: ([], NEVER)},
        2: {0: ([], 1000), 500: ([], 1000), 1000: (["hi"], 4000)},
        3: {0: ([], 500), 500: (["poke"], NEVER)},
    }
    nodes = {n: Script(n) for n in (1, 2, 3)}
    srv = Server(["--vtime=3", LEGACY] + graph_args([(1, 2), (2, 3)]))
    d = Driver(ARGS.sock, nodes)
    d.connect()
    log = d.run()
    check(srv.wait() == 0, "server exit")
    got = [(t, n) for t, n, _, _ in log]
    want = [(0, 1), (0, 2), (0, 3), (500, 3), (500, 2), (1000, 2), (1000, 1), (1000, 3),
            (3000, 1), (4000, 2)]
    check(got == want, f"wakes {got}")
    check("no more events" in srv.output(), "end reason")


def test_end_boundary():
    Script.TABLE = {1: {0: ([], 3000), 3000: ([], 6000)}}
    nodes = {1: Script(1)}
    srv = Server(["--vtime=1", "--vtime-end=0.003"])
    d = Driver(ARGS.sock, nodes)
    d.connect()
    log = d.run()
    check(srv.wait() == 0, "server exit")
    check([t for t, *_ in log] == [0, 3000], f"wakes {[t for t, *_ in log]}")
    check("reached 0.003000 s" in srv.output(), "virtual time reached")


class PastDeadline(Behaviour):
    """Reports a deadline before now once: the conductor moves it to now."""

    def run(self, t, frames, first=False):
        self.now = t
        self.calls = getattr(self, "calls", 0) + 1
        return [], {1: 1000, 2: 400, 3: NEVER}[self.calls]


def test_past_deadline():
    srv = Server(["--vtime=1"])
    d = Driver(ARGS.sock, {1: PastDeadline(1)})
    d.connect()
    log = d.run()
    check(srv.wait() == 0, "server exit")
    check([t for t, *_ in log] == [0, 1000, 1000], f"wakes {[t for t, *_ in log]}")
    check("moved to now" in srv.output(), "not logged")


class Staggered(Behaviour):
    """Sends every (1000 + 10 * (ID % 7)) us, first at ID % 13 * 100 us."""

    def start(self):
        self.set_timer("tx", (self.nid % 13) * 100)

    def on_timer(self, name):
        self.send(f"{self.nid}@{self.now}")
        self.set_timer("tx", self.now + 1000 + 10 * (self.nid % 7))


def test_many_nodes():
    ids = list(range(1, 501))
    edges = [(ids[i], ids[(i + 1) % len(ids)]) for i in range(len(ids))]
    srv = Server([f"--vtime={len(ids)}", "--vtime-end=0.02", LEGACY] + graph_args(edges))
    d = Driver(ARGS.sock, {n: Staggered(n) for n in ids})
    d.connect()
    log = d.run(timeout=60)
    check(srv.wait(60) == 0, "server exit")
    mlog, _ = Model(graph_of(edges), {n: Staggered(n) for n in ids}, end_us=20000).run()
    check(log == mlog, "differs from the model")


def test_loop_cap():
    nodes = {n: PingPong(n) for n in (1, 2)}
    srv = Server(["--vtime=2", LEGACY])
    d = Driver(ARGS.sock, nodes)
    d.connect()
    log = d.run(timeout=120)
    status = srv.wait(120)
    out = srv.output()
    check(status != 0, "loop cap: exit status 0")
    check("same-time loop" in out and "queue head" in out, "loop cap not logged")
    at1000 = sum(1 for t, *_ in log if t == 1000)
    check(at1000 == 100000, f"{at1000} wakes at 1000")
    check(d.quit == {1, 2}, "xq after the loop cap")


def test_sigint():
    nodes = {n: Forever(n) for n in (1, 2)}
    srv = Server(["--vtime=2"])
    d = Driver(ARGS.sock, nodes)

    def hook(drv, nid, t):
        if t == 50000 and nid == 1:
            srv.p.send_signal(signal.SIGINT)
    d.on_wake = hook
    d.connect()
    d.run()
    check(srv.wait() == 0, "server exit after SIGINT")
    check(d.quit == {1, 2}, f"xq after SIGINT: {d.quit}")
    check("interrupted" in srv.output(), "end reason")


def test_disconnect():
    nodes = {n: Forever(n) for n in (1, 2)}
    srv = Server(["--vtime=2"])
    d = Driver(ARGS.sock, nodes)
    d.on_wake = lambda drv, nid, t: "close" if (nid, t) == (2, 5000) else None
    d.connect()
    d.run()
    check(srv.wait() != 0, "server exit 0 after a node left")
    check(d.quit == {1}, f"xq: {d.quit}")
    check("node 2 disconnected" in srv.output(), "disconnection not logged")


def test_kill_cut_link():
    """--vtime-cut: node 2 no longer hears node 1 from 3 ms; --vtime-kill:
    node 2 stopped at 6 ms (its connection closed, no more wakes);
    --vtime-link: accepted on an existing link, refused on a missing one."""
    nodes = {n: Forever(n) for n in (1, 2, 3)}
    srv = Server(["--vtime=3", "--vtime-end=0.01", LEGACY, "--vtime-cut=1:2:0.003", "--vtime-kill=2:0.006",
                  "--vtime-link=1:3:0.002:-80"] + graph_args([(1, 2), (1, 3)]))
    d = Driver(ARGS.sock, nodes)
    d.connect()
    log = d.run()
    check(srv.wait() == 0, "server exit status")
    rx2 = [(t, src) for t, nid, frames, out in log if nid == 2 for src, p in frames]
    check(any(t < 3000 for t, s in rx2) and not any(t >= 3000 for t, s in rx2), f"node 2 frames: {rx2}")
    rx3 = [t for t, nid, frames, out in log if nid == 3 for src, p in frames]
    check(any(t >= 3000 for t in rx3), "node 3 lost its frames")
    check(2 in d.closed and not any(nid == 2 and t >= 6000 for t, nid, f, o in log), "node 2 not stopped at 6 ms")
    check(d.quit == {1, 3}, f"xq: {d.quit}")
    out = srv.output()
    check("node 2 stopped at 0.006000 s" in out and "link 1-3 at -80.0 dBm" in out, "events not logged")
    srv = Server(["--vtime=2", "--vtime-end=0.01", LEGACY, "--vtime-link=1:2:0.002:-80"] + graph_args([(1, 3)]))
    d = Driver(ARGS.sock, {n: Forever(n) for n in (1, 2)})
    d.connect()
    d.run()
    check(srv.wait() != 0 and "no link 1-2" in srv.output(), "a missing link accepted")


class Late(Forever):
    """A started instance: its first run is at its start (the welcome's
    start_us), and its timers count from there, as the simulation port's."""

    def start(self):
        self.set_timer("tx", self.now + 1000)


def test_start():
    """--vtime-start: a second instance of node 2 connects at the start and is
    welcomed at 5 ms with start_us 5 ms (protocol version 4), after the first
    one is stopped then; it runs first at 5 ms and its timers count from
    there."""
    nodes = {n: Forever(n) for n in (1, 2)}
    srv = Server(["--vtime=2", "--vtime-end=0.01", LEGACY, "--vtime-start=2:0.005"] + graph_args([(1, 2)]))
    d = Driver(ARGS.sock, nodes)
    d.connect()
    s2 = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s2.connect(ARGS.sock)
    s2.send(HELLO.pack(b"xh", 2, HELLO_VTIME, 0))
    welcomed = []

    def swap(drv, nid):
        if nid == 2 and not welcomed:
            welcomed.append(True)
            drv.socks[2] = s2
            drv.nodes[2] = Late(2)
            drv.closed.discard(2)
    d.on_close = swap
    log = d.run()
    check(srv.wait() == 0, "server exit status")
    w2 = [t for t, nid, f, o in log if nid == 2]
    check(welcomed and 0 in w2 and 5000 in w2 and 6000 in w2 and max(w2) >= 9000, f"node 2 wakes: {w2}")
    check(d.start == {1: 0, 2: 5000}, f"welcome start_us: {d.start}")
    check(d.quit == {1, 2}, f"xq: {d.quit}")
    out = srv.output()
    check("node 2 started at 0.005000 s" in out and "is before now" not in out, "start not logged, or a warning")


def test_power_up():
    """--vtime-power-up=2:0.0035: node 2 (its only instance) is welcomed at
    3.5 ms with start_us 3.5 ms; it does not run or hear before (node 1's
    frames at 1, 2 and 3 ms do not reach it), then runs and hears as usual."""
    nodes = {1: Forever(1), 2: Late(2)}
    srv = Server(["--vtime=2", "--vtime-end=0.006", LEGACY, "--vtime-power-up=2:0.0035"] + graph_args([(1, 2)]))
    d = Driver(ARGS.sock, nodes)
    d.connect()
    log = d.run()
    check(srv.wait() == 0, "server exit status")
    w2 = [(t, len(f)) for t, nid, f, o in log if nid == 2]
    check(w2 == [(3500, 0), (4000, 1), (4500, 0), (5000, 1), (5500, 0), (6000, 1)], f"node 2 wakes: {w2}")
    check(d.start == {1: 0, 2: 3500}, f"welcome start_us: {d.start}")
    check("node 2 started at 0.003500 s (powered up)" in srv.output(), "start not logged")


def raw_hello(nid, flags):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.connect(ARGS.sock)
    s.send(HELLO.pack(b"xh", nid, flags, 0))
    return s


def closed_by_server(s):
    s.settimeout(5)
    try:
        return s.recv(100) == b""
    except (socket.timeout, ConnectionResetError):
        return False


def test_vtime_refuses_wallclock_hello():
    srv = Server(["--vtime=1", "--vtime-end=0.01"])
    bad = raw_hello(9, 0)
    check(closed_by_server(bad), "wall-clock hello accepted in --vtime mode")
    Script.TABLE = {1: {0: ([], 1000)}}
    d = Driver(ARGS.sock, {1: Script(1)})
    d.connect()
    d.run()
    check(srv.wait() == 0, "server exit")
    check("refused" in srv.output(), "refusal not logged")


def test_wallclock():
    srv = Server(graph_args([(1, 2), (2, 3)]))
    bad = raw_hello(9, HELLO_VTIME)
    check(closed_by_server(bad), "virtual-time hello accepted in wall-clock mode")
    s1, s2, s3 = (raw_hello(n, 0) for n in (1, 2, 3))
    time.sleep(0.2)
    s2.send(frame_msg(b"abc", seq=7))
    for s in (s1, s3):
        s.settimeout(5)
        msg = s.recv(100)
        h = frame_unpack(msg)
        check(h["src"] == 2 and h["seq"] == 7 and msg[FRAME.size:] == b"abc", f"relay: {msg!r}")
    s1.send(frame_msg(b"z", seq=1, src=0))
    s2.settimeout(5)
    check(frame_unpack(s2.recv(100))["src"] == 1, "relay 1 -> 2")
    s3.setblocking(False)
    try:
        s3.recv(100)
        raise TestError("node 3 got a frame from node 1 (not neighbours)")
    except BlockingIOError:
        pass
    shm = "/dev/shm/wssimnodesinfos." + ARGS.sock.replace("/", "_")
    check(os.path.exists(shm), f"{shm} missing")
    srv.p.send_signal(signal.SIGTERM)
    check(srv.wait() == 0, "server exit")
    check("refused" in srv.output(), "refusal not logged")
    check(not os.path.exists(shm), f"{shm} left")
    for s in (s1, s2, s3, bad):
        s.close()


def run_drift(opts, seed="1"):
    nodes = {n: Periodic(n) for n in Periodic.PERIODS}
    srv = Server([f"--vtime={len(nodes)}", "--vtime-end=0.01", LEGACY, *opts] + graph_args(PERIODIC_EDGES),
                 {"WSSIM_SEED": seed})
    d = Driver(ARGS.sock, nodes)
    d.connect()
    d.run()
    check(srv.wait() == 0, "server exit")
    return d.drift


def test_drift():
    # No option: every node gets 0
    check(set(run_drift([]).values()) == {0}, "drift without --drift")
    # --drift for all, then a node list that overrides it (the last one wins)
    d = run_drift(["--drift=-12.5", "--drift=7,20:30"])
    check(d[7] == 30000 and d[20] == 30000, f"--drift node list: {d}")
    check(all(v == -12500 for n, v in d.items() if n not in (7, 20)), f"--drift all: {d}")
    # --drift-random: within the bound, reproducible, seed-dependent, and a
    # --drift naming a node takes precedence
    r1 = run_drift(["--drift-random=50", "--drift=10:1"])
    r2 = run_drift(["--drift-random=50", "--drift=10:1"])
    r3 = run_drift(["--drift-random=50", "--drift=10:1"], seed="2")
    check(r1 == r2, f"--drift-random not reproducible: {r1} {r2}")
    check(r1 != r3, "--drift-random: same draws for another seed")
    check(r1[10] == 1000, f"--drift over --drift-random: {r1}")
    check(all(-50000 <= v <= 50000 for v in r1.values()), f"--drift-random bound: {r1}")
    check(len(set(r1.values())) == len(r1), f"--drift-random: equal draws {r1}")


# --- Central medium (docs/MEDIUM.md §8 item 2) ------------------------------

NOISE_DBM = -153.930645 + 30 + 4
PAYLOAD = b"p" * 20             # 20 bytes: with the defaults, 36 bytes on air
AIRTIME = 36 * 8 * 1000000 // 50000     # 5760 us
SHR_US = 10 * 8 * 1000000 // 50000      # 1600 us
BITS = 8 * (2 + 20 + 4)


class Radio(Behaviour):
    """A radio for the central medium. ACTIONS: {time: [action, ...]} with
    ("tx", fields), ("sense", chan) and ("radio", state, chan). It listens on
    channel 0 from the start; a frame the medium delivers puts it in RECEIVE,
    a verdict back to LISTEN, as the real radio does."""

    def __init__(self, nid, actions=None, period=None):
        super().__init__(nid)
        self.actions = actions or {}
        self.period = period        # (first, every, count): ("tx", {}) at these times

    def times(self):
        t = set(self.actions)
        if self.period:
            first, every, count = self.period
            t |= {first + k * every for k in range(count)}
        return t

    def run(self, t, frames, first=False):
        self.now = t
        self.out = []
        if first:
            self.radio = (RADIO_LISTEN, 0)
        for fr in frames:
            if fr[0] == "xv":
                self.radio = (RADIO_LISTEN, self.radio[1])
            else:
                self.radio = (RADIO_RECEIVE, self.radio[1])
        acts = list(self.actions.get(t, []))
        if self.period and t in self.times() - set(self.actions):
            acts.append(("tx", {}))
        for a in acts:
            if a[0] == "tx":
                fields = dict(a[1])
                self.send(fields.pop("payload", PAYLOAD), **fields)
            elif a[0] == "sense":
                self.sense(a[1])
            elif a[0] == "radio":
                self.radio = (a[1], a[2])
        later = [x for x in self.times() if x > t]
        return self.out, min(later) if later else NEVER


def medium_run(radios, links, opts=(), seed="1", hook=None, setup=None):
    """links: [(a, b, rssi or None)]. Returns the driver, the server output
    and the --medium-log lines as tuples. setup(driver), if given, runs once
    the nodes are connected (e.g. to connect a later instance)."""
    mlog = os.path.join(ARGS.logdir, "medium.log")
    if os.path.exists(mlog):
        os.unlink(mlog)
    g = sum((["-g", f"{a},{b}" + ("" if r is None else f":{r}")] for a, b, r in links), [])
    srv = Server([f"--vtime={len(radios)}", "--medium=central", f"--medium-log={mlog}", *opts] + g,
                 {"WSSIM_SEED": seed})
    d = Driver(ARGS.sock, radios)
    d.on_wake = hook
    d.connect()
    if setup:
        setup(d)
    d.run(timeout=60)
    status = srv.wait(60)
    out = srv.output()
    check(status == 0, f"server exit {status}:\n{out[-2000:]}")
    check(all(f & WELCOME_MEDIUM for f in d.welcome_flags.values()), f"welcome flags {d.welcome_flags}")
    with open(mlog) as f:
        lines = [ln.split() for ln in f]
    rows = [(int(a[0]), int(a[1]), int(a[2]), int(a[3]), a[4], float(a[5]), float(a[6])) for a in lines]
    return d, out, rows


def got(radio, kind):
    return [(t, m) for t, k, m in radio.rx if k == kind]


def empty_wakes(d, nid):
    """Wakes of nid with no message (a stale verdict must not wake it): the
    radios here have no deadline of their own at those times."""
    acts = d.nodes[nid].times()
    return [t for t, n, f, o in d.log if n == nid and t and not f and t not in acts]


def test_medium_options():
    for args, msg in ((["--medium=central"], "needs --vtime"),
                      (["--vtime=1", "--medium=bogus"], "legacy or central"),
                      (["--vtime=1", "--medium=legacy", "--medium-log=/dev/null"], "need the central medium"),
                      (["--vtime=1", "--medium=legacy", "--medium-capture-db=3"], "need the central medium"),
                      (["--vtime=1", "--medium=central", "--medium-lock-snr=x"], "expected a number")):
        srv = Server(args)
        check(srv.wait() != 0 and msg in srv.output(), f"{args} accepted, or no \"{msg}\"")
    # The default with --vtime is the central medium: the welcome says so, and
    # the medium's tuning options need no --medium
    srv = Server(["--vtime=1", "--vtime-end=0.001", "--medium-capture-db=3"])
    d = Driver(ARGS.sock, {1: Script(1)})
    d.connect()
    d.run()
    check(srv.wait() == 0, "server exit (default medium)")
    check(d.welcome_flags == {1: WELCOME_MEDIUM} and "medium: central" in srv.output(),
          f"default welcome flags {d.welcome_flags}")
    # Legacy (explicit): no medium flag in the welcome, frames with no medium fields
    Script.TABLE = {1: {0: (["a"], NEVER)}, 2: {0: ([], NEVER)}}
    srv = Server(["--vtime=2", "--medium=legacy"] + graph_args([(1, 2)]))
    d = Driver(ARGS.sock, {n: Script(n) for n in (1, 2)})
    d.connect()
    d.run()
    check(srv.wait() == 0, "server exit")
    check(d.welcome_flags == {1: 0, 2: 0}, f"legacy welcome flags {d.welcome_flags}")


def test_medium_overlap():
    """Two overlapping frames at one receiver, from hidden terminals (1 and 3
    are not neighbours, both reach 2): 2 locks onto the first, the second is
    interference; the first is lost at equal power, kept 30 dB above. 3's CCA
    before it sends hears nothing."""
    for rssi3, ok, cause in ((-60, 0, 2), (-90, 1, 0)):
        r = {1: Radio(1, {1000: [("tx", {})]}), 2: Radio(2), 3: Radio(3, {1500: [("sense", 0), ("tx", {})]})}
        d, out, rows = medium_run(r, [(1, 2, -60), (3, 2, rssi3)])
        xx, xv = got(r[2], "xx"), got(r[2], "xv")
        check([(t, m["src"], m["frame_id"], m["flags"], m["rx_dbm"]) for t, m in xx] ==
              [(1000, 1, 1, FRAME_MEDIUM, -60.0)], f"frames at 2: {xx}")
        check(len(xv) == 1 and xv[0][0] == 1000 + AIRTIME and xv[0][1]["frame_id"] == 1, f"verdicts at 2: {xv}")
        v = xv[0][1]
        sinr = -60 - 10 * math.log10(10 ** (NOISE_DBM / 10) + 10 ** (rssi3 / 10))
        check(v["ok"] == ok and v["cause"] == cause and abs(v["sinr_db"] - sinr) < 0.01, f"verdict {v}, sinr {sinr}")
        check(abs(r[3].sensed[0][2] - NOISE_DBM) < 0.01, f"CCA of the hidden terminal: {r[3].sensed}")
        want = {(1000 + AIRTIME, 1, 2, 1, "ok" if ok else "interference"), (1500 + AIRTIME, 3, 2, 2, "busy")}
        check({x[:5] for x in rows} == want, f"medium log {rows}")
        check(not got(r[1], "xx") and not got(r[3], "xx"), "1 or 3 heard the other")


def test_medium_capture():
    """--medium-model=em4 (the MVP's rule, kept for comparison): a frame
    20 dB stronger captures the receiver during the first frame's whole SHR
    (1600 us), not after; one 3 dB stronger never does. Since EM5 the default
    model captures only in the measured windows (test_medium_calib_capture);
    this test now passes --medium-model=em4 to keep checking the old rule."""
    for delay, rssi3, captured in ((SHR_US - 1, -60, True), (SHR_US, -60, False), (500, -77, False)):
        r = {1: Radio(1, {1000: [("tx", {})]}), 2: Radio(2), 3: Radio(3, {1000 + delay: [("tx", {})]})}
        d, out, rows = medium_run(r, [(1, 2, -80), (3, 2, rssi3)], opts=["--medium-model=em4"])
        xx = [(t, m["frame_id"]) for t, m in got(r[2], "xx")]
        xv = [(t, m["frame_id"], m["ok"]) for t, m in got(r[2], "xv")]
        outcomes = {(x[3], x[4]) for x in rows}
        if captured:
            check(xx == [(1000, 1), (1000 + delay, 2)], f"capture: frames {xx}")
            check(xv == [(1000 + delay + AIRTIME, 2, 1)], f"capture: verdicts {xv}")
            check(outcomes == {(1, "captured"), (2, "ok")}, f"capture: log {rows}")
            check(not empty_wakes(d, 2), f"capture: empty wakes of 2 {empty_wakes(d, 2)}")
        else:
            check(xx == [(1000, 1)], f"no capture ({delay}, {rssi3}): frames {xx}")
            check(xv == [(1000 + AIRTIME, 1, 0)], f"no capture ({delay}, {rssi3}): verdicts {xv}")
            check(outcomes == {(1, "interference"), (2, "busy")}, f"no capture: log {rows}")


def test_medium_half_duplex():
    """2 sends while it receives 1's frame: its lock ends, no verdict."""
    r = {1: Radio(1, {1000: [("tx", {})]}), 2: Radio(2, {2000: [("tx", {})]})}
    d, out, rows = medium_run(r, [(1, 2, -60)])
    check(len(got(r[2], "xx")) == 1 and not got(r[2], "xv"), f"2 got {r[2].rx}")
    check((1000 + AIRTIME, 1, 2, 1, "tx_abort") in {x[:5] for x in rows}, f"log {rows}")
    check("tx_abort       1" in out, "summary")
    check(not empty_wakes(d, 2), f"half duplex: empty wakes of 2 {empty_wakes(d, 2)}")


def test_medium_not_listening():
    """1 sends on channel 3: 2 listens on 5, 3 is idle on 3, 4 listens on 3,
    5 listens on 3 but gets the frame below the noise floor."""
    r = {1: Radio(1, {1000: [("tx", {"chan": 3})]}), 2: Radio(2, {0: [("radio", RADIO_LISTEN, 5)]}),
         3: Radio(3, {0: [("radio", RADIO_IDLE, 3)]}), 4: Radio(4, {0: [("radio", RADIO_LISTEN, 3)]}),
         5: Radio(5, {0: [("radio", RADIO_LISTEN, 3)]})}
    d, out, rows = medium_run(r, [(1, 2, -60), (1, 3, -60), (1, 4, -60), (1, 5, -125)])
    check([n for n in r if got(r[n], "xx")] == [4], f"receivers {[(n, r[n].rx) for n in r]}")
    check({(x[2], x[4]) for x in rows} == {(2, "not_listening"), (3, "not_listening"), (4, "ok"),
                                           (5, "below_lock")}, f"log {rows}")
    check(got(r[4], "xx")[0][1]["chan"] == 3, "channel")


def test_medium_same_time():
    """A frame ends at t at 2 and another starts at t: 2 gets the verdict, then
    the new frame, in one wake; they do not overlap."""
    r = {1: Radio(1, {1000 + AIRTIME: [("tx", {})]}), 2: Radio(2), 3: Radio(3, {1000: [("tx", {})]})}
    d, out, rows = medium_run(r, [(1, 2, -60), (3, 2, -60)])
    wake = [f for t, n, f, o in d.log if n == 2 and t == 1000 + AIRTIME]
    check(wake == [(("xv", 1, 1, 0), (1, PAYLOAD))], f"2 at {1000 + AIRTIME}: {wake}")
    check({x[4] for x in rows} == {"ok"}, f"log {rows}")


def test_medium_cca():
    """CCA energy: the sender's power on its channel at a neighbour (whatever
    the neighbour's MAC knows of it), 10 dB less on the next channel (EM5:
    adjacent-channel leakage; before EM5 the noise floor there), the noise
    floor two channels away, after the frame, at a node that does not hear
    the sender, and at the sender. --medium-model=em4: the noise floor on
    the next channel."""
    for opts, adj in (([], -80), (["--medium-model=em4"], None)):
        r = {1: Radio(1, {1000: [("tx", {"chan": 3})], 2000: [("sense", 3)]}),
             2: Radio(2, {0: [("radio", RADIO_IDLE, 3)], 2000: [("sense", 3), ("sense", 4), ("sense", 5)],
                          7000: [("sense", 3)]}),
             3: Radio(3, {2000: [("sense", 3)]})}
        d, out, rows = medium_run(r, [(1, 2, -70), (2, 3, -50)], opts=opts)
        e = 10 * math.log10(10 ** (NOISE_DBM / 10) + 10 ** (-70 / 10))
        e4 = NOISE_DBM if adj is None else 10 * math.log10(10 ** (NOISE_DBM / 10) + 10 ** (adj / 10))
        s2 = [dbm for t, c, dbm in r[2].sensed]
        check(len(s2) == 4 and abs(s2[0] - e) < 0.01 and abs(s2[1] - e4) < 0.01 and abs(s2[2] - NOISE_DBM) < 0.01
              and abs(s2[3] - NOISE_DBM) < 0.01, f"{opts}: CCA at 2: {r[2].sensed}")
        check(abs(r[3].sensed[0][2] - NOISE_DBM) < 0.01 and abs(r[1].sensed[0][2] - NOISE_DBM) < 0.01,
              f"{opts}: CCA at 3 {r[3].sensed}, at 1 {r[1].sensed}")
        n_on = 1 if adj is None else 2
        check(f"6 CCA queries, {n_on} with a frame on air" in out, f"{opts}: CCA summary")


def test_medium_tx_ref():
    """--medium-tx-ref=0: a frame sent at +10 dBm arrives 10 dB above the
    link's value; a link without a value stays perfect (0 dBm)."""
    r = {1: Radio(1, {1000: [("tx", {"tx_dbm": 10})]}), 2: Radio(2), 3: Radio(3)}
    d, out, rows = medium_run(r, [(1, 2, -60), (1, 3, None)], opts=["--medium-tx-ref=0"])
    check(got(r[2], "xx")[0][1]["rx_dbm"] == -50 and got(r[3], "xx")[0][1]["rx_dbm"] == 0,
          f"{r[2].rx} {r[3].rx}")


def test_medium_left():
    """A receiver stopped (--vtime-kill) or whose radio is off by the next
    decision leaves its frame: no verdict, outcome "left". The kill writes
    DRVSTATE_OFF and SIM_RADIO_OFF in the node's 4-byte entry."""
    seen = {}

    def hook(drv, nid, t):
        if nid == 1 and t == 4000:
            with open(shm_path(ARGS.sock), "rb") as f:
                seen["shm"] = f.read(4 * 6)
    r = {1: Radio(1, {1000: [("tx", {})], 4000: []}), 2: Radio(2), 3: Radio(3, {3000: [("tx", {})]}),
         4: Radio(4, {2000: [("radio", RADIO_OFF, 0)]}), 5: Radio(5, {0: [("radio", RADIO_IDLE, 7)]})}
    d, out, rows = medium_run(r, [(1, 2, -60), (1, 4, -60), (3, 4, -60)], opts=["--vtime-kill=2:0.002"], hook=hook)
    check(not got(r[2], "xv") and not got(r[4], "xv"), "a verdict after leaving")
    check({(x[2], x[3], x[4]) for x in rows} == {(2, 1, "left"), (4, 1, "left"), (4, 2, "not_listening")},
          f"log {rows}")
    check(not empty_wakes(d, 4), f"left: empty wakes of 4 {empty_wakes(d, 4)}")
    shm = seen.get("shm", b"")
    check(shm[8:12] == bytes([7, 0, 0, RADIO_OFF]) and shm[20:24] == bytes([2, 7, 0, RADIO_IDLE]),
          f"shared memory {shm.hex()}")


def curve_run(seed):
    rssis = {2: -108, 3: -110, 4: -112}
    n = 1000
    r = {1: Radio(1, period=(1000, 6000, n))}
    r.update({k: Radio(k) for k in rssis})
    d, out, rows = medium_run(r, [(1, k, v) for k, v in rssis.items()], seed=seed)
    return rssis, n, r, rows


def test_medium_curve():
    """Without interference, the loss rate at three RSSI values is today's
    curve (the radio's formula), and the verdict's SINR is RSSI - noise."""
    rssis, n, r, rows = curve_run("1")
    for k, rssi in rssis.items():
        ber = min(0.5, 0.5 * math.exp(0.5 * (NOISE_DBM - rssi)))
        fer = 1 - (1 - ber) ** BITS
        xv = [m for t, m in got(r[k], "xv")]
        check(len(xv) == n, f"node {k}: {len(xv)} verdicts")
        lost = sum(1 for m in xv if not m["ok"])
        sigma = math.sqrt(n * fer * (1 - fer))
        check(abs(lost - n * fer) < 4.5 * sigma, f"RSSI {rssi}: {lost} lost of {n}, expected {n * fer:.0f}")
        check(all(m["cause"] in (0, 1) and abs(m["sinr_db"] - (rssi - NOISE_DBM)) < 0.01 for m in xv),
              f"node {k}: causes or SINR")
        print(f"     RSSI {rssi} dBm: {lost}/{n} lost, curve {n * fer:.1f} (sigma {sigma:.1f})")


def test_medium_determinism():
    rows = [curve_run(s)[3] for s in ("5", "5", "6")]
    check(rows[0] == rows[1], "same seed, different medium logs")
    check(rows[0] != rows[2], "another seed, same medium log")


def sinr_db(p, *intf):
    return p - 10 * math.log10(10 ** (NOISE_DBM / 10) + sum(10 ** (i / 10) for i in intf))


def p_success(segments):
    """MEDIUM.md §2, --medium-model=em4: segments [(share of the frame, SINR
    dB)] -> P_success."""
    return math.prod((1 - min(0.5, 0.5 * math.exp(-0.5 * s))) ** (BITS * share) for share, s in segments)


# Measured interference curves (MEDIUM.md §2.1; firmware repo
# docs/COCHANNEL_RESULTS.md §4.1, §5): x0, b per profile
INT_CURVE = {50000: (-2.40, 0.90), 150000: (-2.85, 0.90)}


def ber_fg25(p, intf, rate=50000):
    """The default model's bit error rate: the larger of the noise curve of
    the SNR and the interference curve of the SIR."""
    x0, b = INT_CURVE[rate]
    ber = 0.5 * math.exp(-0.5 * (p - NOISE_DBM))
    if intf:
        sir = p - 10 * math.log10(sum(10 ** (i / 10) for i in intf))
        ber = max(ber, 0.5 * math.exp(-(sir - x0) / b))
    return min(0.5, ber)


def p_success_fg25(segments, bits=BITS, rate=50000):
    """segments [(share of the counted bits, P dBm, [interferers dBm])]."""
    return math.prod((1 - ber_fg25(p, i, rate)) ** (bits * share) for share, p, i in segments)


def test_medium_segments():
    """The counted bits are shared over the segments (§2): an interferer
    covers a quarter of the frame, at the end (it starts after the lock) or
    at the start (it started before the frame, while the receiver was idle),
    at a SINR where that quarter loses about half of the frames. The loss
    rate matches P_success of the formula, far from the whole frame
    interfered. 2 locks every frame and hears every interferer (busy, or
    not_listening when it was idle).

    Two models: --medium-model=em4 (the interferer 7 dB below, the noise
    curve of the SINR over the whole airtime, as before EM5) and the default
    (EM5): the interferer 0.9 dB below, the measured curve, the counted bits
    over the frame after its SHR, so the interferer covers a quarter of the
    airtime at the start but only (quarter - SHR) of the bits after it."""
    for model in ("em4", "fg25"):
        test_medium_segments_model(model)


def test_medium_segments_model(model):
    n, period, p_a = 400, 12000, -80
    quarter = AIRTIME // 4
    if model == "em4":
        p_i = -87
        want_loss = 1 - p_success([(0.75, sinr_db(p_a)), (0.25, sinr_db(p_a, p_i))])
        want_start = want_loss
        whole = 1 - p_success([(1, sinr_db(p_a, p_i))])
        opts = ["--medium-model=em4"]
    else:
        p_i = -80.9
        data = AIRTIME - SHR_US
        want_loss = 1 - p_success_fg25([(1 - quarter / data, p_a, []), (quarter / data, p_a, [p_i])])
        # Here the quarter (1440 us) is shorter than the SHR (1600 us): no
        # counted bit is interfered, nothing is lost
        part = max(0, quarter - SHR_US) / data
        want_start = 1 - p_success_fg25([(1 - part, p_a, []), (part, p_a, [p_i])])
        whole = 1 - p_success_fg25([(1, p_a, [p_i])])
        opts = []
    for where in ("end", "start"):
        want = want_loss if where == "end" else want_start
        sigma = math.sqrt(n * want * (1 - want))
        first = 10000
        if where == "end":
            off3, acts2 = AIRTIME - quarter, {}
        else:
            # 2 idle when 3's frame starts, listening again before 1's
            off3 = quarter - AIRTIME
            acts2 = {}
            for k in range(n):
                t = first + k * period
                acts2[t + off3 - 1] = [("radio", RADIO_IDLE, 0)]
                acts2[t - 1] = [("radio", RADIO_LISTEN, 0)]
        r = {1: Radio(1, period=(first, period, n)), 2: Radio(2, acts2), 3: Radio(3, period=(first + off3, period, n))}
        d, out, rows = medium_run(r, [(1, 2, p_a), (3, 2, p_i)], opts=opts)
        xv = [m for t, m in got(r[2], "xv")]
        check(len(xv) == n and all(m["frame_id"] % 2 == (1 if where == "end" else 0) for m in xv),
              f"{model} {where}: {len(xv)} verdicts, or not for 1's frames")
        lost = sum(1 for m in xv if not m["ok"])
        check(all(m["ok"] or m["cause"] == 2 for m in xv), f"{model} {where}: a loss not caused by interference")
        # The lowest SINR over the counted bits (fg25 start: the interferer
        # ended before them)
        smin = sinr_db(p_a) if model == "fg25" and where == "start" else sinr_db(p_a, p_i)
        check(all(abs(m["sinr_db"] - smin) < 0.01 for m in xv), f"{model} {where}: segment SINR")
        check(abs(lost - n * want) < max(4 * sigma, 0.5), f"{model} {where}: {lost} lost of {n}, formula {n * want:.0f}")
        heard3 = {x[4] for x in rows if x[1] == 3}
        check(heard3 == ({"busy"} if where == "end" else {"not_listening"}),
              f"{model} {where}: 3's frames at 2: {heard3}")
        print(f"     {model}, interferer at the {where}: {lost}/{n} lost, formula {n * want:.1f} "
              f"(sigma {sigma:.1f}), whole frame interfered {n * whole:.0f}")


def test_medium_lock_refused():
    """2 is idle when 3's frame starts, then listens: 1's frame, 50 dB above
    the noise but 10 dB below 3's frame on air, is not locked (below_lock,
    SINR -10 dB from I(t)); with 3's frame 20 dB below 1's, it is."""
    for p3, locked in ((-60, False), (-90, True)):
        r = {1: Radio(1, {2000: [("tx", {})]}), 2: Radio(2, {0: [("radio", RADIO_IDLE, 0)],
                                                         1500: [("radio", RADIO_LISTEN, 0)]}),
             3: Radio(3, {1000: [("tx", {})]})}
        d, out, rows = medium_run(r, [(1, 2, -70), (3, 2, p3)])
        log = {(x[1], x[4]): x[6] for x in rows}
        check(log.get((3, "not_listening")) is not None, f"{p3}: 3's frame at 2: {rows}")
        xx = [m["src"] for t, m in got(r[2], "xx")]
        if locked:
            check(xx == [1] and abs(log.get((1, "ok"), 99) - sinr_db(-70, p3)) < 0.01, f"{p3}: {rows}, {xx}")
        else:
            check(not xx and abs(log.get((1, "below_lock"), 99) - sinr_db(-70, p3)) < 0.01, f"{p3}: {rows}, {xx}")


def test_medium_other_channel():
    """A frame two channels away, starting during the lock or before it,
    does not interfere: the verdict's SINR is the noise SNR, and the other
    frame is not_listening at 2 (its channel is not 2's). Before EM5 the
    test used the next channel (1), which now interferes 10 dB weaker
    (test_medium_calib_adjacent); with --medium-model=em4 it still does not."""
    for t3, chan, opts in ((2000, 2, []), (500, 2, []), (2000, 1, ["--medium-model=em4"]),
                           (500, 1, ["--medium-model=em4"])):
        r = {1: Radio(1, {1000: [("tx", {})]}), 2: Radio(2), 3: Radio(3, {t3: [("tx", {"chan": chan})]})}
        d, out, rows = medium_run(r, [(1, 2, -100), (3, 2, -50)], opts=opts)
        xv = [m for t, m in got(r[2], "xv")]
        check(len(xv) == 1 and abs(xv[0]["sinr_db"] - sinr_db(-100)) < 0.01, f"t3 {t3}: verdicts {xv}")
        check({(x[1], x[4]) for x in rows} - {(1, "ok"), (1, "noise")} == {(3, "not_listening")}, f"t3 {t3}: {rows}")


def test_medium_cut_drop():
    """A frame that 2 does not hear (--vtime-cut 3 -> 2, or --vtime-drop of
    its sequence number to 2) is neither interference to 2's frame nor
    energy in 2's CCA, and not in the log for 2; node 4 still hears it. The
    control without either: 3's frame, 20 dB stronger, destroys 1's and is
    in 2's CCA energy."""
    t3 = 1000 + SHR_US + 100
    for opt in ("", "--vtime-cut=3:2:0.0005", f"--vtime-drop=3:2:{PAYLOAD[2]}:0:1"):
        r = {1: Radio(1, {1000: [("tx", {})]}), 2: Radio(2, {t3 + 100: [("sense", 0)]}),
             3: Radio(3, {t3: [("tx", {})]}), 4: Radio(4)}
        d, out, rows = medium_run(r, [(1, 2, -80), (3, 2, -60), (3, 4, -60)], opts=[opt] if opt else [])
        xv = [m for t, m in got(r[2], "xv")]
        sensed = r[2].sensed[0][2]
        to2 = {(x[1], x[4]) for x in rows if x[2] == 2}
        check(got(r[4], "xx") and got(r[4], "xx")[0][1]["src"] == 3, f"{opt}: 4 did not hear 3")
        if opt:
            check(len(xv) == 1 and xv[0]["ok"] and abs(xv[0]["sinr_db"] - sinr_db(-80)) < 0.01, f"{opt}: {xv}")
            check(abs(sensed - 10 * math.log10(10 ** (NOISE_DBM / 10) + 10 ** (-80 / 10))) < 0.01,
                  f"{opt}: CCA at 2 {sensed}")
            check(to2 == {(1, "ok")}, f"{opt}: log for 2 {to2}")
        else:
            check(len(xv) == 1 and not xv[0]["ok"] and xv[0]["cause"] == 2, f"control: {xv}")
            check(abs(sensed - 10 * math.log10(10 ** (NOISE_DBM / 10) + 10 ** (-80 / 10) + 10 ** (-60 / 10))) < 0.01,
                  f"control: CCA at 2 {sensed}")
            check(to2 == {(1, "interference"), (3, "busy")}, f"control: log for 2 {to2}")


def test_medium_capture_margin():
    """Capture margin 3 dB (default since EM5, measured; 6 dB before, and
    with --medium-model=em4): a frame exactly at the margin, in the early
    capture window (200 us after the first frame's start, 50 kbit/s: mode
    1a's window is 20 bits, 400 us), captures; 0.01 dB less does not. With
    --medium-capture-db=10, 6 dB no longer captures and 10 dB does;
    --medium-capture-db=1a:4 sets the 50 kbit/s profile only. Before EM5 the
    second frame started 500 us in (inside the old whole-SHR window; now in
    the window where a stronger frame breaks the search, test_medium_calib_capture).
    The captured frame stays on air: it interferes with the capturing one."""
    for p3, opts, captured in ((-77, [], True), (-77.01, [], False),
                               (-74, ["--medium-model=em4"], True), (-74.01, ["--medium-model=em4"], False),
                               (-74, ["--medium-capture-db=10"], False), (-70, ["--medium-capture-db=10"], True),
                               (-76.01, ["--medium-capture-db=1a:4"], False),
                               (-76, ["--medium-capture-db=1a:4"], True),
                               (-77, ["--medium-capture-db=3:10"], True)):
        r = {1: Radio(1, {1000: [("tx", {})]}), 2: Radio(2), 3: Radio(3, {1200: [("tx", {})]})}
        d, out, rows = medium_run(r, [(1, 2, -80), (3, 2, p3)], opts=opts)
        outcomes = {(x[1], x[4]) for x in rows}
        if captured:
            check(outcomes in ({(1, "captured"), (3, "ok")}, {(1, "captured"), (3, "interference")}) and
                  abs(rows[-1][6] - sinr_db(p3, -80)) < 0.01, f"{p3} {opts}: {rows}")
        else:
            check(outcomes == {(1, "interference"), (3, "busy")}, f"{p3} {opts}: {rows}")


def test_medium_restart():
    """A receiver stopped while it holds a frame (left), restarted
    (--vtime-start) before that frame's end: the new instance gets no verdict
    and no wake at the old frame's end, and receives the next frame as
    usual. Also with the new instance started after the frame's end."""
    for opts, t_new in ((["--vtime-start=2:0.003"], 3000),
                        (["--vtime-kill=2:0.002", "--vtime-start=2:0.004"], 4000),
                        (["--vtime-kill=2:0.002", "--vtime-start=2:0.008"], 8000)):
        old, new2 = Radio(2), Radio(2)
        r = {1: Radio(1, {1000: [("tx", {})], 10000: [("tx", {})]}), 2: old}

        def setup(drv):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            s.connect(ARGS.sock)
            s.send(HELLO.pack(b"xh", 2, HELLO_VTIME, 0))

            def swap(drv2, nid):
                if nid == 2 and drv2.nodes[2] is not new2:
                    drv2.socks[2] = s
                    drv2.nodes[2] = new2
                    drv2.closed.discard(2)
            drv.on_close = swap
        d, out, rows = medium_run(r, [(1, 2, -60)], opts=opts, setup=setup)
        check([(t, m["frame_id"]) for t, m in got(old, "xx")] == [(1000, 1)] and not got(old, "xv"),
              f"{opts}: old instance {old.rx}")
        check([(t, k, m["frame_id"]) for t, k, m in new2.rx] == [(10000, "xx", 2), (10000 + AIRTIME, "xv", 2)],
              f"{opts}: new instance {new2.rx}")
        w2 = [t for t, n, f, o in d.log if n == 2 and t >= t_new]
        check(w2 == [t_new, 10000, 10000 + AIRTIME], f"{opts}: wakes of the new instance {w2}")
        check({(x[3], x[4]) for x in rows} == {(1, "left"), (2, "ok")}, f"{opts}: log {rows}")
        check(d.start.get(2) == t_new, f"{opts}: start {d.start}")


# EM5: the model calibrated on the FG25 kits (MEDIUM.md §2.1; firmware repo
# docs/COCHANNEL_RESULTS.md). Mode 3: 150 kbit/s, 12-byte preamble and 2-byte
# SFD as the FG25's PHY, 45-byte PSDU: 8 * (2 + 45 + 4) = 408 counted bits,
# the kits' 50-byte frame.
M3 = {"rate_bps": 150000, "shr_len": 14}
M3_SHR = 14 * 8 * 1000000 // 150000             # 746 us
M3_PRE = 12 * 8 * 1000000 // 150000             # 640 us
M3_PSDU = b"q" * 45
M3_AIR = (14 + 2 + 45 + 4) * 8 * 1000000 // 150000   # 3466 us


def received_share(rssi_a, rssi_i, n, wanted, interferer, period, opts=(), seed="1"):
    """Node 1 sends n frames (fields wanted) to 2 at rssi_a; node 3 sends
    one (fields interferer, with its "at" offset from 1's start) at rssi_i
    with each. Returns the share of 1's frames received and the verdicts."""
    first = 10000
    at = interferer.pop("at")
    r = {1: Radio(1, {first + k * period: [("tx", dict(wanted))] for k in range(n)}), 2: Radio(2),
         3: Radio(3, {first + at + k * period: [("tx", dict(interferer))] for k in range(n)})}
    d, out, rows = medium_run(r, [(1, 2, rssi_a), (3, 2, rssi_i)], opts=opts, seed=seed)
    xv = [m for t, m in got(r[2], "xv") if m["frame_id"] % 2 == 1]
    check(len(xv) == n, f"{len(xv)} verdicts for 1's frames, not {n}")
    return sum(m["ok"] for m in xv) / n, xv


def test_medium_calib_sir():
    """The measured locked-frame points (COCHANNEL_RESULTS.md §4.1, §5): a
    frame already locked, an interferer from just after its SHR to its end.
    Mode 3, 50-byte frame: received 0 of about 200 at +0.4 dB, 50 % at
    +2.65 dB, 90 % at +3.8 dB, 99 % at +5.0 dB. Mode 1a (interferer from
    1800 us): 50 % at +2.9 dB, 99 % at +5.3 dB. The model is checked against
    these within the measurement's SIR accuracy (about 1 dB): at the 50 %
    points between 30 and 75 %, at the 99 % points at least 93 % (the fit is
    1-2 dB conservative there, §4.1). Each point's rate also equals the
    model's formula. The EM4 model, for comparison, loses nearly all frames
    at +5 dB (it needed about 20 dB)."""
    n = 200
    m3_int = dict(M3, payload=b"i" * 120, at=M3_SHR + 153)
    m3_bits = (M3_AIR - M3_SHR - 153) / (M3_AIR - M3_SHR)
    # Mode 1a: the kits' 50-byte frame, 46-byte PSDU, 416 counted bits
    a1 = {"payload": b"q" * 46}
    a1_air = (10 + 2 + 46 + 4) * 8 * 1000000 // 50000
    a1_int = {"payload": b"i" * 60, "at": 1800}
    a1_bits = (a1_air - 1800) / (a1_air - SHR_US)
    cases = [   # (name, wanted, interferer, share of the bits interfered, rate, SIR, low, high, opts)
        ("mode 3", dict(M3, payload=M3_PSDU), m3_int, m3_bits, 150000, 0.4, 0, 0.05, []),
        ("mode 3", dict(M3, payload=M3_PSDU), m3_int, m3_bits, 150000, 2.65, 0.30, 0.75, []),
        ("mode 3", dict(M3, payload=M3_PSDU), m3_int, m3_bits, 150000, 3.8, 0.70, 0.97, []),
        ("mode 3", dict(M3, payload=M3_PSDU), m3_int, m3_bits, 150000, 5.0, 0.93, 1, []),
        ("mode 3", dict(M3, payload=M3_PSDU), m3_int, m3_bits, 150000, 8.0, 0.99, 1, []),
        ("mode 3 em4", dict(M3, payload=M3_PSDU), m3_int, m3_bits, 150000, 5.0, 0, 0.05, ["--medium-model=em4"]),
        ("mode 1a", a1, a1_int, a1_bits, 50000, 2.9, 0.30, 0.75, []),
        ("mode 1a", a1, a1_int, a1_bits, 50000, 5.3, 0.93, 1, []),
    ]
    for name, wanted, intf, share, rate, sir, low, high, opts in cases:
        p_a = -60
        rx, xv = received_share(p_a, p_a - sir, n, wanted, dict(intf), 30000, opts)
        bits = 8 * (2 + len(wanted.get("payload", PAYLOAD)) + 4)
        if opts:
            # em4 counts the bits over the whole airtime: the share after the interferer's start
            air = M3_AIR if rate == 150000 else a1_air
            at = intf["at"]
            want = (1 - min(0.5, 0.5 * math.exp(-0.5 * sinr_db(p_a, p_a - sir)))) ** (bits * (air - at) / air)
        else:
            want = p_success_fg25([(1 - share, p_a, []), (share, p_a, [p_a - sir])], bits, rate)
        sigma = math.sqrt(n * want * (1 - want))
        check(low <= rx <= high, f"{name} at SIR {sir} dB: {rx:.2f} received, measured band {low}-{high}")
        check(abs(rx * n - want * n) < max(4 * sigma, 0.5), f"{name} at SIR {sir} dB: {rx:.2f}, formula {want:.2f}")
        print(f"     {name}, SIR {sir:+.2f} dB: {rx * 100:.0f} % received (formula {want * 100:.0f} %)")


def capture_case(offset, p_b, wanted=None, opts=(), late=None):
    """Node 1's frame at 2 from 1000 at -70 dBm, node 3's from 1000 +
    offset at p_b (same fields). late: node 4 sends a frame at that time.
    Returns 2's frames, its verdicts and the medium log."""
    w = dict(wanted or {})
    r = {1: Radio(1, {1000: [("tx", dict(w))]}), 2: Radio(2), 3: Radio(3, {1000 + offset: [("tx", dict(w))]})}
    links = [(1, 2, -70), (3, 2, p_b)]
    if late is not None:
        r[4] = Radio(4, {late: [("tx", dict(w))]})
        links.append((4, 2, -70))
    d, out, rows = medium_run(r, links, opts=opts)
    xx = [(t, m["src"]) for t, m in got(r[2], "xx")]
    xv = [(t, m["frame_id"], m["ok"]) for t, m in got(r[2], "xv")]
    return xx, xv, {(x[1], x[4]) for x in rows}, d


def test_medium_calib_capture():
    """Capture windows (COCHANNEL_RESULTS.md §4.2, §5, §6 item 3). A frame
    from 3, 10 dB stronger than the one 2 receives from 1, starting
    `offset` after it:
    - mode 3 (preamble 640 us, SHR 746 us; early window 37 bits = 246 us):
      captured at 0-245 us (measured: 0-200 us 100 %, 250 us 53-66 %), and
      during the SFD, 640-745 us (measured 650-700 us 97-100 %); between
      them both are lost (measured 300-550 us: 0 %): 2 gets 1's failed
      verdict at once and not 3's frame; from the SHR's end no capture
      (measured from 800 us: 0 %), 1's frame lost by interference at its end;
    - mode 1a (preamble 1280 us, SHR 1600 us; early window 20 bits =
      400 us): captured up to 399 us, both lost from 400 us to 1599 us,
      SFD included (no late window measured), no capture after;
    - the margin: 3 dB stronger needed (2 dB in the early window: no
      capture, both lost by interference); 2.5 dB breaks the search (2 dB
      does not: 1's frame then ends with its verdict); a weaker second frame
      in the early window leaves the first (received);
    - after a broken search the receiver locks the next frame;
    - --medium-model=em4: captured anywhere in the SHR, as before EM5."""
    m3 = dict(M3, payload=M3_PSDU)
    air = {"m3": M3_AIR, "1a": AIRTIME}
    cases = [   # (PHY, offset, P of 3's frame, opts, expected)
        ("m3", 0, -60, [], "captured"), ("m3", 100, -60, [], "captured"), ("m3", 245, -60, [], "captured"),
        ("m3", 246, -60, [], "broken"), ("m3", 400, -60, [], "broken"), ("m3", 639, -60, [], "broken"),
        ("m3", 640, -60, [], "captured"), ("m3", 700, -60, [], "captured"), ("m3", 745, -60, [], "captured"),
        ("m3", 746, -60, [], "none"), ("m3", 2000, -60, [], "none"),
        ("m3", 100, -67, [], "captured"), ("m3", 100, -68, [], "none"),
        ("m3", 400, -67.5, [], "broken"), ("m3", 400, -68, [], "none"),
        ("m3", 100, -78, [], "kept"), ("m3", 400, -78, [], "kept"),
        ("m3", 400, -60, ["--medium-model=em4"], "captured"), ("m3", 400, -65, ["--medium-model=em4"], "none"),
        ("m3", 400, -60, ["--medium-break-db=off"], "none"),
        ("m3", 700, -60, ["--medium-capture-sfd=3:off"], "broken"),
        ("m3", 400, -60, ["--medium-capture-early-bits=3:96"], "captured"),
        ("1a", 0, -60, [], "captured"), ("1a", 399, -60, [], "captured"), ("1a", 400, -60, [], "broken"),
        ("1a", 1400, -60, [], "broken"), ("1a", 1600, -60, [], "none"),
    ]
    for phy, offset, p_b, opts, want in cases:
        w = m3 if phy == "m3" else {}
        xx, xv, log, d = capture_case(offset, p_b, w, opts)
        tag = f"{phy} +{offset} us, {p_b} dBm {opts}"
        b_end = 1000 + offset + air[phy]
        if want == "captured":
            check(xx == [(1000, 1), (1000 + offset, 3)] and [(t, f) for t, f, ok in xv] == [(b_end, 2)] and
                  log in ({(1, "captured"), (3, "ok")}, {(1, "captured"), (3, "interference")}),
                  f"{tag}: captured expected: {xx} {xv} {log}")
        elif want == "broken":
            check(xx == [(1000, 1)] and xv == [(1000 + offset, 1, 0)] and log == {(1, "interference"), (3, "busy")},
                  f"{tag}: both lost at once expected: {xx} {xv} {log}")
            check(not empty_wakes(d, 2), f"{tag}: empty wakes of 2 {empty_wakes(d, 2)}")
        elif want == "none":
            check(xx == [(1000, 1)] and xv == [(1000 + air[phy], 1, 0)] and log == {(1, "interference"), (3, "busy")},
                  f"{tag}: no capture expected: {xx} {xv} {log}")
        else:   # kept: the first frame, 8 dB stronger, is received
            check(xx == [(1000, 1)] and xv == [(1000 + air[phy], 1, 1)] and log == {(1, "ok"), (3, "busy")},
                  f"{tag}: first frame kept expected: {xx} {xv} {log}")
    # After a broken search, 2 listens again: 4's frame, after 3's, is received
    late = 1000 + 400 + M3_AIR + 10
    xx, xv, log, d = capture_case(400, -60, m3, late=late)
    check(xx == [(1000, 1), (late, 4)] and xv == [(1400, 1, 0), (late + M3_AIR, 3, 1)],
          f"after a break: {xx} {xv} {log}")


def test_medium_calib_adjacent():
    """Adjacent channel (COCHANNEL_RESULTS.md §4.4): a frame on the next
    channel interferes as a co-channel frame 10 dB weaker, two channels
    away not at all; also an interferer that started before the lock (it
    also counts against the lock threshold: 10 dB above 1's frame on the
    next channel, 1's frame is not locked). 1's frame on channel 0 at
    -60 dBm; 3's frame from 2000 us (or from 500 us, before 1's) on channel
    `chan` at `p3`. --medium-adjacent-db sets it (off: no effect; 1a:30 for
    the 50 kbit/s profile)."""
    r = {1: Radio(1, {1000: [("tx", {})]}), 2: Radio(2), 3: Radio(3, {500: [("tx", {"chan": 1})]})}
    d, out, rows = medium_run(r, [(1, 2, -60), (3, 2, -45)])
    log = {(x[1], x[4]): x[6] for x in rows}
    check(not got(r[2], "xx") and abs(log.get((1, "below_lock"), 99) - sinr_db(-60, -55)) < 0.01,
          f"adjacent before the lock: {rows}")
    cases = [   # (chan, p3, t3, opts, ok, interferer power in the verdict's SINR)
        (1, -45, 2000, [], 0, -55),
        (1, -40, 2000, [], 0, -50), (1, -58, 500, [], 1, -68), (1, -62, 2000, [], 1, -72),
        (2, -30, 2000, [], 1, None), (1, -45, 2000, ["--medium-adjacent-db=off"], 1, None),
        (1, -45, 2000, ["--medium-adjacent-db=1a:30"], 1, -75), (1, -45, 2000, ["--medium-adjacent-db=3:30"], 0, -55),
    ]
    for chan, p3, t3, opts, ok, ieff in cases:
        r = {1: Radio(1, {1000: [("tx", {})]}), 2: Radio(2), 3: Radio(3, {t3: [("tx", {"chan": chan})]})}
        d, out, rows = medium_run(r, [(1, 2, -60), (3, 2, p3)], opts=opts)
        xv = [m for t, m in got(r[2], "xv")]
        want_sinr = sinr_db(-60) if ieff is None else sinr_db(-60, ieff)
        check(len(xv) == 1 and xv[0]["ok"] == ok and abs(xv[0]["sinr_db"] - want_sinr) < 0.01,
              f"chan {chan}, {p3} dBm at {t3} {opts}: {xv}, SINR {want_sinr:.2f} expected")
        check((3, "not_listening") in {(x[1], x[4]) for x in rows}, f"chan {chan}: log {rows}")


def test_medium_calib_options():
    """The calibrated model's options: errors, and the profiles printed at
    the start (the measured defaults; em4; a per-PHY override)."""
    for args, msg in ((["--vtime=1", "--medium-model=x"], "fg25 or em4"),
                      (["--vtime=1", "--medium=legacy", "--medium-model=em4"], "need the central medium"),
                      (["--vtime=1", "--medium-int-curve=3:1"], "X0,B"),
                      (["--vtime=1", "--medium-int-curve=3:1,0"], "above 0"),
                      (["--vtime=1", "--medium-capture-early-bits=2a:5"], "expected [1a|1b|3|other:]VALUE"),
                      (["--vtime=1", "--medium-capture-early-bits=3:1.5"], "whole number"),
                      (["--vtime=1", "--medium-capture-sfd=yes"], "on|off"),
                      (["--vtime=1", "--medium-break-db=abc"], "expected a number"),
                      (["--vtime=1", "--medium-adjacent-db=3:"], "expected a number")):
        srv = Server(args)
        check(srv.wait() != 0 and msg in srv.output(), f"{args} accepted, or no \"{msg}\": {srv.output()[-300:]}")
    for opts, lines in (([], ["model fg25",
                              "PHY 1a: interference x0 -2.40 dB, b 0.90 dB, capture 3.0 dB in the first 20 bits, "
                              "break 2.5 dB, adjacent channel 10.0 dB",
                              "PHY 3: interference x0 -2.85 dB, b 0.90 dB, capture 3.0 dB in the first 37 bits and "
                              "the SFD, break 2.5 dB, adjacent channel 10.0 dB",
                              "PHY other: interference x0 -2.85 dB, b 0.90 dB, capture 3.0 dB in the first 37 bits "
                              "and the SFD, break 2.5 dB, adjacent channel 10.0 dB"]),
                        (["--medium-model=em4"], ["model em4",
                                                  "PHY 3: interference as noise, capture 6.0 dB in the first SHR, "
                                                  "break off, adjacent channel off"]),
                        (["--medium-int-curve=1b:-1,2", "--medium-capture-early-bits=shr", "--medium-break-db=3:off"],
                         ["PHY 1a: interference x0 -1.00 dB, b 2.00 dB, capture 3.0 dB in the first SHR, break 2.5 dB",
                          "PHY 3: interference x0 -2.85 dB, b 0.90 dB, capture 3.0 dB in the first SHR and the SFD, "
                          "break off"])):
        srv = Server(["--vtime=1", "--vtime-end=0.001"] + opts)
        d = Driver(ARGS.sock, {1: Script(1)})
        d.connect()
        d.run()
        out = srv.output()
        check(srv.wait() == 0, f"{opts}: server exit")
        for ln in lines:
            check(ln in out, f"{opts}: no \"{ln}\" in {out[:1500]}")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("server", help="wssimserver binary (copied before use)")
    ap.add_argument("--sock", default="/tmp/em3c_sock")
    ap.add_argument("--copy", default=None,
                    help="name of the copy that runs (default: wssimserver-em3c in the log directory, so that "
                         "several runs at once do not share it)")
    ap.add_argument("--logdir", default=None)
    ap.add_argument("-k", default="", help="run only tests whose name contains this")
    ARGS = ap.parse_args()
    if ARGS.sock == "/tmp/sim_socket":
        sys.exit("refusing the default socket: it may belong to a real run")
    ARGS.logdir = ARGS.logdir or tempfile.mkdtemp(prefix="em3c_")
    os.makedirs(ARGS.logdir, exist_ok=True)
    ARGS.copy = ARGS.copy or os.path.join(ARGS.logdir, "wssimserver-em3c")
    shutil.copy2(ARGS.server, ARGS.copy)
    ARGS.bin = ARGS.copy

    failed = 0
    periodic_log = None
    tests = [
        ("periodic_vs_model", lambda: None),
        ("determinism", lambda: test_determinism(periodic_log)),
        ("seed", test_seed),
        ("ties", test_ties),
        ("replacement", test_replacement),
        ("end_boundary", test_end_boundary),
        ("past_deadline", test_past_deadline),
        ("many_nodes", test_many_nodes),
        ("loop_cap", test_loop_cap),
        ("sigint", test_sigint),
        ("disconnect", test_disconnect),
        ("vtime_refuses_wallclock_hello", test_vtime_refuses_wallclock_hello),
        ("wallclock", test_wallclock),
        ("drift", test_drift),
        ("kill_cut_link", test_kill_cut_link),
        ("start", test_start),
        ("power_up", test_power_up),
        ("medium_options", test_medium_options),
        ("medium_overlap", test_medium_overlap),
        ("medium_capture", test_medium_capture),
        ("medium_half_duplex", test_medium_half_duplex),
        ("medium_not_listening", test_medium_not_listening),
        ("medium_same_time", test_medium_same_time),
        ("medium_cca", test_medium_cca),
        ("medium_tx_ref", test_medium_tx_ref),
        ("medium_left", test_medium_left),
        ("medium_curve", test_medium_curve),
        ("medium_determinism", test_medium_determinism),
        ("medium_segments", test_medium_segments),
        ("medium_lock_refused", test_medium_lock_refused),
        ("medium_other_channel", test_medium_other_channel),
        ("medium_cut_drop", test_medium_cut_drop),
        ("medium_capture_margin", test_medium_capture_margin),
        ("medium_restart", test_medium_restart),
        ("medium_calib_sir", test_medium_calib_sir),
        ("medium_calib_capture", test_medium_calib_capture),
        ("medium_calib_adjacent", test_medium_calib_adjacent),
        ("medium_calib_options", test_medium_calib_options),
    ]
    for name, fn in tests:
        if ARGS.k not in name:
            continue
        t0 = time.monotonic()
        try:
            if name == "periodic_vs_model":
                periodic_log, out = test_periodic_vs_model()
            elif name == "determinism" and periodic_log is None:
                periodic_log = run_periodic()[1]
                fn()
            else:
                fn()
            print(f"PASS {name} ({time.monotonic() - t0:.1f} s)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"logs in {ARGS.logdir}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
