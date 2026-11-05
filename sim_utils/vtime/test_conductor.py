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
    srv = Server([f"--vtime={len(nodes)}", "--vtime-end=0.05"] + graph_args(PERIODIC_EDGES), env)
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
    srv = Server(["--vtime=3", "--vtime-end=1"] + graph_args([(1, 3), (2, 3)]))
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
    srv = Server(["--vtime=3"] + graph_args([(1, 2), (2, 3)]))
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
    srv = Server([f"--vtime={len(ids)}", "--vtime-end=0.02"] + graph_args(edges))
    d = Driver(ARGS.sock, {n: Staggered(n) for n in ids})
    d.connect()
    log = d.run(timeout=60)
    check(srv.wait(60) == 0, "server exit")
    mlog, _ = Model(graph_of(edges), {n: Staggered(n) for n in ids}, end_us=20000).run()
    check(log == mlog, "differs from the model")


def test_loop_cap():
    nodes = {n: PingPong(n) for n in (1, 2)}
    srv = Server(["--vtime=2"])
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
    srv = Server(["--vtime=3", "--vtime-end=0.01", "--vtime-cut=1:2:0.003", "--vtime-kill=2:0.006",
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
    srv = Server(["--vtime=2", "--vtime-end=0.01", "--vtime-link=1:2:0.002:-80"] + graph_args([(1, 3)]))
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
    srv = Server(["--vtime=2", "--vtime-end=0.01", "--vtime-start=2:0.005"] + graph_args([(1, 2)]))
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
    srv = Server(["--vtime=2", "--vtime-end=0.006", "--vtime-power-up=2:0.0035"] + graph_args([(1, 2)]))
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
    srv = Server([f"--vtime={len(nodes)}", "--vtime-end=0.01", *opts] + graph_args(PERIODIC_EDGES),
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
                self.send(PAYLOAD, **a[1])
            elif a[0] == "sense":
                self.sense(a[1])
            elif a[0] == "radio":
                self.radio = (a[1], a[2])
        later = [x for x in self.times() if x > t]
        return self.out, min(later) if later else NEVER


def medium_run(radios, links, opts=(), seed="1", hook=None):
    """links: [(a, b, rssi or None)]. Returns the driver, the server output
    and the --medium-log lines as tuples."""
    mlog = os.path.join(ARGS.logdir, "medium.log")
    if os.path.exists(mlog):
        os.unlink(mlog)
    g = sum((["-g", f"{a},{b}" + ("" if r is None else f":{r}")] for a, b, r in links), [])
    srv = Server([f"--vtime={len(radios)}", "--medium=central", f"--medium-log={mlog}", *opts] + g,
                 {"WSSIM_SEED": seed})
    d = Driver(ARGS.sock, radios)
    d.on_wake = hook
    d.connect()
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
                      (["--vtime=1", "--medium-log=/dev/null"], "need --medium=central"),
                      (["--vtime=1", "--medium=legacy", "--medium-capture-db=3"], "need --medium=central"),
                      (["--vtime=1", "--medium=central", "--medium-lock-snr=x"], "expected a number")):
        srv = Server(args)
        check(srv.wait() != 0 and msg in srv.output(), f"{args} accepted, or no \"{msg}\"")
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
    """A frame 20 dB stronger captures the receiver during the first frame's
    SHR (1600 us), not after; one 3 dB stronger never does."""
    for delay, rssi3, captured in ((SHR_US - 1, -60, True), (SHR_US, -60, False), (500, -77, False)):
        r = {1: Radio(1, {1000: [("tx", {})]}), 2: Radio(2), 3: Radio(3, {1000 + delay: [("tx", {})]})}
        d, out, rows = medium_run(r, [(1, 2, -80), (3, 2, rssi3)])
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
    the neighbour's MAC knows of it), the noise floor on another channel, after
    the frame, at a node that does not hear the sender, and at the sender."""
    r = {1: Radio(1, {1000: [("tx", {"chan": 3})], 2000: [("sense", 3)]}),
         2: Radio(2, {0: [("radio", RADIO_IDLE, 3)], 2000: [("sense", 3), ("sense", 4)], 7000: [("sense", 3)]}),
         3: Radio(3, {2000: [("sense", 3)]})}
    d, out, rows = medium_run(r, [(1, 2, -70), (2, 3, -50)])
    e = 10 * math.log10(10 ** (NOISE_DBM / 10) + 10 ** (-70 / 10))
    s2 = [dbm for t, c, dbm in r[2].sensed]
    check(len(s2) == 3 and abs(s2[0] - e) < 0.01 and abs(s2[1] - NOISE_DBM) < 0.01 and abs(s2[2] - NOISE_DBM) < 0.01,
          f"CCA at 2: {r[2].sensed}")
    check(abs(r[3].sensed[0][2] - NOISE_DBM) < 0.01 and abs(r[1].sensed[0][2] - NOISE_DBM) < 0.01,
          f"CCA at 3 {r[3].sensed}, at 1 {r[1].sensed}")
    check("5 CCA queries, 1 with a frame on air" in out, "CCA summary")


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
