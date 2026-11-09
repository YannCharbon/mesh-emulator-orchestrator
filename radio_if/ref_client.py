#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Reference client of the emulator's stack-neutral radio interface.

Written from docs/RADIO_INTERFACE.md (interface version 2) alone: it uses
the Python standard library only, and no emulator or Nanostack code. One
process is one node. It illustrates the interface: a client for another
stack is written from the document, not copied from this file.

Three layers:

- RadioIf: the interface itself (connect, hello, welcome, wakes, frames,
  verdicts, radio-state reports, CCA, idle, quit, and from version 2
  preamble sniffs and long-preamble frames). A stack's own client does the
  same in its own language.
- WindowNode: a small demonstration protocol on top of it. Every node has a
  receive window of --window-ms at the start of each period of --period-ms
  (on its own clock), sleeps with its radio off in between, and sends its
  frames at the offsets given with --tx, inside the window. CCA before each
  frame unless --no-cca; a busy channel skips the frame (no backoff).
- SniffNode (--sniff-period-ms or --lp-tx): preamble sampling. The node
  sniffs one channel for --sniff-us every --sniff-period-ms on its own
  clock, its radio off in between, and receives a frame whose preamble it
  detects. It sends its frames at the times given with --lp-tx, each with a
  wake-up preamble of --lp-preamble-ms ("xl"), so that every sniffing
  neighbour samples the preamble at least once. No CCA before them.

Output: one line per event on stdout, "<virtual µs> <node> <event> k=v ...",
read by test_ref_client.py. With --mop-period-ms, also "<virtual µs> <node>
MOP1 {json}" lines: the node's state, links, counters and events in the Mesh
Observation Protocol (MOP v1) of the mesh testbed controller, which then
shows the node with its relations and events (class Mop).

    ref_client.py --sock /tmp/sock --id 2 --tx 10 --window-ms 30
"""
import argparse
import json
import socket
import struct
import sys

IF_VERSION = 2                           # the newest version this client implements

# Message layouts (docs/RADIO_INTERFACE.md §4): little-endian, no padding
HELLO = struct.Struct("<2sHHH")          # magic, node_id, flags, version
WELCOME = struct.Struct("<2sHIQiIQ")     # magic, version, flags, seed, drift_ppb, reserved, start_us
ADVANCE = struct.Struct("<2sHHHQ")       # magic, count, reserved, reserved, now_us
IDLE = struct.Struct("<2sHHHQ")          # magic, reserved x3, next_us
WAKE = struct.Struct("<2sH6H")           # magic, events, timers[6]
REPORT = struct.Struct("<2sBbBBH")       # magic, state, tx_dbm, events, phy_mode, channel
SENSE = struct.Struct("<2sHf")           # magic, channel, dbm
FRAME = struct.Struct("<2sHHHHBBBbBBIIf")  # magic, len, chan, seq, src, shr, fcs, phr, tx_dbm, flags, res, rate, id, rx_dbm
VERDICT = struct.Struct("<2sBBIff")      # magic, ok, cause, frame_id, rx_dbm, sinr_db
# Version 2 (§6.7)
SNIFF = struct.Struct("<2sHIIBBH")       # magic, channel, duration_us, sniff_id, flags, end_state, reserved
SNIFF_RESULT = struct.Struct("<2sBBIIHHQff")  # magic, detected, locked, sniff_id, frame_id, src, res, t_us, rssi, sinr
LP_FRAME = struct.Struct("<2sHI")        # magic, reserved, preamble_us; a complete "xx" follows

HELLO_VTIME, HELLO_RADIO_IF = 0x0001, 0x0002
WELCOME_POWER, WELCOME_MEDIUM, WELCOME_SNIFF = 0x0001, 0x0002, 0x0004
SNIFF_LOCK = 0x01
NOT_LOCKED, LOCKED, RECEIVING = 0, 1, 2  # "xd" locked
FRAME_MEDIUM, FRAME_LONG_PREAMBLE = 0x01, 0x02
NEVER = 2**64 - 1
OFF, IDLE_STATE, LISTEN, RECEIVE, TX = range(5)
STATE_NAMES = ("off", "idle", "listen", "receive", "tx")
EV_CCA = 0x01
CAUSES = ("ok", "noise", "interference")
CAUSE_RADIO, CAUSE_OTHER = 4, 5          # timer causes of the "xc" report


def printable(psdu):
    """A PSDU for the log: as text if it is printable ASCII without spaces (this
    protocol's frames, without their padding), else in hex (another stack's
    frames, which a node of a mixed run hears too)."""
    p = psdu.rstrip(b".")
    if p and all(0x21 <= b < 0x7f for b in p):
        return p.decode()
    return "hex:" + psdu.hex()


class Closed(Exception):
    """The run ended: "xq", or the server closed the connection."""


class RadioIf:
    """One node's connection to the emulator."""

    def __init__(self, sock_path, node_id, version=IF_VERSION):
        self.node_id = node_id
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.s.connect(sock_path)
        self.s.send(HELLO.pack(b"xh", node_id, HELLO_VTIME | HELLO_RADIO_IF, version))
        msg = self._recv()
        if msg[:2] != b"xw" or len(msg) != WELCOME.size:
            raise RuntimeError(f"expected a welcome, got {msg[:2]!r} ({len(msg)} bytes)")
        _, self.version, self.flags, self.seed, self.drift_ppb, _, self.start_us = WELCOME.unpack(msg)
        if self.version != version:
            raise RuntimeError(f"welcome for interface version {self.version}, expected {version}")
        self.now = self.start_us
        self.power = bool(self.flags & WELCOME_POWER)
        # Sniffing (§6.7): version 2, and the server says it serves it
        self.can_sniff = self.version >= 2 and bool(self.flags & WELCOME_SNIFF)
        self._timers_fired = [0] * 6
        self._events = 0

    def _recv(self):
        msg = self.s.recv(65536)
        if not msg:
            raise Closed("connection closed by the server")
        if msg[:2] == b"xq":
            raise Closed("end of the run")
        return msg

    # --- the node's clock (§6.3) ---
    def local_us(self, v=None):
        """The node's own clock at virtual time v (default: now), from its start."""
        e = (self.now if v is None else v) - self.start_us
        return e + (e * self.drift_ppb) // 10**9

    def virtual_at(self, local):
        """The first virtual µs at which the node's clock reads at least local."""
        v = self.start_us + (local * 10**9) // (10**9 + self.drift_ppb)
        while self.local_us(v) < local:
            v += 1
        while v > self.start_us and self.local_us(v - 1) >= local:
            v -= 1
        return v

    # --- what the node does during a wake ---
    def report(self, state, channel, tx_dbm=0, cca=False):
        self.s.send(REPORT.pack(b"xr", state, tx_dbm, EV_CCA if cca else 0, 0, channel))

    def cca(self, channel):
        """Energy on the channel now, in dBm (noise floor plus every frame on air)."""
        self.s.send(SENSE.pack(b"xs", channel, 0.0))
        msg = self._recv()
        if msg[:2] != b"xs" or len(msg) != SENSE.size:
            raise RuntimeError(f"expected the CCA answer, got {msg[:2]!r}")
        return SENSE.unpack(msg)[2]

    def send_frame(self, psdu, channel, rate_bps, shr_len, phr_len, fcs_len, tx_dbm, seq=0):
        """Sends a frame starting now; returns its airtime in µs."""
        self.s.send(FRAME.pack(b"xx", len(psdu), channel, seq & 0xFFFF, 0, shr_len, fcs_len, phr_len, tx_dbm,
                               0, 0, rate_bps, 0, 0.0) + psdu)
        return (shr_len + phr_len + len(psdu) + fcs_len) * 8 * 10**6 // rate_bps

    def send_lp_frame(self, preamble_us, psdu, channel, rate_bps, shr_len, phr_len, fcs_len, tx_dbm, seq=0):
        """Sends a frame with preamble_us of wake-up preamble before it, starting
        now ("xl"); returns the whole transmission's time in µs."""
        if not self.can_sniff:
            raise RuntimeError("long-preamble frames need interface version 2 and WELCOME_SNIFF")
        self.s.send(LP_FRAME.pack(b"xl", 0, preamble_us) +
                    FRAME.pack(b"xx", len(psdu), channel, seq & 0xFFFF, 0, shr_len, fcs_len, phr_len, tx_dbm,
                               0, 0, rate_bps, 0, 0.0) + psdu)
        return preamble_us + (shr_len + phr_len + len(psdu) + fcs_len) * 8 * 10**6 // rate_bps

    def sniff(self, channel, duration_us, sniff_id, lock=True, end_state=OFF):
        """Turns the receiver on, on channel, for duration_us, looking for a
        preamble ("xn"). The answer, one "xd", comes in a later wake."""
        if not self.can_sniff:
            raise RuntimeError("sniffing needs interface version 2 and WELCOME_SNIFF")
        self.s.send(SNIFF.pack(b"xn", channel, duration_us, sniff_id & 0xFFFFFFFF, SNIFF_LOCK if lock else 0,
                               end_state, 0))

    def count(self, events=0, timer_cause=None):
        """Work done in this wake, for the power report's MCU model (optional)."""
        self._events += events
        if timer_cause is not None:
            self._timers_fired[timer_cause] += 1

    # --- between wakes ---
    def idle(self, next_us):
        """Ends the wake; next_us is virtual time (NEVER: no deadline)."""
        if self.power:
            self.s.send(WAKE.pack(b"xc", min(self._events, 65535), *(min(t, 65535) for t in self._timers_fired)))
        self._timers_fired = [0] * 6
        self._events = 0
        self.s.send(IDLE.pack(b"xi", 0, 0, 0, next_us))

    def wait(self):
        """Blocks until the next wake: returns its messages, each
        ("frame", header dict, psdu), ("verdict", dict) or ("sniff", dict).
        Raises Closed."""
        msg = self._recv()
        if msg[:2] != b"xa" or len(msg) != ADVANCE.size:
            raise RuntimeError(f"expected a wake, got {msg[:2]!r} ({len(msg)} bytes)")
        _, count, _, _, now = ADVANCE.unpack(msg)
        if now < self.now:
            raise RuntimeError(f"woken at {now} us, before {self.now} us")
        self.now = now
        out = []
        for _ in range(count):
            m = self._recv()
            if m[:2] == b"xx":
                f = FRAME.unpack_from(m)
                hdr = dict(zip(("len", "chan", "seq", "src", "shr_len", "fcs_len", "phr_len", "tx_dbm", "flags",
                                "reserved", "rate_bps", "frame_id", "rx_dbm"), f[1:]))
                out.append(("frame", hdr, m[FRAME.size:FRAME.size + hdr["len"]]))
            elif m[:2] == b"xv":
                _, ok, cause, fid, rx_dbm, sinr = VERDICT.unpack(m)
                out.append(("verdict", {"ok": ok, "cause": cause, "frame_id": fid, "rx_dbm": rx_dbm,
                                        "sinr_db": sinr}))
            elif m[:2] == b"xd" and len(m) == SNIFF_RESULT.size and self.version >= 2:
                f = SNIFF_RESULT.unpack(m)
                out.append(("sniff", dict(zip(("detected", "locked", "sniff_id", "frame_id", "src", "reserved",
                                               "t_us", "rssi_dbm", "sinr_db"), f[1:]))))
            else:
                raise RuntimeError(f"unexpected message {m[:2]!r} in a wake")
        return out


class WindowNode:
    """Periodic receive windows, radio off in between, frames sent at given offsets."""

    def __init__(self, radio, args):
        self.r = radio
        self.a = args
        self.state = OFF
        self.timers = {}            # name -> virtual µs
        self.held = None            # header of the frame being received
        self.window_open = False
        self.close_pending = False
        self.period = 0
        self.seq = 0
        self.mop = Mop(self) if args.mop_period_us else None

    def log(self, event, **kv):
        print(f"{self.r.now} {self.r.node_id} {event}" + "".join(f" {k}={v}" for k, v in kv.items()), flush=True)
        if self.mop is not None:
            self.mop.on_log(event, kv)

    def set_state(self, state, cca=False):
        if state != self.state or cca:
            self.state = state
            self.r.report(state, self.a.chan, self.a.tx_dbm if state == TX else 0, cca)

    def start(self):
        self.set_state(OFF)
        self.log("start", seed=self.r.seed, drift_ppb=self.r.drift_ppb, start_us=self.r.start_us)
        self.period = 1                         # the first window one period after power-up
        self.timers["open"] = self.r.virtual_at(self.period * self.a.period_us + self.a.phase_us)

    # --- timers ---
    def on_open(self):
        base = self.period * self.a.period_us + self.a.phase_us
        self.window_open = True
        self.close_pending = False
        self.set_state(LISTEN)
        self.log("window_open", period=self.period)
        self.timers["close"] = self.r.virtual_at(base + self.a.window_us)
        for i, off in enumerate(self.a.tx):
            self.timers[f"tx{i}"] = self.r.virtual_at(base + off)
        self.period += 1
        self.timers["open"] = self.r.virtual_at(self.period * self.a.period_us + self.a.phase_us)

    def on_close(self):
        self.window_open = False
        if self.state in (RECEIVE, TX):
            self.close_pending = True           # after the frame's end
        else:
            self.set_state(OFF)
            self.log("window_close")

    def on_tx(self):
        if self.state == TX:
            self.log("tx_skipped", reason="already_sending")
            return
        if self.a.cca:
            dbm = self.r.cca(self.a.chan)
            # the CCA is charged at the end of the listening before it
            self.set_state(self.state, cca=True)
            if dbm >= self.a.cca_dbm:
                self.log("cca_busy", dbm=f"{dbm:.1f}")
                return
        if self.held is not None:
            self.log("rx_aborted", src=self.held["src"], frame=self.held["frame_id"])
            self.held = None                    # half duplex: no verdict will come
        self.seq += 1
        psdu = f"N{self.r.node_id}P{self.period - 1}S{self.seq}".encode().ljust(self.a.payload_len, b".")
        self.set_state(TX)
        airtime = self.r.send_frame(psdu, self.a.chan, self.a.rate, self.a.shr, self.a.phr, self.a.fcs,
                                    self.a.tx_dbm, self.seq)
        self.log("tx", seq=self.seq, len=len(psdu), airtime_us=airtime)
        self.timers["tx_end"] = self.r.now + airtime

    def on_tx_end(self):
        self.leave_busy()

    def leave_busy(self):
        if self.window_open:
            self.set_state(LISTEN)
        else:
            self.set_state(OFF)
            if self.close_pending:
                self.close_pending = False
                self.log("window_close")

    def fire(self, name):
        if name.startswith("tx") and name != "tx_end":
            self.on_tx()
        else:
            getattr(self, "on_" + name)()

    # --- messages ---
    def on_sniff(self, res):
        self.log("anomaly", what="sniff_result_without_sniff")

    def on_frame(self, hdr, psdu):
        if self.state != LISTEN:
            self.log("anomaly", what="lock_while_not_listening", state=STATE_NAMES[self.state])
        self.held = dict(hdr, psdu=psdu)
        self.set_state(RECEIVE)
        self.log("rx_start", src=hdr["src"], frame=hdr["frame_id"], rx_dbm=f"{hdr['rx_dbm']:.1f}")

    def on_verdict(self, v):
        if self.held is None or self.held["frame_id"] != v["frame_id"]:
            self.log("verdict_ignored", frame=v["frame_id"])
            return
        h, self.held = self.held, None
        if v["ok"]:
            self.log("rx_ok", src=h["src"], frame=v["frame_id"], psdu=printable(h["psdu"]),
                     sinr_db=f"{v['sinr_db']:.1f}")
        else:
            self.log("rx_lost", src=h["src"], frame=v["frame_id"], cause=CAUSES[v["cause"]],
                     sinr_db=f"{v['sinr_db']:.1f}")
        self.leave_busy()

    # --- the loop ---
    def run(self):
        self.start()
        try:
            while True:
                if self.mop is not None:
                    self.mop.tick()
                self.r.idle(min(self.timers.values()) if self.timers else NEVER)
                msgs = self.r.wait()
                # messages first, then the timers due now, by deadline then name
                for kind, *m in msgs:
                    self.r.count(events=1)
                    getattr(self, "on_" + kind)(*m)
                while True:
                    due = sorted((t, n) for n, t in self.timers.items() if t <= self.r.now)
                    if not due:
                        break
                    name = due[0][1]
                    del self.timers[name]
                    self.r.count(timer_cause=CAUSE_RADIO if name.endswith("_end") else CAUSE_OTHER)
                    self.fire(name)
        except Closed as e:
            self.log("end", why=str(e).replace(" ", "_"))
            if self.mop is not None:
                self.mop.final(str(e))


class Mop:
    """The node's observation in MOP v1 text lines (--mop-period-ms): the
    Mesh Observation Protocol of the mesh testbed controller, "MOP1 {json}"
    in a console or log line, the first word of the line being the virtual
    µs as in the other lines. A report at the start, then one at the first
    wake at least a period after the previous one (the reports add no wake
    and change nothing in the run), and a last one at the end; events for
    the start, the first frame from each peer, anomalies and the end.

    The report is derived from this node's own log lines: counters per event,
    and per peer the frames received and lost with the last RSSI (links). A
    parent given with --mop-parent is reported as a configured relation: the
    demonstration protocol has no routing of its own."""

    COUNTED = ("tx", "lp_tx", "rx_ok", "rx_lost", "rx_aborted", "cca_busy", "tx_skipped", "window_open", "sniff",
               "sniff_detect", "sniff_none", "anomaly")

    def __init__(self, node):
        self.n = node
        self.counters = {}
        self.peers = {}             # src -> {"ok", "lost", "rssi", "t"}
        self.next_us = None
        self.state = "starting"

    def _print(self, msg):
        print(f"{self.n.r.now} {self.n.r.node_id} MOP1 " + json.dumps(msg, separators=(",", ":")), flush=True)

    def event(self, level, cat, text):
        self._print({"mop": 1, "type": "event", "seq": 0, "t_ms": self.n.r.local_us() // 1000,
                     "node": {"id": self.n.r.node_id}, "level": level, "cat": cat, "text": text})

    def report(self):
        r, a = self.n.r, self.n.a
        sniff = isinstance(self.n, SniffNode)
        detail = (f"sniff {a.sniff_us} us every {a.sniff_period_us // 1000} ms" if sniff and a.sniff_period_us
                  else "long-preamble sender" if sniff
                  else f"receive window {a.window_us // 1000} ms every {a.period_us // 1000} ms")
        if self.state == "not-running":
            detail = "end of the run"
        links = []
        for src, p in sorted(self.peers.items()):
            n = p["ok"] + p["lost"]
            links.append({"peer_id": src, "rssi_in": round(p["rssi"], 1), "quality": round(p["ok"] / n, 3) if n else 0,
                          "age_s": round((r.now - p["t"]) / 1e6, 1), "attrs": {"rx_ok": str(p["ok"]), "rx_lost": str(p["lost"])}})
        rel = []
        if a.mop_parent is not None:
            rel.append({"kind": "parent", "peer_id": a.mop_parent, "preferred": True, "attrs": {"source": "configured (--mop-parent)"}})
        self._print({"mop": 1, "type": "report", "seq": 0, "t_ms": r.local_us() // 1000,
                     "node": {"id": r.node_id, "name": f"ref-client {r.node_id}", "stack": "em7-ref-client",
                              "version": f"interface v{r.version}", "role": "sniff" if sniff else "window", "root": False,
                              "state": self.state, "detail": detail, "uptime_s": (r.now - r.start_us) // 10**6},
                     "links": links, "relations": rel, "counters": dict(sorted(self.counters.items())),
                     "values": {"channel": a.chan, "tx_dbm": a.tx_dbm, "rate_bps": a.rate, "drift_ppb": r.drift_ppb}})

    def on_log(self, event, kv):
        if event in self.COUNTED:
            self.counters[event] = self.counters.get(event, 0) + 1
        if event == "start":
            self.state = "operational"
            self.event("info", "radio", f"started: interface version {self.n.r.version}, clock drift {self.n.r.drift_ppb} ppb")
            self.report()
            self.next_us = self.n.r.now + self.n.a.mop_period_us
        elif event in ("rx_start", "rx_ok", "rx_lost"):
            src = kv["src"]
            p = self.peers.get(src)
            if p is None:
                p = self.peers[src] = {"ok": 0, "lost": 0, "rssi": 0.0, "t": self.n.r.now}
                if event == "rx_start":
                    self.event("info", "link", f"first frame from node {src} ({kv['rx_dbm']} dBm)")
            if event == "rx_start":
                p["rssi"], p["t"] = float(kv["rx_dbm"]), self.n.r.now
            else:
                p["ok" if event == "rx_ok" else "lost"] += 1
        elif event == "anomaly":
            self.event("warn", "anomaly", " ".join(f"{k}={v}" for k, v in kv.items()))

    def tick(self):
        if self.next_us is not None and self.n.r.now >= self.next_us:
            self.report()
            self.next_us = self.n.r.now + self.n.a.mop_period_us

    def final(self, why):
        self.state = "not-running"
        self.event("info", "run", f"ended: {why}")
        self.report()


class SniffNode(WindowNode):
    """Preamble sampling (§6.7): short sniffs every period, the radio off in
    between; frames sent with a wake-up preamble longer than the period.

    The server applies the radio states a sniff implies (listening from the
    "xn", the end state when nothing is detected, receiving on a lock), so
    this node reports only the others: TX and its end, the end of a
    reception, and OFF after a detection that did not lock."""

    def __init__(self, radio, args):
        super().__init__(radio, args)
        self.sniff_n = 0
        self.sniffing = None        # sniff_id of the sniff in progress
        self.lock_next = False      # the "xd" said a locked frame follows

    def start(self):
        self.set_state(OFF)
        self.log("start", seed=self.r.seed, drift_ppb=self.r.drift_ppb, start_us=self.r.start_us,
                 can_sniff=int(self.r.can_sniff))
        if self.a.sniff_period_us:
            self.timers["sniff"] = self.r.virtual_at(self.a.sniff_phase_us)
        for i, t in enumerate(self.a.lp_tx):
            self.timers[f"lp{i}"] = self.r.virtual_at(t)

    def fire(self, name):
        if name == "sniff":
            self.on_sniff_timer()
        elif name.startswith("lp") and name != "lp_end":
            self.on_lp()
        else:
            getattr(self, "on_" + name)()

    # --- timers ---
    def on_sniff_timer(self):
        """One sniff now, the next one a period later on the node's clock."""
        k = self.sniff_n
        self.sniff_n += 1
        self.timers["sniff"] = self.r.virtual_at(self.a.sniff_phase_us + self.sniff_n * self.a.sniff_period_us)
        if self.state in (TX, RECEIVE):
            self.log("sniff_skipped", id=k, state=STATE_NAMES[self.state])
            return
        self.r.sniff(self.a.chan, self.a.sniff_us, k, lock=self.a.lock, end_state=OFF)
        self.sniffing = k
        self.state = LISTEN                     # applied by the server from the "xn"
        self.log("sniff", id=k, us=self.a.sniff_us)

    def on_lp(self):
        if self.state == TX:
            self.log("tx_skipped", reason="already_sending")
            return
        if self.held is not None:
            self.log("rx_aborted", src=self.held["src"], frame=self.held["frame_id"])
            self.held = None                    # half duplex: no verdict will come
        if self.sniffing is not None:
            self.log("sniff_ended", id=self.sniffing, why="tx")   # no "xd" will come
            self.sniffing = None
        self.seq += 1
        psdu = f"L{self.r.node_id}S{self.seq}".encode().ljust(self.a.payload_len, b".")
        self.set_state(TX)
        total = self.r.send_lp_frame(self.a.lp_preamble_us, psdu, self.a.chan, self.a.rate, self.a.shr, self.a.phr,
                                     self.a.fcs, self.a.tx_dbm, self.seq)
        self.log("lp_tx", seq=self.seq, len=len(psdu), preamble_us=self.a.lp_preamble_us, total_us=total)
        self.timers["lp_end"] = self.r.now + total

    def on_lp_end(self):
        self.set_state(OFF)

    # --- messages ---
    def on_sniff(self, res):
        if self.sniffing is None or res["sniff_id"] != self.sniffing:
            self.log("anomaly", what="unexpected_sniff_result", id=res["sniff_id"])
            return
        self.sniffing = None
        if not res["detected"]:
            self.log("sniff_none", id=res["sniff_id"], rssi_dbm=f"{res['rssi_dbm']:.2f}")
            if self.held is None:
                self.state = OFF                # the request's end state, applied by the server
            return
        self.log("sniff_detect", id=res["sniff_id"], src=res["src"], frame=res["frame_id"], locked=res["locked"],
                 t_us=res["t_us"], rssi_dbm=f"{res['rssi_dbm']:.1f}", sinr_db=f"{res['sinr_db']:.1f}")
        if res["locked"] == LOCKED:
            self.lock_next = True               # its "xx" is the next message
        elif res["locked"] == NOT_LOCKED and self.held is None:
            self.set_state(OFF)                 # too late to receive it: the radio goes off
        # RECEIVING: the frame is held already, its verdict will come

    def on_frame(self, hdr, psdu):
        if self.lock_next:
            # Locked by the sniff: the server has put the radio in RECEIVE
            self.lock_next = False
            self.held = dict(hdr, psdu=psdu)
            self.state = RECEIVE
            self.log("rx_start", src=hdr["src"], frame=hdr["frame_id"], rx_dbm=f"{hdr['rx_dbm']:.1f}", via="sniff",
                     lp=int(bool(hdr["flags"] & FRAME_LONG_PREAMBLE)))
            return
        super().on_frame(hdr, psdu)

    def leave_busy(self):
        # A frame that started during a sniff still in progress: back to the
        # sniff, which goes on (a report of OFF would end it, §6.7)
        self.set_state(LISTEN if self.sniffing is not None else OFF)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sock", required=True, help="the server's socket path")
    ap.add_argument("--id", type=int, required=True, help="node ID (0-4095)")
    ap.add_argument("--period-ms", type=float, default=1000)
    ap.add_argument("--window-ms", type=float, default=30)
    ap.add_argument("--phase-ms", type=float, default=0, help="window offset in the period")
    ap.add_argument("--tx", type=float, action="append", default=[], help="send at this offset in the window (ms)")
    ap.add_argument("--chan", type=int, default=0)
    ap.add_argument("--tx-dbm", type=int, default=10)
    ap.add_argument("--rate", type=int, default=50000, help="bit/s")
    ap.add_argument("--shr", type=int, default=8, help="preamble + sync word, bytes")
    ap.add_argument("--phr", type=int, default=2)
    ap.add_argument("--fcs", type=int, default=2)
    ap.add_argument("--payload-len", type=int, default=16)
    ap.add_argument("--cca-dbm", type=float, default=-85, help="busy at or above this energy")
    ap.add_argument("--no-cca", dest="cca", action="store_false")
    ap.add_argument("--if-version", type=int, default=IF_VERSION, help="interface version to ask for")
    m = ap.add_argument_group("observation (MOP v1 lines for the mesh testbed controller)")
    m.add_argument("--mop-period-ms", type=float, default=0, help="print MOP1 report lines this often (0: none)")
    m.add_argument("--mop-parent", type=int, default=None, help="report this node as a configured parent")
    g = ap.add_argument_group("preamble sampling (SniffNode, interface version 2)")
    g.add_argument("--sniff-period-ms", type=float, default=0, help="sniff every period (0: no sniffing)")
    g.add_argument("--sniff-us", type=int, default=1000, help="length of each sniff")
    g.add_argument("--sniff-phase-ms", type=float, default=0, help="time of the first sniff on the node's clock")
    g.add_argument("--no-lock", dest="lock", action="store_false", help="sniff without SNIFF_LOCK")
    g.add_argument("--lp-tx", type=float, action="append", default=[],
                   help="send a frame with a wake-up preamble at this time on the node's clock (ms)")
    g.add_argument("--lp-preamble-ms", type=float, default=110, help="wake-up preamble before each --lp-tx frame")
    a = ap.parse_args()
    a.period_us, a.window_us, a.phase_us = (int(x * 1000) for x in (a.period_ms, a.window_ms, a.phase_ms))
    a.tx = [int(x * 1000) for x in a.tx]
    a.sniff_period_us, a.sniff_phase_us = int(a.sniff_period_ms * 1000), int(a.sniff_phase_ms * 1000)
    a.lp_tx = [int(x * 1000) for x in a.lp_tx]
    a.lp_preamble_us = int(a.lp_preamble_ms * 1000)
    a.mop_period_us = int(a.mop_period_ms * 1000)
    radio = RadioIf(a.sock, a.id, a.if_version)
    node = SniffNode(radio, a) if (a.sniff_period_us or a.lp_tx) else WindowNode(radio, a)
    node.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
