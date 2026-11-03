#!/bin/bash
# RFC 9685 multicast subscriptions of FAN 1.1 LFNs, in virtual time (the
# stack's docs/LFN_STACK.md §4, §9). Border router 0, an FFN router 1 (LFN
# parent), LFNs 2 and 3 hearing only the border router, LFNs 4 and 5 hearing
# only the router. LFNs 2 and 4 join the group GROUP (test_udp_group: the
# LFN registers it with its parent, EARO with P = 1); 3 and 5 do not. LFN 4
# leaves the group at LEAVE_AT s (EARO lifetime 0). The LFNs' registration
# lifetime is LIFETIME_MIN minutes, so that the registrations are refreshed
# during the run. The border router sends a UDP request to GROUP every
# BR_PERIOD s (MPL; the parents copy it to the LFN children that registered
# it); every LFN echoes what it receives.
#
# Checks, from the logs:
#   - every LFN joined and kept its parent;
#   - LFNs 2 and 4 received every group request sent from 15 s after they
#     became operational (LFN 4: until LEAVE_AT), each once;
#   - LFNs 3 and 5 received none; LFN 4 none from LEAVE_AT + 5 s;
#   - each subscribed LFN refreshed its group registration (more than one
#     EARO for the group, the last ones after the first lifetime ended).
#
# PROF=balanced or eco: the LFNs and both parents take Silicon Labs' LFN
# profile (lfn_profile.sh; listen intervals of 60 s and 300 s). The group
# requests come every 1.5 listen intervals (BR_PERIOD 90 s and 450 s), so a
# parent often holds two for an LFN, the second one for a whole listen
# interval or more (the path of the "second packet dropped after 30 s" bug,
# the stack's docs/LFN_STACK.md §11). A parent sends an LFN one frame per
# window: more than one request per listen interval would overload it.
#
# usage: test_lfn_mcast.sh <bindir> <workdir(holds examples/)> <outdir> [profile]
# Environment: PROF (none: the stack's defaults, the Test profile's
# intervals), DUR (1800; balanced 7200, eco 21600), WSSIM_SEED (1), TUN
# (tunvt0), GROUP (ff05::1234), BR_PERIOD (37; balanced 90, eco 450), LIFETIME_MIN (5; balanced 10,
# eco 30), LEAVE_AT (1200; balanced 5400, eco 14400), JOIN_MAX (300;
# balanced 900, eco 3600), SIM_OPTS (e.g. --drift-random=20), LFN_EXTRA,
# ROUTER_EXTRA.
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
PROFILE=${4:-$WORK/examples/power/efr32fg25-soc.ini}
HERE=$(cd "$(dirname "$0")" && pwd)
LFN_PROF=; PARENT_PROF=; D=(1800 5 1200 300 37)
if [ -n "${PROF:-}" ]; then
    . "$HERE/lfn_profile.sh"
    case $PROF in balanced) D=(7200 10 5400 900 90) ;; eco) D=(21600 30 14400 3600 450) ;; esac
fi
DUR=${DUR:-${D[0]}}; TUN=${TUN:-tunvt0}; GROUP=${GROUP:-ff05::1234}
BR_PERIOD=${BR_PERIOD:-${D[4]}}; LIFETIME_MIN=${LIFETIME_MIN:-${D[1]}}; LEAVE_AT=${LEAVE_AT:-${D[2]}}; JOIN_MAX=${JOIN_MAX:-${D[3]}}
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_mcast: $OUT is not empty" >&2; exit 1; }
printf '0,1:-50\n0,2:-50\n0,3:-50\n1,4:-50\n1,5:-50\n' > "$OUT.topo"
LFN="-o device_type=lfn -o enable_lfn=false -o test_udp_echo=true $LFN_PROF -o lfn_registration_lifetime_min=$LIFETIME_MIN ${LFN_EXTRA:-}"
export NODE_OPTS_1="-o device_type=ffn -o enable_lfn=true $PARENT_PROF ${ROUTER_EXTRA:-}"
export NODE_OPTS_2="$LFN -o test_udp_group=$GROUP"
export NODE_OPTS_3="$LFN"
export NODE_OPTS_4="$LFN -o test_udp_group=$GROUP -o test_udp_group_leave_s=$LEAVE_AT"
export NODE_OPTS_5="$LFN"
BR_OPTS="-o test_udp_echo=true -o test_udp_dst=$GROUP -o test_udp_period_s=$BR_PERIOD -o test_udp_start_s=60 $PARENT_PROF" \
CONF=lfn TOPO="$OUT.topo" TUN=$TUN POWER=$PROFILE \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > "$OUT.run" 2>&1
RC=$?
rm -f "$OUT.topo"
[ $RC = 0 ] || { echo "test_lfn_mcast: run failed ($OUT.run)"; exit 1; }

PYTHONPATH=$HERE python3 - "$OUT" "$DUR" "$JOIN_MAX" "$GROUP" "$LEAVE_AT" "$LIFETIME_MIN" <<'EOF'
import re, sys, ipaddress
from lfn_lib import listen_interval_s, mpl_seq_of_requests, mpl_received
out, dur, join_max, group, leave_at, life = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4], float(sys.argv[5]), float(sys.argv[6]) * 60
group = str(ipaddress.ip_address(group))
T0 = 1700000000.0
fails = []
def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        fails.append(what)
def lines(name):
    with open(f'{out}/{name}', errors='replace') as f:
        for l in f:
            p = l.split(' ', 1)
            try:
                yield float(p[0]) - T0, re.sub(r'\x1b\[[0-9;]*m', '', p[1]).strip()
            except (ValueError, IndexError):
                pass
sent = []
for t, m in lines('br.log'):
    r = re.search(r'test-udp tx to (\S+): (Q \d+)', m)
    if r and str(ipaddress.ip_address(r.group(1))) == group:
        sent.append((t, r.group(2)))
copies = {n: 0 for n in ('br.log', 'node1.log')}
for n in copies:
    for t, m in lines(n):
        if 'multicast copy to LFN' in m:
            copies[n] += 1
# A request counts against the LFN path under the router only if the router
# received its MPL message (an MPL loss between FFNs is printed, not counted)
seq = mpl_seq_of_requests(f'{out}/br.log', group)
r_got = mpl_received(f'{out}/node1.log')
print(f"info {len(sent)} group requests sent; MAC-unicast copies: border router {copies['br.log']}, router {copies['node1.log']}")
for node, sub in ((2, True), (3, False), (4, True), (5, False)):
    oper, disc, lost, rx, earo, parent = None, 0, 0, [], [], None
    for t, m in lines(f'node{node}.log'):
        if 'LFN operational' in m and oper is None:
            oper = t
        elif 'LFN discovery start' in m:
            disc += 1
        elif re.search(r'parent .* lost', m):
            lost += 1
        elif 'test-udp rx from' in m:
            r = re.search(r': (Q \d+)', m)
            if r:
                rx.append((t, r.group(1)))
        elif 'LFN: EARO multicast' in m:
            r = re.search(r'EARO multicast (\S+) to \S+, (\d+) min', m)
            earo.append((t, str(ipaddress.ip_address(r.group(1))), int(r.group(2))))
        r = re.search(r'LFN: parent (\S+), PAN', m)
        if r:
            parent = r.group(1)
    where = 'the border router' if node in (2, 3) else 'the router'
    check(oper is not None and oper <= join_max and disc == 1 and not lost,
          f"LFN {node} under {where}: operational at {oper:.1f} s, parent {parent} kept" if oper else f"LFN {node} operational")
    got = {}
    for t, p in rx:
        got.setdefault(p, []).append(t)
    dup = sum(len(v) - 1 for v in got.values())
    if sub:
        # a request sent less than a listen interval (and 15 s) before the end,
        # or before the LFN leaves, may not reach it in time
        iv = listen_interval_s(f'{out}/node{node}.log')
        until = (leave_at if node == 4 else dur) - 15 - iv
        want = [p for t, p in sent if oper and oper + 15 < t < until]
        ffn = [p for p in want if node == 4 and seq.get(p) not in r_got]
        want = [p for p in want if p not in ffn]
        if ffn:
            print(f"info LFN {node}: {len(ffn)} group requests its router never got by MPL (FFN loss, not counted): {', '.join(ffn)}")
        miss = [p for p in want if p not in got]
        check(want and not miss and not dup, f"LFN {node} (subscribed): {len(want) - len(miss)} of {len(want)} group requests (sent until {until:.0f} s"
              + (", that reached its router" if node == 4 else '') + f") received, {dup} duplicates" + (f"; missing {', '.join(miss[:6])}" if miss else ''))
        regs = [x for x in earo if x[1] == group and x[2] > 0]
        check(len(regs) >= 2 and regs[-1][0] - regs[0][0] >= life * 0.9,
              f"LFN {node}: {len(regs)} group registrations, from {regs[0][0]:.1f} to {regs[-1][0]:.1f} s (lifetime {life:.0f} s)" if regs else f"LFN {node}: group registered")
        if node == 4:
            # requests sent after the deregistration must not reach it
            after = [p for t, p in sent if t > leave_at + 5 and p in got]
            unreg = [x for x in earo if x[1] == group and x[2] == 0]
            check(unreg and not after, f"LFN 4 left the group at {leave_at:.0f} s: EARO lifetime 0 at {unreg[0][0]:.1f} s, {len(after)} group requests received after it" if unreg else 'LFN 4 left the group')
    else:
        check(not got, f"LFN {node} (not subscribed): {len(got)} group requests received")
print('test_lfn_mcast: ' + ('PASSED' if not fails else f'FAILED ({len(fails)})'))
sys.exit(1 if fails else 0)
EOF
