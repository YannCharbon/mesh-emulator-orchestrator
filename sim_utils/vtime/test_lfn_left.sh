#!/bin/bash
# An LFN's radio leaving a reception without the server's verdict (central
# medium, docs/MEDIUM.md §3 "left" and §6; the radio's rf_medium_drop_held()),
# with real nodes. Border router 0, FFN router 1 (no LFN parenting), LFN 2
# under the border router; every link heard by every node (0-1 at -55 dBm,
# 0-2 and 1-2 at -50 dBm). The router sends UDP_DSTS requests a second to the
# border router, UDP_GAP_MS apart, from 150 s: frames on the border router's
# channel that the LFN hears. The border router sends a request to the LFN
# every 5 s, the LFN one to the border router every 2 s; both echo. From
# CUT_AT s, the LFN's frames no longer reach the border router (--vtime-cut
# 2:0): each of its uplinks waits the whole ACK wait (10.3 ms), and when one
# of the router's frames starts in that wait, the LFN's radio locks onto it
# and the MAC's ACK timeout turns the radio off during the frame.
#
# By the stack's design a closing receive window does not cut a frame: the
# MAC defers the close while the radio receives (PHY_EXTENSION_READ_RX_ACTIVE,
# mac_mlme_rx_window()). The test checks that, and the path that does leave
# a frame without a verdict: an ACK timeout during an overheard frame (the
# MAC's no-ACK confirm turns the radio off, mac_data_poll_radio_disable_check()).
#
# Checks, from the medium log (--medium-log), the radios' logs and the power
# ledger (--power-log):
#   - every node: each frame the radio took is a frame the server locked it
#     onto, each loss line has the server's outcome, sender and power, and
#     the radio's exit counters equal the server's outcomes (as
#     test_medium_radio.sh); nothing refused or ignored;
#   - the LFN left at least MIN_LEFT frames without a verdict; each "left"
#     row of the server is a "was lost: radio off" (or another drop) line of
#     the LFN for that frame, before the frame's end;
#   - the power ledger: every frame the LFN locked onto is one "radio
#     receive" interval, from the frame's start to its end (verdict) or to the
#     drop (left), and the LFN has no other receive time; after a drop by
#     "radio off" the radio is off (or, in the same microsecond, on again for
#     the MAC's next queued frame);
#   - the MAC: every drop is at an ACK timeout (the MAC's no-ACK confirm at the
#     same microsecond; a drop elsewhere, e.g. a queued uplink turning the
#     radio off while a deferred window close waits for a frame, is a MAC
#     fault), and the frame is sent again (same MAC handle) unless
#     that was its last attempt;
#   - no stuck RX: the LFN received frames after its last drop;
#   - window closes during a frame: at least MIN_DEFERRED frames that started
#     inside one of the LFN's windows ended after its scheduled close; none
#     of them was left without a verdict (the close waited for the frame);
#   - traffic before the cut: the LFN's downlinks and uplinks arrived (at
#     least MIN_DELIVERY of each, from 120 s to CUT_AT - 10 s).
# Run of DUR (900) virtual seconds, about 5 s of wall time.
#
# usage: test_lfn_left.sh <bindir> <workdir(holds examples/)> <outdir> [profile]
# Environment: WSSIM_SEED (1), DUR (900), CUT_AT (450), UDP_DSTS (16),
# UDP_GAP_MS (60), MIN_LEFT (3), MIN_DEFERRED (1), MIN_DELIVERY (0.95),
# SIM_OPTS (more wssimserver options; --medium=central is always given,
# after them). TUN is always none. Profile: examples/power/efr32fg25-lfn.ini.
# Exit status 0 if every check passes.
set -u
[ $# -ge 3 ] || { echo "usage: test_lfn_left.sh <bindir> <workdir> <outdir> [profile]" >&2; exit 2; }
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
PROFILE=${4:-$WORK/examples/power/efr32fg25-lfn.ini}
DUR=${DUR:-900}; CUT_AT=${CUT_AT:-450}; UDP_DSTS=${UDP_DSTS:-16}; UDP_GAP_MS=${UDP_GAP_MS:-60}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_left: $OUT is not empty" >&2; exit 1; }
printf '0,1:-55\n0,2:-50\n1,2:-50\n' > "$OUT/topology.in"
BR_ADDR=fd12:3456::1615:16ff:fe17:1819
LFN_ADDR=fd12:3456::302:304:506:2
DSTS=; for _ in $(seq "$UDP_DSTS"); do DSTS+=" -o test_udp_dst=$BR_ADDR"; done

TUN=none CONF=lfn TOPO="$OUT/topology.in" CUT="2:0:$CUT_AT" POWER=$PROFILE POWER_LOG=1 \
    SIM_OPTS="${SIM_OPTS:-} --medium=central --medium-log=$OUT/run/medium.log" \
    BR_OPTS="-o test_udp_echo=true -o test_udp_dst=$LFN_ADDR -o test_udp_period_s=5 -o test_udp_start_s=60" \
    NODE_OPTS_1="-o device_type=ffn -o enable_lfn=false $DSTS -o test_udp_gap_ms=$UDP_GAP_MS -o test_udp_period_s=1 -o test_udp_start_s=150" \
    NODE_OPTS_2="-o device_type=lfn -o enable_lfn=false -o test_udp_echo=true -o test_udp_dst=$BR_ADDR -o test_udp_period_s=2 -o test_udp_start_s=70" \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT/run" "$DUR" > "$OUT/run.txt" 2>&1 ||
    { echo "test_lfn_left: run failed ($OUT/run.txt)"; exit 1; }

PYTHONPATH=$HERE python3 - "$OUT/run" "$DUR" "$CUT_AT" "${MIN_LEFT:-3}" "${MIN_DEFERRED:-1}" "${MIN_DELIVERY:-0.95}" <<'EOF'
import bisect, collections, re, sys
from lfn_lib import Checks, lines, udp, BR_ADDR, lfn_addr
out, dur, cut_at = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
min_left, min_deferred, min_delivery = int(sys.argv[4]), int(sys.argv[5]), float(sys.argv[6])
LFN = 2
PENDING_HOLD_US = 30000     # lfn_lib.PENDING_HOLD_S
ck = Checks('test_lfn_left')
us = lambda t: int(round(t * 1e6))

if not any('medium: central' in l for l in open(f'{out}/simserver.log')):
    print('FAIL the server did not run the central medium'); sys.exit(1)

# The server: outcome of (frame, receiver)
rows = {}
for l in open(f'{out}/medium.log'):
    t_end, src, dst, fid, outcome, dbm, sinr = l.split()
    rows[(int(fid), int(dst))] = (int(t_end), int(src), outcome, float(dbm))
LOCKED = {'ok', 'noise', 'interference', 'captured', 'left', 'tx_abort'}
DROP = re.compile(r'Packet from node (\d+) was lost: (.*) \(RSSI (\S+) dBm.* frame (\d+)\)')
COUNTERS = re.compile(r'rf medium: (\d+) received, lost (\d+) noise, (\d+) interference, (\d+) captured, '
                      r'(\d+) without verdict; (\d+) refused, (\d+) verdicts and (\d+) frames ignored')

# Every node: the radio against the server (as test_medium_radio.sh)
took_lfn = drop_lfn = None
for node, name in {0: 'br', 1: 'node1', 2: 'node2'}.items():
    took, lost, counters = {}, {}, None
    for t, m in lines(f'{out}/{name}.log'):
        if r := re.search(r'Receiving \.\.\. .* frame=(\d+)', m):
            took[int(r[1])] = us(t)
        elif r := DROP.search(m):
            lost[int(r[4])] = (int(r[1]), r[2], float(r[3]), us(t))
        elif r := COUNTERS.search(m):
            counters = [int(x) for x in r.groups()]
    mine = {fid: r for (fid, dst), r in rows.items() if dst == node}
    locked = {fid for fid, r in mine.items() if r[2] in LOCKED}
    tail = {fid for fid, t in took.items() if fid not in mine and t > (dur - 1) * 1e6}
    ck(set(took) - tail == locked, f'{name}: {len(locked)} frames locked, {len(took)} taken '
       f'(differences {sorted((set(took) - tail) ^ locked)[:5]})')
    def cause(text):
        if text.startswith('captured by'):
            return 'captured'
        return text if text in ('noise', 'interference') else 'left'
    bad = [(fid, v) for fid, v in lost.items() if fid not in tail and
           (fid not in mine or mine[fid][1] != v[0] or abs(mine[fid][3] - v[2]) > 0.01 or
            cause(v[1]) != {'tx_abort': 'left'}.get(mine[fid][2], mine[fid][2]))]
    ck(not bad, f'{name}: {len(lost)} loss lines, each with the server\'s outcome, sender and power {bad[:3]}')
    n = collections.Counter(r[2] for r in mine.values())
    srv = [n['ok'], n['noise'], n['interference'], n['captured'], n['left'] + n['tx_abort'], 0, 0, 0]
    ck(counters == srv, f'{name}: radio counters {counters} = server {srv} '
       '(received, noise, interference, captured, without verdict, refused, ignored)')
    if node == LFN:
        took_lfn, drop_lfn, mine_lfn, tail_lfn = took, lost, mine, tail

# The LFN's log: windows, ACK timeouts, MAC handles, transmissions
windows = []          # (open us, close us)
no_ack = set()
handle_at = {}        # µs of a "waiting_ack=0" line -> handle
resent = collections.defaultdict(list)    # handle -> µs of each "waiting_ack=1" (a frame sent)
for t, m in lines(f'{out}/node2.log'):
    if r := re.search(r'(?:LFN window open: slot|LFN broadcast window open|LFN discovery window open).* for (\d+) us', m):
        windows.append((us(t), us(t) + int(r[1])))
    elif 'LFN window held for a pending frame' in m:
        windows.append((us(t), us(t) + PENDING_HOLD_US))
    elif m.endswith(' no_ack'):
        no_ack.add(us(t))
    elif r := re.search(r'waiting_ack=(\d), tx_completed=\d, handle=(\d+)', m):
        if r[1] == '0':
            handle_at[us(t)] = int(r[2])
        else:
            resent[int(r[2])].append(us(t))
windows.sort()
wopen = [w[0] for w in windows]

# Left without a verdict at the LFN
left = {fid: r for fid, r in mine_lfn.items() if r[2] in ('left', 'tx_abort')}
ck(len(left) >= min_left, f'LFN: {len(left)} frames left without a verdict (at least {min_left}); '
   f'server outcomes {collections.Counter(r[2] for r in left.values())}')
bad = [fid for fid, r in left.items()
       if fid not in drop_lfn or not drop_lfn[fid][3] < r[0] or not took_lfn.get(fid, r[0]) <= drop_lfn[fid][3]]
ck(not bad, f'LFN: each frame left is a drop line of its radio during the frame {bad[:3]}')
causes = collections.Counter(drop_lfn[fid][1] for fid in left if fid in drop_lfn)
ck(set(causes) <= {'radio off', 'radio left RX', 'no verdict (backup timer)'}, f'LFN: drop causes {dict(causes)}')
ck(not any(r[2] == 'tx_abort' for r in left.values()), 'LFN: no half-duplex abort (tx_abort)')

# The power ledger
radio = []            # (start, end, state) of the LFN's radio
for l in open(f'{out}/power_log.txt'):
    p = l.split()
    if len(p) >= 5 and p[2] == str(LFN) and p[3] == 'radio':
        radio.append((round(float(p[0])), round(float(p[1])), p[4]))
radio.sort()
recv = [(s, e) for s, e, st in radio if st == 'receive']
starts = {s: e for s, e in recv}
expect = {}
for fid, r in mine_lfn.items():
    if r[2] not in LOCKED or fid in tail_lfn or r[2] == 'captured':
        continue
    expect[took_lfn[fid]] = drop_lfn[fid][3] if r[2] in ('left', 'tx_abort') else r[0]
# a capture continues the same receive interval with the new frame
caps = {took_lfn[fid] for fid, r in mine_lfn.items() if r[2] == 'captured'}
bad = [(s, e, starts.get(s)) for s, e in expect.items() if starts.get(s) != e]
ck(not bad, f'ledger: {len(expect)} frames locked, each a receive interval from its start to its verdict '
   f'or to the drop {bad[:3]}')
extra = [(s, e) for s, e in recv if s not in expect and s not in caps and s < (dur - 1) * 1e6]
ck(not extra, f'ledger: no other receive time at the LFN ({len(recv)} intervals) {extra[:3]}')
after = {s: st for (s, e, st) in radio}
ends = {e: st for (s, e, st) in radio}
# after a drop by "radio off" the radio is off; the MAC may turn it on in the
# same microsecond for its next queued frame (a zero-length off interval is
# not logged): then it is not receiving
bad = [fid for fid in left if drop_lfn.get(fid, (0, ''))[1] == 'radio off'
       and after.get(drop_lfn[fid][3]) not in ('off', 'idle', 'cca', 'listen', 'tx')]
ck(not bad, f'ledger: the radio is off (or starts a transmission) after each drop by "radio off" {bad[:3]}')
rx_us = sum(e - s for s, e in recv)
left_us = sum(drop_lfn[f][3] - took_lfn[f] for f in left if f in drop_lfn)
left_air = sum(r[0] - took_lfn[f] for f, r in left.items() if f in took_lfn)
ck(True, f'ledger: LFN receive time {rx_us / 1e6:.3f} s; the frames left {left_us / 1e3:.1f} ms of '
   f'{left_air / 1e3:.1f} ms of airtime')

# The MAC: the drops at an ACK timeout, and the frames sent again
at_ack = [fid for fid in left if fid in drop_lfn and drop_lfn[fid][3] in no_ack]
again = 0
for fid in at_ack:
    t = drop_lfn[fid][3]
    h = handle_at.get(t)
    if h is not None and any(x > t for x in resent[h]):
        again += 1
ck(len(at_ack) == len(left), f'MAC: {len(at_ack)} of {len(left)} drops at the MAC\'s ACK timeout (no_ack at the '
   'same microsecond)')
ck(again >= len(at_ack) - 2, f'MAC: {again} of {len(at_ack)} frames sent again after the drop (same handle; the '
   'others were their last attempt)')

# No stuck RX
last = max((drop_lfn[f][3] for f in left if f in drop_lfn), default=0)
later = sum(1 for fid, r in mine_lfn.items() if r[2] == 'ok' and took_lfn.get(fid, 0) > last)
ck(later > 0, f'LFN: {later} frames received after its last drop ({last / 1e6:.1f} s)')

# Window closes during a frame
deferred = []
for fid, r in mine_lfn.items():
    s = took_lfn.get(fid)
    if s is None or r[2] not in LOCKED:
        continue
    i = bisect.bisect_right(wopen, s) - 1
    if i >= 0 and windows[i][0] <= s <= windows[i][1] < r[0]:
        deferred.append((fid, r[2], r[0] - windows[i][1]))
ck(len(deferred) >= min_deferred and not any(o in ('left', 'tx_abort') for _, o, _ in deferred),
   f'deferred window closes: no frame dropped while the MAC waited for it to end; {len(deferred)} frames '
   f'ended after their window\'s close (up to '
   f'{max((d for *_, d in deferred), default=0) / 1e3:.1f} ms later), none left: '
   f'{dict(collections.Counter(o for _, o, _ in deferred))}')

# Traffic before the cut
br, lfn = udp(f'{out}/br.log'), udp(f'{out}/node2.log')
# (the border router's source address without a TUN is not BR_ADDR: any peer)
for what, tx, rx, peer, dst in (('downlinks', br, lfn, None, lfn_addr(LFN)),
                                ('uplinks', lfn, br, lfn_addr(LFN), BR_ADDR)):
    q = [(t, p) for t, a, p in tx['tx'] if p.startswith('Q') and 120 < t < cut_at - 10 and a == dst]
    got = {p for t, a, p in rx['rx'] if peer is None or a == peer}
    n = sum(1 for t, p in q if p in got)
    ck(q and n >= min_delivery * len(q), f'{what} before the cut: {n} of {len(q)}')
sys.exit(ck.done())
EOF
