#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Reference client of the emulator's stack-neutral radio interface.

Written from docs/RADIO_INTERFACE.md (interface version 1) alone: it uses
the Python standard library only, and no emulator or Nanostack code. One
process is one node.

Two layers:

- RadioIf: the interface itself (connect, hello, welcome, wakes, frames,
  verdicts, radio-state reports, CCA, idle, quit). A stack's own client
  does the same in its own language.
- WindowNode: a small demonstration protocol on top of it. Every node has a
  receive window of --window-ms at the start of each period of --period-ms
  (on its own clock), sleeps with its radio off in between, and sends its
  frames at the offsets given with --tx, inside the window. CCA before each
  frame unless --no-cca; a busy channel skips the frame (no backoff).

Output: one line per event on stdout, "<virtual µs> <node> <event> k=v ...",
read by test_ref_client.py.

    ref_client.py --sock /tmp/sock --id 2 --tx 10 --window-ms 30
"""
import argparse
import socket
import struct
import sys

IF_VERSION = 1

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

HELLO_VTIME, HELLO_RADIO_IF = 0x0001, 0x0002
WELCOME_POWER, WELCOME_MEDIUM = 0x0001, 0x0002
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

    def __init__(self, sock_path, node_id):
        self.node_id = node_id
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.s.connect(sock_path)
        self.s.send(HELLO.pack(b"xh", node_id, HELLO_VTIME | HELLO_RADIO_IF, IF_VERSION))
        msg = self._recv()
        if msg[:2] != b"xw" or len(msg) != WELCOME.size:
            raise RuntimeError(f"expected a welcome, got {msg[:2]!r} ({len(msg)} bytes)")
        _, version, self.flags, self.seed, self.drift_ppb, _, self.start_us = WELCOME.unpack(msg)
        if version != IF_VERSION:
            raise RuntimeError(f"welcome for interface version {version}, expected {IF_VERSION}")
        self.now = self.start_us
        self.power = bool(self.flags & WELCOME_POWER)
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
        ("frame", header dict, psdu) or ("verdict", dict). Raises Closed."""
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

    def log(self, event, **kv):
        print(f"{self.r.now} {self.r.node_id} {event}" + "".join(f" {k}={v}" for k, v in kv.items()), flush=True)

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

    # --- messages ---
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
                self.r.idle(min(self.timers.values()) if self.timers else NEVER)
                msgs = self.r.wait()
                # messages first, then the timers due now, by deadline then name
                for kind, *m in msgs:
                    self.r.count(events=1)
                    if kind == "frame":
                        self.on_frame(*m)
                    else:
                        self.on_verdict(*m)
                while True:
                    due = sorted((t, n) for n, t in self.timers.items() if t <= self.r.now)
                    if not due:
                        break
                    name = due[0][1]
                    del self.timers[name]
                    self.r.count(timer_cause=CAUSE_RADIO if name == "tx_end" else CAUSE_OTHER)
                    if name.startswith("tx") and name != "tx_end":
                        self.on_tx()
                    else:
                        getattr(self, "on_" + name)()
        except Closed as e:
            self.log("end", why=str(e).replace(" ", "_"))


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
    a = ap.parse_args()
    a.period_us, a.window_us, a.phase_us = (int(x * 1000) for x in (a.period_ms, a.window_ms, a.phase_ms))
    a.tx = [int(x * 1000) for x in a.tx]
    WindowNode(RadioIf(a.sock, a.id), a).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
