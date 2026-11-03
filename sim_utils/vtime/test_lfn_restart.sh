#!/bin/bash
# Restarts without a key change, in virtual time: border router 0, FFN router
# 1 (LFN parent), LFN 2 hearing only the router. The router restarts at
# ROUTER_AT s, the LFN at LFN_AT s (run.sh RESTART: the program is stopped
# outright and a new instance starts with the same key storage; no key
# changes meanwhile: the LGTK and GTKs are those of the start). Test traffic:
# the border router sends a UDP request to the LFN every BR_PERIOD s, the LFN
# one to the border router every LFN_PERIOD s; each side echoes.
#
# Checks:
#   - the LFN joined, the router and the LFN came back after their restart
#     (operational again);
#   - no frame was dropped as a replay (MLME_COUNTER_ERROR in any log, other
#     than for the MAC retry of a frame already received: from frames.txt);
#   - no frame counter reused under the same key: for each sender and key
#     index, a new instance's counters are above all those of its previous
#     instance, and no counter carries two different frames (other sequence
#     number or destination; a MAC retry keeps both); from frames.txt;
#   - traffic resumes: every request sent from RECOVER_S after each restart
#     to the next restart (or to the end, less 15 s) arrived and was echoed,
#     both ways; the time to the first request that got through is printed.
#
# usage: test_lfn_restart.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: DUR (1800), WSSIM_SEED (1), TUN (none), ROUTER_AT (600),
# LFN_AT (1200), RECOVER_S (300), BR_PERIOD (37), LFN_PERIOD (53), JOIN_MAX
# (300), SIM_OPTS (--drift-random=20), LFN_EXTRA, ROUTER_EXTRA, KEEP (1: keep
# the run directory).
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
DUR=${DUR:-1800}; TUN=${TUN:-none}; ROUTER_AT=${ROUTER_AT:-600}; LFN_AT=${LFN_AT:-1200}
RECOVER_S=${RECOVER_S:-300}; BR_PERIOD=${BR_PERIOD:-37}; LFN_PERIOD=${LFN_PERIOD:-53}
JOIN_MAX=${JOIN_MAX:-300}; SIM_OPTS=${SIM_OPTS---drift-random=20}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_restart: $OUT is not empty" >&2; exit 1; }
printf '0,1:-50\n1,2:-50\n' > "$OUT.topo"
BR_ADDR=fd12:3456::1615:16ff:fe17:1819
export NODE_OPTS_1="-o device_type=ffn -o enable_lfn=true ${ROUTER_EXTRA:-}"
export NODE_OPTS_2="-o device_type=lfn -o enable_lfn=false -o test_udp_echo=true -o test_udp_dst=$BR_ADDR -o test_udp_period_s=$LFN_PERIOD -o test_udp_start_s=70 ${LFN_EXTRA:-}"
BR_OPTS="-o test_udp_echo=true -o test_udp_dst=fd12:3456::302:304:506:2 -o test_udp_period_s=$BR_PERIOD -o test_udp_start_s=60" \
CONF=lfn TOPO="$OUT.topo" TUN=$TUN FRAMES=1 SIM_OPTS="$SIM_OPTS" RESTART="1:$ROUTER_AT 2:$LFN_AT" \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > "$OUT.run" 2>&1
RC=$?
rm -f "$OUT.topo"
[ $RC = 0 ] || { echo "test_lfn_restart: run failed ($OUT.run)"; exit 1; }

PYTHONPATH=$HERE python3 - "$OUT" "$DUR" "$ROUTER_AT" "$LFN_AT" "$RECOVER_S" "$JOIN_MAX" <<'PY'
import collections, glob, os, re, sys
from lfn_lib import Checks, lines, frames, udp, listen_interval_s
out = sys.argv[1]
dur, r_at, l_at, recover, join_max = (float(x) for x in sys.argv[2:])
ck = Checks('test_lfn_restart')
# An instance started later prints its first run (before its first wake) at
# time 0: those lines belong to its start time
def inst_lines(path, start):
    for t, m in lines(path):
        yield (start if t < start else t), m
def first(path, start, pat):
    return next((t for t, m in inst_lines(path, start) if pat in m), None)
oper = first(f'{out}/node2.log', 0, 'LFN operational')
ck(oper is not None and oper <= join_max, f'LFN operational at {oper:.1f} s' if oper else 'LFN operational')
r_back = first(f'{out}/node1.r1.log', r_at, 'Bootstrap Done')
ck(r_back is not None, f'router restarted at {r_at:.0f} s: operational again {r_back - r_at:.1f} s later' if r_back else 'router operational again')
l_back = first(f'{out}/node2.r1.log', l_at, 'LFN operational')
ck(l_back is not None, f'LFN restarted at {l_at:.0f} s: operational again {l_back - l_at:.1f} s later' if l_back else 'LFN operational again')
# the LFN (first instance) after the router's restart: EAPOL again?
auth = [t for t, m in lines(f'{out}/node2.log') if 'LFN authentication start' in m and t > r_at]
ck.info(f"after the router's restart the LFN " + ('authenticated again' if auth else 'kept its keys (no EAPOL)'))
auth = [t for t, m in inst_lines(f'{out}/node2.r1.log', l_at) if 'LFN authentication start' in m]
ck.info(f"after its own restart the LFN " + ('authenticated again (EAPOL)' if auth else 'joined with its stored keys (no EAPOL)'))
# Replay drops. A drop is matched with the frame the node was receiving (the
# last frame sent to it, or broadcast, before the drop); the retry of a frame
# it already received (same sender, key, counter and sequence number: its ACK
# was lost) is a duplicate, rightly dropped, not a replay.
allf = [(t, src, f) for t, src, ch, f in frames(f'{out}/frames.txt')]
def node_of(name):
    return 0 if name == 'br' else int(name[4:].split('.')[0])
drops, dups = collections.Counter(), collections.Counter()
for path in sorted(glob.glob(f'{out}/*.log')):
    name = os.path.basename(path)[:-4]
    if name == 'simserver':
        continue
    me = node_of(name)
    mymac = f'01020304050600{me:02x}'
    start = r_at if name == 'node1.r1' else l_at if name == 'node2.r1' else 0
    for t, m in inst_lines(path, start):
        if 'MLME_COUNTER_ERROR' not in m:
            continue
        cand = [x for x in allf if x[0] <= t and x[1] != me and x[2]['dst'] in (mymac, None, 'ffff')]
        last = cand[-1] if cand else None
        if last and any(x[0] < last[0] and x[1] == last[1] and x[2]['counter'] == last[2]['counter'] and
                        x[2]['seq'] == last[2]['seq'] and x[2]['key_index'] == last[2]['key_index'] for x in cand[:-1]):
            dups[name] += 1
        else:
            drops[name] += 1
if dups:
    ck.info('retries of frames already received, dropped as duplicates: ' + ', '.join(f'{n} {c}' for n, c in sorted(dups.items())))
ck(not drops, 'frames dropped as replays (MLME_COUNTER_ERROR, not a retry of a frame received): '
   + (', '.join(f'{n} {c}' for n, c in sorted(drops.items())) or 'none'))
# Frame counters per sender and key index, per instance
restarts = {1: r_at, 2: l_at}
use = collections.defaultdict(lambda: collections.defaultdict(set))   # (src, key, inst) -> counter -> {(seq, dst)}
for t, src, ch, f in frames(f'{out}/frames.txt'):
    if f['security'] and f['counter'] is not None:
        inst = int(src in restarts and t >= restarts[src])
        use[(src, f['key_index'], inst)][f['counter']].add((f['seq'], f['dst']))
reused, regress = [], []
for (src, key, inst), cs in sorted(use.items()):
    reused += [(src, key, inst, c) for c, v in cs.items() if len(v) > 1]
    if inst == 1 and (src, key, 0) in use:
        old = max(use[(src, key, 0)])
        low = [c for c in cs if c <= old]
        if low:
            regress.append(f'node {src} key {key}: {len(low)} counters of the new instance at or below {old} (lowest {min(low)})')
ck(not reused, f'no frame counter carries two different frames' + (f': {len(reused)} do, first node {reused[0][0]} key {reused[0][1]} counter {reused[0][3]}' if reused else ''))
ck(not regress, 'counters after each restart above those before it' + (': ' + '; '.join(regress) if regress else ''))
for (src, key, inst), cs in sorted(use.items()):
    if src in restarts:
        ck.info(f'node {src} key index {key} instance {inst}: counters {min(cs)}-{max(cs)}')
# Traffic: requests from RECOVER_S after each restart to the next one. The
# LFN's request numbers start again with its new instance, so a request is
# matched to the answers that follow it within 60 s.
br = udp(f'{out}/br.log')
lfn = udp(f'{out}/node2.log')
for t, m in inst_lines(f'{out}/node2.r1.log', l_at):
    r = re.search(r'test-udp (tx to|rx from) (\S+): (\S+ \d+)', m)
    if r:
        lfn['tx' if r.group(1) == 'tx to' else 'rx'].append((t, r.group(2), r.group(3)))
def answered(t, p, rx):
    return any(x == p and t <= tr < t + 60 for tr, a, x in rx)
def traffic(a, b):
    res = []
    for tx, rx_peer in ((br, lfn), (lfn, br)):
        q = [(t, p) for t, d, p in tx['tx'] if p.startswith('Q') and a < t < b]
        got = [x for x in q if answered(*x, rx_peer['rx'])]
        back = [x for x in q if answered(x[0], x[1].replace('Q', 'R', 1), tx['rx'])]
        res.append((q, got, back))
    return res
periods = (('before the restarts', (oper or dur) + 15, r_at), ('after the router restart', r_at + recover, l_at),
           ('after the LFN restart', l_at + recover, dur - 15 - listen_interval_s(f'{out}/node2.r1.log')))
for name, a, b in periods:
    if b - a < 60:
        continue
    for way, (q, got, back) in zip(('BR -> LFN', 'LFN -> BR'), traffic(a, b)):
        ck(q and len(got) == len(q) and len(back) == len(q),
           f'{way} {name} ({a:.0f}-{b:.0f} s): {len(q)} requests, {len(got)} received, {len(back)} echoes back')
# Recovery: the first request after each restart that got its echo
for name, at in (('router', r_at), ('LFN', l_at)):
    for way, (q, got, back) in zip(('downlink', 'uplink'), traffic(at, dur - 15)):
        ck.info(f'after the {name} restart: first {way} echoed ' + (f'{back[0][0] - at:.1f} s later' if back else 'never'))
sys.exit(ck.done())
PY
RC=$?
[ "${KEEP:-0}" = 1 ] || rm -rf "$OUT" "$OUT.run"
exit $RC
