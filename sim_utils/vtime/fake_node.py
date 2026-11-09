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

Central medium (docs/MEDIUM.md): a behaviour may set `radio` to
(state, channel), which the driver writes to the node's 4-byte entry of the
shared memory "/wssimcca" before it answers each wake; it may send frames with
header fields (`send(payload, chan=..., ...)`) and CCA queries
(`sense(chan)`, answered before the node's "xi", the result in `sensed`).
Verdicts ("xv") reach `on_verdict()`, and in the event log they appear among
the frames as ("xv", frame_id, ok, cause). `rx` keeps every message received,
with the frames' headers.

Preamble sniffing (docs/MEDIUM.md §10): `sniff(chan, duration_us, ...)` sends
an "xn", `send_lp(payload, preamble_us, ...)` a frame with a long preamble
("xl"); sniff results ("xd") reach `on_sniff()` and appear in the event log
as ("xd", fields).

Used by test_conductor.py; no dependencies outside the standard library.
"""
import heapq
import mmap
import os
import select
import socket
import struct

NEVER = 2**64 - 1
HELLO_VTIME = 0x0001

HELLO = struct.Struct("=2sHHH")      # magic, node_id, flags, reserved
# magic, len, chan, seq, src, shr_len, fcs_len, phr_len, tx_dbm, flags, reserved,
# rate_bps, frame_id, rx_dbm
FRAME = struct.Struct("=2sHHHHBBBbBBIIf")
FRAME_FIELDS = ("magic", "len", "chan", "seq", "src", "shr_len", "fcs_len", "phr_len", "tx_dbm", "flags",
                "reserved", "rate_bps", "frame_id", "rx_dbm")
FRAME_MEDIUM = 0x01
VERDICT = struct.Struct("=2sBBIff")  # magic, ok, cause, frame_id, rx_dbm, sinr_db
SENSE = struct.Struct("=2sHf")       # magic, channel, dbm
SNIFF = struct.Struct("=2sHIIBBH")   # magic, channel, duration_us, sniff_id, flags, end_state, reserved
SNIFF_RESULT = struct.Struct("=2sBBIIHHQff")  # magic, detected, locked, sniff_id, frame_id, src, reserved, t_us,
#                                               rssi_dbm, sinr_db
SNIFF_FIELDS = ("magic", "detected", "locked", "sniff_id", "frame_id", "src", "reserved", "t_us", "rssi_dbm",
                "sinr_db")
LP_FRAME = struct.Struct("=2sHI")    # magic, reserved, preamble_us; an "xx" follows
SNIFF_LOCK = 0x01
SNIFF_LOCKED, SNIFF_RECEIVING = 1, 2
FRAME_LONG_PREAMBLE = 0x02
WELCOME = struct.Struct("=2sHIQiIQ")  # magic, version, flags, seed, drift_ppb, reserved, start_us
PROTO_VERSION = 4
WELCOME_POWER = 0x0001
WELCOME_MEDIUM = 0x0002
# Defaults of the frames the fake nodes send: SHR 10 bytes, PHR 2, FCS 4, 50 kbit/s
FRAME_DEFAULTS = {"chan": 0, "shr_len": 10, "phr_len": 2, "fcs_len": 4, "tx_dbm": 0, "rate_bps": 50000}

# enum sim_radio_state, and the driver states of app_wshwsim/sl_rf_driver.c
# that go with them (byte 0 of the shared-memory entry)
RADIO_OFF, RADIO_IDLE, RADIO_LISTEN, RADIO_RECEIVE, RADIO_TX = range(5)
DRVSTATE = {RADIO_OFF: 7, RADIO_IDLE: 2, RADIO_LISTEN: 3, RADIO_RECEIVE: 5, RADIO_TX: 4}
SHM_STRIDE = 4


def frame_msg(payload, seq=0, src=0xFFFF, **fields):
    """An "xx" message; fields override FRAME_DEFAULTS."""
    f = dict(FRAME_DEFAULTS, **fields)
    return FRAME.pack(b"xx", len(payload), f["chan"], seq, src, f["shr_len"], f["fcs_len"], f["phr_len"],
                      f["tx_dbm"], 0, 0, f["rate_bps"], 0, 0.0) + payload


def frame_unpack(msg):
    return dict(zip(FRAME_FIELDS, FRAME.unpack_from(msg)))


def shm_path(sock_path):
    """The file of the server's "/wssimcca" (sim_shm_name())."""
    if sock_path == "/tmp/sim_socket":
        return "/dev/shm/wssimcca"
    return "/dev/shm/wssimcca." + sock_path.replace("/", "_")
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
        self.radio = None           # (state, channel) for the shared memory, or None
        self.sensed = []            # (time, channel, dBm) of each "xs" answered
        self.rx = []                # (time, kind, fields) of each message received

    def set_timer(self, name, t):
        self.timers[name] = t

    def cancel(self, name):
        self.timers.pop(name, None)

    def send(self, payload, **fields):
        """A frame; fields (FRAME_DEFAULTS' names) only for the central medium."""
        payload = payload.encode() if isinstance(payload, str) else payload
        self.out.append((payload, fields) if fields else payload)

    def sense(self, chan):
        """A CCA query, in order with the frames sent in this run."""
        self.out.append(("xs", chan))

    def sniff(self, chan, duration_us, sniff_id=0, flags=0, end_state=0):
        """A preamble sniff ("xn"); its result reaches on_sniff()."""
        self.out.append(("xn", chan, duration_us, sniff_id, flags, end_state))

    def send_lp(self, payload, preamble_us, **fields):
        """A frame after preamble_us of preamble ("xl")."""
        payload = payload.encode() if isinstance(payload, str) else payload
        self.out.append(("xl", preamble_us, payload, fields))

    def raw(self, msg):
        """Any other message, as bytes (e.g. an "xr")."""
        self.out.append(("raw", msg))

    def on_sniff(self, result):
        pass

    def start(self):
        pass

    def on_timer(self, name):
        pass

    def on_frame(self, src, payload):
        pass

    def on_verdict(self, frame_id, ok, cause):
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
        for fr in frames:
            if fr[0] == "xv":
                self.on_verdict(*fr[1:])
            elif fr[0] == "xd":
                self.on_sniff(fr[1])
            else:
                self.on_frame(*fr)
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
        self.start = {}                 # node -> start_us from its welcome
        self.quit = set()
        self.closed = set()            # closed by the conductor without "xq"
        self.on_wake = None            # hook(driver, node, t) -> "close" to drop the node
        self.on_close = None           # hook(driver, node) when the conductor closes a node
        self.socks = {}
        self.welcome_flags = {}         # node -> flags of its welcome
        self.shm = None

    def connect(self):
        for nid in sorted(self.nodes):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            s.connect(self.sock_path)
            s.send(HELLO.pack(b"xh", nid, HELLO_VTIME if self.vtime_flag else 0, 0))
            self.socks[nid] = s

    def _write_radio(self, nid):
        radio = self.nodes[nid].radio
        if radio is None:
            return
        if self.shm is None:
            with open(shm_path(self.sock_path), "r+b") as f:
                self.shm = mmap.mmap(f.fileno(), 0)
        state, chan = radio
        self.shm[nid * SHM_STRIDE:(nid + 1) * SHM_STRIDE] = struct.pack("=BHB", DRVSTATE[state], chan, state)

    def _reply(self, nid, t, frames, first=False):
        node = self.nodes[nid]
        out, nxt = node.run(t, frames, first)
        self.log.append((t, nid, tuple(frames), tuple(o for o in out if not (isinstance(o, tuple) and o[0] == "xs"))))
        self._write_radio(nid)
        s = self.socks[nid]
        for i, p in enumerate(out):
            if isinstance(p, tuple) and p[0] == "xs":
                s.send(SENSE.pack(b"xs", p[1], 0.0))
                s.settimeout(10)
                msg = s.recv(65536)
                s.settimeout(None)
                if len(msg) != SENSE.size or msg[:2] != b"xs":
                    raise TestError(f"node {nid}: expected the answer to \"xs\", got {msg[:2]!r} ({len(msg)} bytes)")
                _, chan, dbm = SENSE.unpack(msg)
                node.sensed.append((t, chan, dbm))
                continue
            if isinstance(p, tuple) and p[0] == "raw":
                s.send(p[1])
                continue
            if isinstance(p, tuple) and p[0] == "xn":
                s.send(SNIFF.pack(b"xn", *p[1:], 0))
                continue
            if isinstance(p, tuple) and p[0] == "xl":
                s.send(LP_FRAME.pack(b"xl", 0, p[1]) + frame_msg(p[2], seq=i, **p[3]))
                continue
            payload, fields = p if isinstance(p, tuple) else (p, {})
            s.send(frame_msg(payload, seq=i, **fields))     # src is set by the server
        s.send(IDLE.pack(b"xi", 0, 0, 0, nxt))

    def _recv_frame(self, nid, s, t=None):
        msg = s.recv(65536)
        if msg[:2] == b"xd":
            if len(msg) != SNIFF_RESULT.size:
                raise TestError(f"node {nid}: sniff result of {len(msg)} bytes")
            res = dict(zip(SNIFF_FIELDS, SNIFF_RESULT.unpack(msg)))
            self.nodes[nid].rx.append((t, "xd", res))
            return ("xd", res)
        if msg[:2] == b"xv":
            if len(msg) != VERDICT.size:
                raise TestError(f"node {nid}: verdict of {len(msg)} bytes")
            _, ok, cause, frame_id, rx_dbm, sinr = VERDICT.unpack(msg)
            self.nodes[nid].rx.append((t, "xv", {"frame_id": frame_id, "ok": ok, "cause": cause,
                                                 "rx_dbm": rx_dbm, "sinr_db": sinr}))
            return ("xv", frame_id, ok, cause)
        if msg[:2] != b"xx":
            raise TestError(f"node {nid}: expected a frame, got {msg[:2]!r}")
        hdr = frame_unpack(msg)
        if hdr["len"] != len(msg) - FRAME.size:
            raise TestError(f"node {nid}: frame length {hdr['len']}, message {len(msg)}")
        if hdr["src"] not in self.nodes:
            raise TestError(f"node {nid}: frame from unknown src {hdr['src']}")
        self.nodes[nid].rx.append((t, "xx", dict(hdr, payload=msg[FRAME.size:])))
        return hdr["src"], msg[FRAME.size:]

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
                    if self.on_close:
                        self.on_close(self, nid)
                    continue
                magic = msg[:2]
                if magic == b"xw":
                    _, version, flags, seed, drift, _, start = WELCOME.unpack(msg)
                    if version != PROTO_VERSION:
                        raise TestError(f"node {nid}: welcome version {version}")
                    self.welcome_flags[nid] = flags
                    self.seed = seed
                    self.drift[nid] = drift
                    self.start[nid] = start
                    self._reply(nid, start, [], first=True)
                elif magic == b"xa":
                    _, count, _, _, t = ADVANCE.unpack(msg)
                    frames = [self._recv_frame(nid, s, t) for _ in range(count)]
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
        if self.shm is not None:
            self.shm.close()
            self.shm = None
        return self.log
