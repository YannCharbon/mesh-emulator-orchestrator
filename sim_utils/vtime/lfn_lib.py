# SPDX-License-Identifier: Apache-2.0
"""Helpers shared by the LFN tests: log lines, checks, and the frames of a
run's frames.txt (wssimserver --vtime-frames, run.sh FRAMES=1).

Used by the test_lfn_*.sh scripts (python3 - ... with sys.path set to this
directory); the standard library only.
"""
import re

T0 = 1700000000.0
ANSI = re.compile(r'\x1b\[[0-9;]*m')


class Checks:
    """'ok   what' or 'FAIL what' per check; the failures are kept."""

    def __init__(self, name):
        self.name = name
        self.fails = []

    def __call__(self, cond, what):
        print(('ok   ' if cond else 'FAIL ') + what)
        if not cond:
            self.fails.append(what)
        return bool(cond)

    def info(self, what):
        print('info ' + what)

    def done(self):
        print(f'{self.name}: ' + ('PASSED' if not self.fails else f'FAILED ({len(self.fails)})'))
        return 1 if self.fails else 0


def lines(path, offset=0.0):
    """(seconds since the run's start, message without colours) per line.
    offset is added to the time of every line (an instance started later
    prints its first run's lines at time 0: see restart_offset())."""
    with open(path, errors='replace') as f:
        for line in f:
            t, _, msg = line.partition(' ')
            try:
                yield float(t) - T0 + offset, ANSI.sub('', msg.rstrip('\n'))
            except ValueError:
                pass


def mac(i):
    return f'01:02:03:04:05:06:{i >> 8:02x}:{i & 0xff:02x}'


def lfn_addr(i):
    return f'fd12:3456::302:304:506:{i:x}'


BR_ADDR = 'fd12:3456::1615:16ff:fe17:1819'


def eui_node(e):
    p = e.split(':')
    return int(p[-2] + p[-1], 16)


def udp(path):
    """test-udp lines of a log: {'tx': [(t, peer, payload)], 'rx': [...]}"""
    d = {'tx': [], 'rx': []}
    for t, m in lines(path):
        r = re.search(r'test-udp (tx to|rx from) (\S+): (\S+ \d+)', m)
        if r:
            d['tx' if r.group(1) == 'tx to' else 'rx'].append((t, r.group(2), r.group(3)))
    return d


def exchange(tx_side, rx_side, peer_addr, t_from, t_to, dst=None):
    """Requests "Q n" sent by tx_side (to dst, if given) between t_from and
    t_to: (requests, received by rx_side from peer_addr, echoes back).
    Each is a list of (t, payload)."""
    q = [(t, p) for t, a, p in tx_side['tx'] if p.startswith('Q') and t_from < t < t_to and (dst is None or a == dst)]
    got = {p for t, a, p in rx_side['rx'] if peer_addr is None or a == peer_addr}
    back = {p.replace('R', 'Q', 1) for t, a, p in tx_side['rx'] if p.startswith('R')}
    return q, [x for x in q if x[1] in got], [x for x in q if x[1] in back]


# --- Frames (IEEE 802.15.4-2015 MAC header and auxiliary security header)

def _pan_ids(version, dst_mode, src_mode, comp):
    """(destination PAN ID present, source PAN ID present): 802.15.4-2015
    table 7-2 for frame version 2, the 2006 rule before it."""
    if version < 2:
        if dst_mode and src_mode:
            return True, not comp
        return bool(dst_mode), bool(src_mode)
    if not dst_mode and not src_mode:
        return bool(comp), False
    if dst_mode and not src_mode:
        return not comp, False
    if src_mode and not dst_mode:
        return False, not comp
    if dst_mode == 3 and src_mode == 3:
        return not comp, False
    return True, not comp


def parse_frame(psdu):
    """The fields of a frame the tests use, or None if too short:
    type, seq (None if suppressed), dst (hex, or None), src, security
    (bool), level, key_index (None without key ID mode 1), counter."""
    b = bytes.fromhex(psdu) if isinstance(psdu, str) else psdu
    if len(b) < 3:
        return None
    fcf = b[0] | b[1] << 8
    f = {'type': fcf & 7, 'security': bool(fcf & 8), 'ar': bool(fcf & 0x20), 'version': (fcf >> 12) & 3,
         'seq': None, 'dst': None, 'src': None, 'level': None, 'key_index': None, 'counter': None}
    comp, seq_supp = bool(fcf & 0x40), bool(fcf & 0x100)
    dst_mode, src_mode = (fcf >> 10) & 3, (fcf >> 14) & 3
    i = 2
    if not seq_supp:
        f['seq'] = b[i]
        i += 1
    dpan, span = _pan_ids(f['version'], dst_mode, src_mode, comp)
    alen = {0: 0, 2: 2, 3: 8}
    if dpan:
        i += 2
    if dst_mode:
        f['dst'] = b[i:i + alen[dst_mode]][::-1].hex()
        i += alen[dst_mode]
    if span:
        i += 2
    if src_mode:
        f['src'] = b[i:i + alen[src_mode]][::-1].hex()
        i += alen[src_mode]
    if f['security']:
        if i >= len(b):
            return None
        sc = b[i]
        i += 1
        f['level'] = sc & 7
        kmode = (sc >> 3) & 3
        if not sc & 0x20:
            f['counter'] = int.from_bytes(b[i:i + 4], 'little')
            i += 4
        if kmode == 1:
            f['key_index'] = b[i]
        elif kmode in (2, 3):
            i += 4 if kmode == 2 else 8
            f['key_index'] = b[i]
    return f


def frames(path):
    """(time s, sender node, channel, fields) per line of frames.txt."""
    with open(path) as fh:
        for line in fh:
            p = line.split()
            if len(p) < 4:
                continue
            f = parse_frame(p[3])
            if f is not None:
                yield int(p[0]) / 1e6, int(p[1]), int(p[2]), f


# --- Frames an LFN received against its listen windows

ACK_WAIT_S = 0.0105     # the MAC's wait for an ACK after its frame (TX_done to no_ack:
                        # 10.3 ms in mode 3), when the log shows no "no_ack"
ACK_ON_S = 0.010        # the LFN stays on this long after a frame it received (its ACK)


def lfn_reception(path, t_from=0.0):
    """Every frame an LFN's radio received (from its log), against its
    windows. Airtimes are those the emulated PHY used ("SIG TX in N us",
    "SIG RX in N us": the configured rate and overheads).

    Returns a dict: rx (frame start times), acks (frames received while the
    LFN waits for the ACK of a frame it sent, the ACK itself included: from
    the end of that frame (not one of its own ACKs) for the MAC's ACK wait,
    the longest "no_ack" after a frame's end in the log, else ACK_WAIT_S),
    overheard (frames starting within
    ACK_ON_S of the end of a frame it received, while it sends its ACK),
    outside (the other frames that started outside every window), windows."""
    import bisect
    win, rx, tx = [], [], []
    pend_rx = pend_tx = None
    ack_tx = False
    no_ack = []
    for t, m in lines(path):
        if 'window open: slot' in m or 'discovery window open' in m:
            r = re.search(r'for (\d+) us', m)
            if r:
                win.append((t, int(r.group(1)) / 1e6))
        elif m.startswith('Transmitting ...'):
            pend_tx = [t, None, ack_tx]
            ack_tx = False
            tx.append(pend_tx)
        elif m.startswith('SIG TX in') and pend_tx is not None:
            pend_tx[1] = pend_tx[0] + int(re.search(r'in (\d+) us', m).group(1)) / 1e6
            pend_tx = None
        elif m.startswith('Receiving ...'):
            pend_rx = [t, None]
            rx.append(pend_rx)
        elif m.startswith('SIG RX in') and pend_rx is not None:
            pend_rx[1] = pend_rx[0] + int(re.search(r'in (\d+) us', m).group(1)) / 1e6
            pend_rx = None
        elif 'ACK TX' in m:
            ack_tx = True       # the next "Transmitting" is the ACK...
        elif m.endswith(' no_ack'):
            no_ack.append(t)
        elif 'TX_done' in m:
            ack_tx = False      # ...unless it was abandoned (CCA busy)
    win.sort()
    wt = [w[0] for w in win]
    # frames whose airtime line is missing: no end
    txe = sorted((s, e) for s, e, a in tx if e is not None and not a)
    txs = [x[1] for x in txe]
    waits = []
    for t in no_ack:
        i = bisect.bisect_right(txs, t) - 1
        if i >= 0 and t - txs[i] < 0.1:
            waits.append(t - txs[i])
    ack_wait = max(waits) if waits else ACK_WAIT_S
    rxe = sorted((s, e) for s, e in rx if e is not None)
    rxends = sorted(e for s, e in rxe)

    def in_window(t):
        i = bisect.bisect_right(wt, t + 1e-6) - 1
        return i >= 0 and win[i][0] - 1e-6 <= t <= win[i][0] + win[i][1] + 1e-6

    def is_ack(t):
        i = bisect.bisect_right(txs, t + 1e-6) - 1
        return i >= 0 and -1e-6 <= t - txs[i] <= ack_wait + 1e-6

    def overheard(t):
        i = bisect.bisect_right(rxends, t) - 1
        return i >= 0 and t - rxends[i] <= ACK_ON_S

    res = {'rx': [], 'acks': [], 'overheard': [], 'outside': [], 'windows': len(win), 'ack_wait': ack_wait}
    for s, e in rx:
        if s < t_from:
            continue
        res['rx'].append(s)
        if in_window(s):
            continue
        if is_ack(s):
            res['acks'].append(s)
        elif overheard(s):
            res['overheard'].append(s)
        else:
            res['outside'].append(s)
    return res
