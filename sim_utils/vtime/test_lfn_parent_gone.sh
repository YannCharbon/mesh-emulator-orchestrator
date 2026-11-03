#!/bin/bash
# An LFN's parent disappears, in virtual time. Border router 0, FFN routers 1
# and 2 (LFN parents, both at -50 dBm from the border router), LFN 3 hearing
# router 1 at -50 dBm and router 2 at -60 dBm: it joins router 1 (router 2
# answers LFNs only from EVENT_AT - 200 s). At EVENT_AT
# s, by MODE:
#   traffic  router 1's program is stopped outright (run.sh KILL), with test
#            traffic both ways (BR_PERIOD, LFN_PERIOD);
#   idle     the same, without test traffic: the LFN notices only the missing
#            sync frames; requests both ways at EVENT_AT + 450 s and + 550 s
#            check the traffic after the change (test_udp_at_s);
#   uplink   router 1 keeps running and sending its sync frames but no longer
#            hears the LFN (run.sh CUT 3 -> 1): its uplinks fail, which must
#            start a parent search (search_uplink_fails); with test traffic.
#
# Checks:
#   - the LFN joined router 1 (one discovery, one EAPOL);
#   - after the event it attached to router 2 within CHANGE_MAX s, without
#     EAPOL (the keys are still valid); uplink: the search was started by the
#     failed uplinks;
#   - traffic: every request sent before the event, and from 30 s after the
#     change to the end (less 15 s), arrived and was echoed, both ways; the
#     requests lost in between are printed.
#
# usage: test_lfn_parent_gone.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: MODE (traffic), DUR (1800), WSSIM_SEED (1), TUN (none),
# EVENT_AT (600), CHANGE_MAX (300), BR_PERIOD (37), LFN_PERIOD (53), JOIN_MAX
# (300), SIM_OPTS (--drift-random=20), LFN_EXTRA, ROUTER_EXTRA, KEEP.
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
MODE=${MODE:-traffic}; DUR=${DUR:-1800}; TUN=${TUN:-none}; EVENT_AT=${EVENT_AT:-600}
CHANGE_MAX=${CHANGE_MAX:-300}; BR_PERIOD=${BR_PERIOD:-37}; LFN_PERIOD=${LFN_PERIOD:-53}
JOIN_MAX=${JOIN_MAX:-300}; SIM_OPTS=${SIM_OPTS---drift-random=20}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_parent_gone: $OUT is not empty" >&2; exit 1; }
printf '0,1:-50\n0,2:-50\n1,3:-50\n2,3:-60\n' > "$OUT.topo"
BR_ADDR=fd12:3456::1615:16ff:fe17:1819; LFN_ADDR=fd12:3456::302:304:506:3
case $MODE in
traffic|uplink)
    BR_T="-o test_udp_period_s=$BR_PERIOD -o test_udp_start_s=60"
    LFN_T="-o test_udp_period_s=$LFN_PERIOD -o test_udp_start_s=70" ;;
idle)
    BR_T="-o test_udp_at_s=$((EVENT_AT + 450)) -o test_udp_at_s=$((EVENT_AT + 550))"
    LFN_T="-o test_udp_at_s=$((EVENT_AT + 460)) -o test_udp_at_s=$((EVENT_AT + 560))" ;;
*) echo "test_lfn_parent_gone: MODE traffic, idle or uplink" >&2; exit 2 ;;
esac
EV=(KILL="1:$EVENT_AT"); [ "$MODE" = uplink ] && EV=(CUT="3:1:$EVENT_AT")
export NODE_OPTS_1="-o device_type=ffn -o enable_lfn=true ${ROUTER_EXTRA:-}"
# Router 2 ignores LFNs until EVENT_AT - 200 s (test_lfn_silent_*), so that
# the LFN joins router 1 whichever router is ready first
export NODE_OPTS_2="$NODE_OPTS_1 -o test_lfn_silent_at_s=1 -o test_lfn_silent_for_s=$((EVENT_AT - 201))"
export NODE_OPTS_3="-o device_type=lfn -o enable_lfn=false -o test_udp_echo=true -o test_udp_dst=$BR_ADDR $LFN_T ${LFN_EXTRA:-}"
env "${EV[@]}" BR_OPTS="-o test_udp_echo=true -o test_udp_dst=$LFN_ADDR $BR_T" \
CONF=lfn TOPO="$OUT.topo" TUN=$TUN SIM_OPTS="$SIM_OPTS" \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > "$OUT.run" 2>&1
RC=$?
rm -f "$OUT.topo"
[ $RC = 0 ] || { echo "test_lfn_parent_gone: run failed ($OUT.run)"; exit 1; }

PYTHONPATH=$HERE python3 - "$OUT" "$MODE" "$DUR" "$EVENT_AT" "$CHANGE_MAX" "$JOIN_MAX" <<'PY'
import re, sys
from lfn_lib import Checks, lines, udp, mac, exchange, BR_ADDR, lfn_addr
out, mode = sys.argv[1], sys.argv[2]
dur, ev, change_max, join_max = (float(x) for x in sys.argv[3:])
ck = Checks(f'test_lfn_parent_gone ({mode})')
R1, R2 = mac(1), mac(2)
L = {'oper': None, 'disc': [], 'auth': [], 'lost': [], 'search': [], 'parents': []}
for t, m in lines(f'{out}/node3.log'):
    if 'LFN operational' in m and L['oper'] is None:
        L['oper'] = t
    elif 'LFN discovery start' in m:
        L['disc'].append(t)
    elif 'LFN authentication start' in m:
        L['auth'].append(t)
    elif re.search(r'parent .* lost', m):
        L['lost'].append(t)
    elif 'LFN: parent search (' in m:
        L['search'].append((t, re.search(r'search \(([^)]*)\)', m).group(1)))
    r = re.search(r'LFN: parent (\S+), PAN', m)
    if r:
        L['parents'].append((t, r.group(1)))
oper = L['oper']
p0 = L['parents'][0][1] if L['parents'] else None
before = [t for t in L['disc'] if t < ev]
ck(oper is not None and oper <= join_max and p0 == R1 and len(before) == 1 and len([t for t in L['auth'] if t < ev]) == 1,
   f"LFN joined router 1 at {oper:.1f} s ({len(before)} discovery)" if oper else 'LFN joined')
new = [(t, p) for t, p in L['parents'] if t > ev and p == R2]
chg = new[0][0] if new else None
auth = [t for t in L['auth'] if t > ev]
ck(chg is not None and chg - ev <= change_max and not auth and L['parents'][-1][1] == R2,
   f"attached to router 2 {chg - ev:.1f} s after the event, {'EAPOL again' if auth else 'without EAPOL'}, "
   f"{len([t for t in L['lost'] if t > ev])} parent losses declared, searches: "
   + (', '.join(f'{t - ev:.0f} s ({w})' for t, w in L['search'] if t > ev) or 'none')
   if chg else f"attached to router 2 (parents after the event: {[p for t, p in L['parents'] if t > ev]})")
if mode == 'uplink':
    s = [w for t, w in L['search'] if t > ev]
    ck(s and s[0] == 'uplinks failed', f"first parent search after the cut: {s[0] if s else 'none'} (expected: uplinks failed)")
br, lfn = udp(f'{out}/br.log'), udp(f'{out}/node3.log')
end = dur - 15
for name, a, b in (('before the event', (oper or dur) + 15, ev), ('after the change', (chg or dur) + 30, end)):
    for way, tx, rx, peer in (('BR -> LFN', br, lfn, None), ('LFN -> BR', lfn, br, lfn_addr(3))):
        q, got, back = exchange(tx, rx, peer, a, b)
        ck((q or mode == 'idle' and name.startswith('before')) and len(got) == len(q) and len(back) == len(q),
           f'{way} {name} ({a:.0f}-{b:.0f} s): {len(q)} requests, {len(got)} received, {len(back)} echoes back')
if chg:
    for way, tx, rx, peer in (('BR -> LFN', br, lfn, None), ('LFN -> BR', lfn, br, lfn_addr(3))):
        q, got, back = exchange(tx, rx, peer, ev, chg + 30)
        ck.info(f'{way} between the event and 30 s after the change: {len(q)} requests, {len(back)} echoed')
sys.exit(ck.done())
PY
RC=$?
[ "${KEEP:-0}" = 1 ] || rm -rf "$OUT" "$OUT.run"
exit $RC
