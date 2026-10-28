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
                       HELLO_VTIME)
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
    s2.send(FRAME.pack(b"xx", 3, 0, 7, 0xFFFF, 10, 4) + b"abc")
    for s in (s1, s3):
        s.settimeout(5)
        msg = s.recv(100)
        _, ln, _, seq, src, _, _ = FRAME.unpack_from(msg)
        check(src == 2 and seq == 7 and msg[FRAME.size:] == b"abc", f"relay: {msg!r}")
    s1.send(FRAME.pack(b"xx", 1, 0, 1, 0, 10, 4) + b"z")
    s2.settimeout(5)
    check(FRAME.unpack_from(s2.recv(100))[4] == 1, "relay 1 -> 2")
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


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("server", help="wssimserver binary (copied before use)")
    ap.add_argument("--sock", default="/tmp/em3c_sock")
    ap.add_argument("--copy", default="/tmp/wssimserver-em3c", help="name of the copy that runs")
    ap.add_argument("--logdir", default=None)
    ap.add_argument("-k", default="", help="run only tests whose name contains this")
    ARGS = ap.parse_args()
    if ARGS.sock == "/tmp/sim_socket":
        sys.exit("refusing the default socket: it may belong to a real run")
    shutil.copy2(ARGS.server, ARGS.copy)
    ARGS.bin = ARGS.copy
    ARGS.logdir = ARGS.logdir or tempfile.mkdtemp(prefix="em3c_")
    os.makedirs(ARGS.logdir, exist_ok=True)

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
