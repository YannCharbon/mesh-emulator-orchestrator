#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fake virtual-time nodes for testing the wssimserver conductor.

The nodes speak the conductor protocol of docs/VIRTUAL_TIME.md §3 and
nanostack-generic-standalone/port/sim/ns_port_sim.h over real sockets, but
their behaviour is a small Python object: timers, frames sent when a timer
fires or a frame arrives. All the nodes of a run live in one process, which is
possible because the conductor runs one node at a time.

The same behaviour objects also run on `Model`, a Python reading of the
conductor rules (order by time, node ID, sequence; "xi" replaces the deadline;
frames to the graph neighbours at the sender's time). A run over the real
conductor must give the same event log as the model.

Used by test_conductor.py; no dependencies outside the standard library.
"""
import heapq
import select
import socket
import struct

NEVER = 2**64 - 1
HELLO_VTIME = 0x0001

HELLO = struct.Struct("=2sHHH")      # magic, node_id, flags, reserved
FRAME = struct.Struct("=2sHHHHBB")   # magic, len, chan, seq, src, shr_len, fcs_len
WELCOME = struct.Struct("=2sHIQiI")  # magic, version, flags, seed, drift_ppb, reserved
ADVANCE = struct.Struct("=2s3HQ")    # magic, frames, reserved[2], now_us
IDLE = struct.Struct("=2s3HQ")       # magic, reserved[3], next_us


class TestError(Exception):
    pass


class Behaviour:
    """One node's behaviour. Subclasses override on_timer/on_frame.

    run() is called at each wake with the time and the frames received
    (src, payload); it returns the payloads to send, and the node's next
    deadline is the earliest of its timers.
    """

    def __init__(self, nid):
        self.nid = nid
        self.timers = {}            # name -> absolute deadline (us)
        self.out = []
        self.now = 0

    def set_timer(self, name, t):
        self.timers[name] = t

    def cancel(self, name):
        self.timers.pop(name, None)

    def send(self, payload):
        self.out.append(payload.encode() if isinstance(payload, str) else payload)

    def start(self):
        pass

    def on_timer(self, name):
        pass

    def on_frame(self, src, payload):
        pass

    def next_deadline(self):
        return min(self.timers.values()) if self.timers else NEVER

    def run(self, t, frames, first=False):
        if t < self.now:
            raise TestError(f"node {self.nid}: woken at {t}, before its last wake {self.now}")
        self.now = t
        self.out = []
        if first:
            self.start()
        for src, payload in frames:
            self.on_frame(src, payload)
        # Like the port: timers due at t fire after the frames, by deadline then name.
        while True:
            due = sorted((d, n) for n, d in self.timers.items() if d <= t)
            if not due:
                break
            d, name = due[0]
            if d < t:
                raise TestError(f"node {self.nid}: timer {name} at {d} missed (woken at {t})")
            del self.timers[name]
            self.on_timer(name)
        return self.out, self.next_deadline()


class Model:
    """The conductor rules, in Python."""

    LOOP_CAP = 100000

    def __init__(self, graph, behaviours, end_us=None):
        self.graph = graph            # node -> set of neighbours
        self.nodes = behaviours       # node -> Behaviour
        self.end_us = end_us

    def run(self):
        heap, log = [], []
        seq = [0]
        deadline_seq = {}

        def push(t, node, frame):
            seq[0] += 1
            heapq.heappush(heap, (t, node, seq[0], frame))
            if frame is None:
                deadline_seq[node] = seq[0]

        def after_run(node, t, out, nxt):
            for p in out:
                for dst in sorted(self.graph.get(node, ())):
                    push(t, dst, (node, p))
            deadline_seq.pop(node, None)
            if nxt != NEVER:
                push(max(nxt, t), node, None)

        for node in sorted(self.nodes):
            out, nxt = self.nodes[node].run(0, [], first=True)
            log.append((0, node, (), tuple(out)))
            after_run(node, 0, out, nxt)
        now, same, wakes = 0, 0, 0
        while True:
            while heap and heap[0][3] is None and deadline_seq.get(heap[0][1]) != heap[0][2]:
                heapq.heappop(heap)
            if not heap:
                return log, "no more events"
            t, node = heap[0][0], heap[0][1]
            if self.end_us is not None and t > self.end_us:
                return log, "end time reached"
            if t == now and wakes:
                same += 1
                if same > self.LOOP_CAP:
                    return log, "same-time loop"
            else:
                same = 1
            frames = []
            while heap and heap[0][0] == t and heap[0][1] == node:
                _, _, s, frame = heapq.heappop(heap)
                if frame is None:
                    if deadline_seq.get(node) == s:
                        del deadline_seq[node]
                else:
                    frames.append(frame)
            now = t
            wakes += 1
            out, nxt = self.nodes[node].run(t, frames)
            log.append((t, node, tuple(frames), tuple(out)))
            after_run(node, t, out, nxt)


class Driver:
    """Runs the behaviours as real nodes connected to a conductor."""

    def __init__(self, sock_path, behaviours, vtime_flag=True):
        self.sock_path = sock_path
        self.nodes = behaviours
        self.vtime_flag = vtime_flag
        self.log = []
        self.seed = None
        self.drift = {}                 # node -> drift_ppb from its welcome
        self.quit = set()
        self.closed = set()            # closed by the conductor without "xq"
        self.on_wake = None            # hook(driver, node, t) -> "close" to drop the node
        self.socks = {}

    def connect(self):
        for nid in sorted(self.nodes):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            s.connect(self.sock_path)
            s.send(HELLO.pack(b"xh", nid, HELLO_VTIME if self.vtime_flag else 0, 0))
            self.socks[nid] = s

    def _reply(self, nid, t, frames, first=False):
        out, nxt = self.nodes[nid].run(t, frames, first)
        self.log.append((t, nid, tuple(frames), tuple(out)))
        s = self.socks[nid]
        for i, p in enumerate(out):
            s.send(FRAME.pack(b"xx", len(p), 0, i, 0xFFFF, 10, 4) + p)   # src is set by the server
        s.send(IDLE.pack(b"xi", 0, 0, 0, nxt))

    def _recv_frame(self, nid, s):
        msg = s.recv(65536)
        if msg[:2] != b"xx":
            raise TestError(f"node {nid}: expected a frame, got {msg[:2]!r}")
        magic, ln, chan, seq, src, _, _ = FRAME.unpack_from(msg)
        if ln != len(msg) - FRAME.size:
            raise TestError(f"node {nid}: frame length {ln}, message {len(msg)}")
        if src not in self.nodes:
            raise TestError(f"node {nid}: frame from unknown src {src}")
        return src, msg[FRAME.size:]

    def run(self, timeout=30.0):
        """Serve until every node got "xq" or was closed."""
        while len(self.quit) + len(self.closed) < len(self.socks):
            live = {s: n for n, s in self.socks.items() if n not in self.quit and n not in self.closed}
            r, _, _ = select.select(list(live), [], [], timeout)
            if not r:
                raise TestError("timeout: no message from the conductor")
            # Only "xq" may reach several nodes at once.
            for s in r:
                nid = live[s]
                msg = s.recv(65536)
                if not msg:
                    self.closed.add(nid)
                    continue
                magic = msg[:2]
                if magic == b"xw":
                    _, version, _, seed, drift, _ = WELCOME.unpack(msg)
                    if version != 2:
                        raise TestError(f"node {nid}: welcome version {version}")
                    self.seed = seed
                    self.drift[nid] = drift
                    self._reply(nid, 0, [], first=True)
                elif magic == b"xa":
                    _, count, _, _, t = ADVANCE.unpack(msg)
                    frames = [self._recv_frame(nid, s) for _ in range(count)]
                    if self.on_wake and self.on_wake(self, nid, t) == "close":
                        s.close()
                        self.closed.add(nid)
                        continue
                    self._reply(nid, t, frames)
                elif magic == b"xq":
                    self.quit.add(nid)
                else:
                    raise TestError(f"node {nid}: unexpected message {magic!r} ({len(msg)} bytes)")
        for s in self.socks.values():
            s.close()
        return self.log
