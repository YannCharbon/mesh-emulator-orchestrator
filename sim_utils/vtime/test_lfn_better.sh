#!/bin/bash
# A FAN 1.1 LFN moves to a better parent while attached, in virtual time (the
# stack's docs/LFN_STACK.md §9, "Better parent"). Border router 0, FFN
# routers 1 and 2 (LFN parents, both at -50 dBm from the border router), the
# LFN 3 hearing router 1 at OLD_RSSI and router 2 at NEW_RSSI. Router 2
# ignores LFNs until LATE s (test_lfn_silent_*): the LFN joins router 1, and
# router 2 is a parent that appears later. The LFN's background checks run
# every CHECK_S s on average (lfn_parent_check_s and lfn_parent_check_weak_s;
# the defaults are hours). Test traffic: the border router sends a UDP
# request to the LFN every BR_PERIOD s, the LFN one to the border router
# every LFN_PERIOD s; each side echoes what it receives.
#
# EXPECT=move: the LFN must change to router 2 within MOVE_MAX s after LATE,
# without a parent loss or EAPOL, release router 1 (ARO lifetime 0), after
# which router 1 drops it (no LFN sync frames from it from 70 s later until
# the LFN's next check, whose LPAS makes it a candidate parent again).
# EXPECT=stay: router 2 is only marginally better; the LFN must keep router 1
# through at least 2 checks made after LATE.
# Both: every request sent from 15 s after the LFN became operational to
# DUR - 15 s arrived once and was echoed, in both directions (no loss during
# the move). EXPECT=move also requires at least MOVE_REQ requests each way
# across the move (from 30 s before it to 60 s after the old parent's
# release), all delivered: the requests are denser for it (BR_PERIOD 13 s,
# LFN_PERIOD 17 s by default).
#
# usage: test_lfn_better.sh <bindir> <workdir(holds examples/)> <outdir> [profile]
# Environment: DUR (1800), WSSIM_SEED (1), TUN (tunvt0), EXPECT (move),
# OLD_RSSI (-87 for move, -70 for stay), NEW_RSSI (-60 for move, -67 for
# stay), LATE (600), CHECK_S (300), MOVE_MAX (3 x CHECK_S + 60: three checks
# on average; a check, two LPAS sweeps, hears a router in range in 94 % of
# the checks),
# BR_PERIOD (37; move: 13), LFN_PERIOD (53; move: 17), MOVE_REQ (3), SIM_OPTS
# (e.g. --drift-random=20), LFN_EXTRA, ROUTER_EXTRA, ROUTER2_EXTRA (router 2
# only; e.g. "-o test_lfn_lpa_unusable=1" with EXPECT=stay: its LPAs lack the
# BS-IE, and the LFN must keep router 1 although router 2 is better).
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
PROFILE=${4:-$WORK/examples/power/efr32fg25-soc.ini}
DUR=${DUR:-1800}; TUN=${TUN:-tunvt0}; EXPECT=${EXPECT:-move}
if [ "$EXPECT" = move ]; then OLD_RSSI=${OLD_RSSI:--87}; NEW_RSSI=${NEW_RSSI:--60}
else OLD_RSSI=${OLD_RSSI:--70}; NEW_RSSI=${NEW_RSSI:--67}; fi
LATE=${LATE:-600}; CHECK_S=${CHECK_S:-300}; MOVE_MAX=${MOVE_MAX:-$((CHECK_S * 3 + 60))}
if [ "$EXPECT" = move ]; then BR_PERIOD=${BR_PERIOD:-13}; LFN_PERIOD=${LFN_PERIOD:-17}
else BR_PERIOD=${BR_PERIOD:-37}; LFN_PERIOD=${LFN_PERIOD:-53}; fi
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_better: $OUT is not empty" >&2; exit 1; }
printf '0,1:-50\n0,2:-50\n1,3:%s\n2,3:%s\n' "$OLD_RSSI" "$NEW_RSSI" > "$OUT.topo"
LFN_ADDR=fd12:3456::302:304:506:3
BR_ADDR=fd12:3456::1615:16ff:fe17:1819
export NODE_OPTS_1="-o device_type=ffn -o enable_lfn=true ${ROUTER_EXTRA:-}"
export NODE_OPTS_2="-o device_type=ffn -o enable_lfn=true -o test_lfn_silent_at_s=1 -o test_lfn_silent_for_s=$((LATE - 1)) ${ROUTER_EXTRA:-} ${ROUTER2_EXTRA:-}"
export NODE_OPTS_3="-o device_type=lfn -o enable_lfn=false -o test_udp_echo=true -o test_udp_dst=$BR_ADDR -o test_udp_period_s=$LFN_PERIOD -o test_udp_start_s=70 -o lfn_parent_check_s=$CHECK_S -o lfn_parent_check_weak_s=$CHECK_S ${LFN_EXTRA:-}"
BR_OPTS="-o test_udp_echo=true -o test_udp_dst=$LFN_ADDR -o test_udp_period_s=$BR_PERIOD -o test_udp_start_s=60" \
CONF=lfn TOPO="$OUT.topo" TUN=$TUN POWER=$PROFILE \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > "$OUT.run" 2>&1
RC=$?
rm -f "$OUT.topo"
[ $RC = 0 ] || { echo "test_lfn_better: run failed ($OUT.run)"; exit 1; }

python3 - "$OUT" "$DUR" "$EXPECT" "$LATE" "$MOVE_MAX" "${MOVE_REQ:-3}" <<'EOF'
import re, sys
out, dur, expect, late, move_max = sys.argv[1], float(sys.argv[2]), sys.argv[3], float(sys.argv[4]), float(sys.argv[5])
move_req = int(sys.argv[6])
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
R1, R2 = '01:02:03:04:05:06:00:01', '01:02:03:04:05:06:00:02'
lfn = {'oper': None, 'disc': 0, 'lost': 0, 'auth': 0, 'checks': [], 'verdicts': [], 'moves': [], 'released': [], 'parents': [], 'utx': [], 'urx': []}
for t, m in lines('node3.log'):
    if 'LFN operational' in m and lfn['oper'] is None:
        lfn['oper'] = t
    elif 'LFN discovery start' in m:
        lfn['disc'] += 1
    elif re.search(r'parent .* lost', m):
        lfn['lost'] += 1
    elif 'LFN authentication start' in m:
        lfn['auth'] += 1
    elif 'LFN: better-parent check (' in m:
        lfn['checks'].append(t)
    elif 'LFN: check: parent' in m:
        lfn['verdicts'].append((t, m.split('LFN: check: ')[1]))
    elif 'LFN: better parent:' in m:
        lfn['moves'].append((t, m.split(' -> ')[1].split()[0]))
    elif 'LFN: old parent' in m and 'released' in m:
        lfn['released'].append(t)
    r = re.search(r'LFN: parent (\S+), PAN', m)
    if r:
        lfn['parents'].append((t, r.group(1)))
    r = re.search(r'test-udp (tx to|rx from) (\S+): (\S+ \d+)', m)
    if r:
        lfn['utx' if r.group(1) == 'tx to' else 'urx'].append((t, r.group(2), r.group(3)))
br = {'tx': [], 'rx': []}
for t, m in lines('br.log'):
    r = re.search(r'test-udp (tx to|rx from) (\S+): (\S+ \d+)', m)
    if r:
        br['tx' if r.group(1) == 'tx to' else 'rx'].append((t, r.group(2), r.group(3)))
oper = lfn['oper']
first = lfn['parents'][0][1] if lfn['parents'] else None
check(oper is not None and first == R1 and lfn['disc'] == 1 and not lfn['lost'] and lfn['auth'] == 1,
      f"LFN joined router 1 at {oper:.1f} s; {lfn['disc']} discovery, {lfn['lost']} parent losses, {lfn['auth']} EAPOL" if oper else 'LFN joined')
after = [t for t in lfn['checks'] if t > late]
print(f"info {len(lfn['checks'])} checks ({len(after)} after {late:.0f} s): " + '; '.join(f'{t:.0f} s {v}' for t, v in lfn['verdicts']))
if expect == 'move':
    mv = [x for x in lfn['moves'] if x[0] > late and x[1] == R2]
    final = lfn['parents'][-1][1] if lfn['parents'] else None
    check(mv and mv[0][0] - late <= move_max and final == R2 and len(lfn['moves']) == 1,
          f"moved to router 2 at {mv[0][0]:.1f} s, {mv[0][0] - late:.1f} s after it appeared (limit {move_max:.0f} s)" if mv else 'moved to router 2')
    rel = [t for t in lfn['released'] if mv and t > mv[0][0]]
    # (until the LFN's next check: its LPAS makes router 1 a candidate
    # parent again, which sends sync frames for 600 s)
    r1_rel, r1_bc_after, r1_lpas = None, 0, None
    for t, m in lines('node1.log'):
        if 'LFN child' in m and 'released its registrations' in m and r1_rel is None:
            r1_rel = t
        elif 'LPAS from LFN' in m and r1_rel is not None and r1_lpas is None:
            r1_lpas = t
        elif 'LFN broadcast slot' in m and r1_rel is not None and t > r1_rel + 70 and r1_lpas is None:
            r1_bc_after += 1
    check(rel and r1_rel is not None and r1_bc_after == 0,
          f"router 1 released {rel[0] - mv[0][0]:.1f} s after the move, dropped the LFN ({r1_bc_after} LFN sync frames from it 70 s later)" if rel and r1_rel else 'router 1 released')
else:
    check(not lfn['moves'] and len(after) >= 2 and lfn['parents'][-1][1] == R1,
          f"kept router 1 through {len(after)} checks after router 2 appeared")
last = dur - 15
def ways(a, b):
    """Requests both ways sent in (a, b): (name, requests, received, echoed, duplicates)"""
    res = []
    for name, tx, rx, back_rx in (('BR -> LFN', br['tx'], lfn['urx'], br['rx']), ('LFN -> BR', lfn['utx'], br['rx'], lfn['urx'])):
        q = [p for t, x, p in tx if p.startswith('Q') and a < t < b]
        got = [p for t, x, p in rx]
        back = {p.replace('R', 'Q', 1) for t, x, p in back_rx if p.startswith('R')}
        res.append((name, q, [p for p in q if p in got], [p for p in q if p in back], sum(got.count(p) > 1 for p in q)))
    return res
for name, q, got, back, dup in ways(oper + 15 if oper else dur, last):
    check(q and len(got) == len(q) and len(back) == len(q) and not dup,
          f"{name}: {len(q)} requests, {len(got)} received, {len(back)} echoes back, {dup} received twice")
if expect == 'move' and mv:
    a, b = mv[0][0] - 30, min((rel[0] if rel else mv[0][0]) + 60, last)
    for name, q, got, back, dup in ways(a, b):
        check((len(q) >= move_req or b - a < 90) and len(back) == len(q),
              f"{name} across the move ({a:.0f}-{b:.0f} s): {len(q)} requests (at least {move_req}), {len(back)} echoed")
print('test_lfn_better: ' + ('PASSED' if not fails else f'FAILED ({len(fails)})'))
sys.exit(1 if fails else 0)
EOF
