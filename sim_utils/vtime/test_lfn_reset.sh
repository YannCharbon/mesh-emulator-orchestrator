#!/bin/bash
# A reset of an LFN parent router, or of an LFN, that keeps its storage and
# finds the same LFN group key (LGTK) in use, in virtual time (the stack's
# docs/LFN_STACK.md §8). Node 0 is the border router, node 1 an FFN router
# that parents LFNs, node 2 an LFN that hears only the router. RESET (router
# or lfn) names the node that resets at RESET_AT s: its interface goes down
# and comes up RESET_FOR s later (wsnode's test_reset_*), its storage kept.
# The stack stores its frame counters at the stop, as it does every
# FRAME_COUNTER_STORE_THRESHOLD frames; a power loss relies on the latter.
# Test traffic: the border router sends a UDP request to the LFN every
# BR_PERIOD s, the LFN one to the border router every LFN_PERIOD s; each side
# echoes.
#
# Checks:
#   - no frame dropped as a replay (MLME_COUNTER_ERROR) by any node;
#   - the reset node restored its group-key frame counters above the values
#     it had reached before the reset (LGTKs on the router, the LFN's LGTKs
#     in its group-key slots), so no counter value is used twice;
#   - the LFN authenticated once (no EAPOL exchange after the reset: its
#     stored keys are installed again, or kept);
#   - the LFN is operational again after the reset;
#   - unicast both ways: every request sent before the reset and from
#     RECOVER s after it to DUR - 15 s arrived and was echoed.
#
# usage: test_lfn_reset.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: RESET (router), RESET_AT (600), RESET_FOR (5), DUR (1500),
# RECOVER (300 for the router, 120 for the LFN), WSSIM_SEED (1), TUN (none),
# BR_PERIOD (37), LFN_PERIOD (53), SIM_OPTS (--drift-random=20), LFN_EXTRA,
# ROUTER_EXTRA.
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
RESET=${RESET:-router}; RESET_AT=${RESET_AT:-600}; RESET_FOR=${RESET_FOR:-5}; DUR=${DUR:-1500}
TUN=${TUN:-none}; BR_PERIOD=${BR_PERIOD:-37}; LFN_PERIOD=${LFN_PERIOD:-53}
SIM_OPTS=${SIM_OPTS---drift-random=20}
case $RESET in
router) RNODE=1; RECOVER=${RECOVER:-300} ;;
lfn)    RNODE=2; RECOVER=${RECOVER:-120} ;;
*) echo "test_lfn_reset: RESET is router or lfn" >&2; exit 2 ;;
esac
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_reset: $OUT is not empty" >&2; exit 1; }
printf '0,1:-50\n1,2:-50\n' > "$OUT.topo"
LFN_ADDR=fd12:3456::302:304:506:2
BR_ADDR=fd12:3456::1615:16ff:fe17:1819
RST="-o test_reset_at_s=$RESET_AT -o test_reset_for_s=$RESET_FOR"
export NODE_OPTS_1="-o device_type=ffn -o enable_lfn=true $( [ $RNODE = 1 ] && echo "$RST") ${ROUTER_EXTRA:-}"
export NODE_OPTS_2="-o device_type=lfn -o enable_lfn=false -o test_udp_echo=true -o test_udp_dst=$BR_ADDR -o test_udp_period_s=$LFN_PERIOD -o test_udp_start_s=70 $( [ $RNODE = 2 ] && echo "$RST") ${LFN_EXTRA:-}"
BR_OPTS="-o test_udp_echo=true -o test_udp_dst=$LFN_ADDR -o test_udp_period_s=$BR_PERIOD -o test_udp_start_s=60" \
CONF=lfn TOPO="$OUT.topo" TUN=$TUN SIM_OPTS="$SIM_OPTS" \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > "$OUT.run" 2>&1
RC=$?
rm -f "$OUT.topo"
[ $RC = 0 ] || { echo "test_lfn_reset: run failed ($OUT.run)"; exit 1; }
rm -f "$OUT.run"

python3 - "$OUT" "$DUR" "$RNODE" "$RESET_AT" "$RECOVER" <<'EOF'
import re, sys
out, dur, rnode, reset_at, recover = sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
T0 = 1700000000.0
fails = []
def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        fails.append(what)
def lines(name):
    for line in open(f'{out}/{name}', errors='replace'):
        t, _, msg = line.partition(' ')
        try:
            yield float(t) - T0, re.sub(r'\x1b\[[0-9;]*m', '', msg.rstrip('\n'))
        except ValueError:
            pass
logs = {0: 'br.log', 1: 'node1.log', 2: 'node2.log'}
replays = {n: sum('MLME_COUNTER_ERROR' in m for t, m in lines(f)) for n, f in logs.items()}
check(not any(replays.values()), 'frames dropped as replays (MLME_COUNTER_ERROR): ' +
      ', '.join(f'node {n}: {c}' for n, c in replays.items()))
# Counters reached before the reset, restored after it, per key slot
before, restored = {}, {}
lgtk = rnode == 1
for t, m in lines(logs[rnode]):
    if lgtk:
        r = re.search(r'LGTK frame counter at stop: index (\d) value (\d+)', m) if t < reset_at + 1 else None
        if r:
            before[int(r.group(1))] = max(before.get(int(r.group(1)), 0), int(r.group(2)))
        r = re.search(r'Read LGTK frame counter: index (\d) value (\d+)', m)
        if r and t > reset_at:
            restored.setdefault(int(r.group(1)), int(r.group(2)))
    else:
        r = re.search(r'(?:Stored updated|Pending to store new) frame counter: index (\d) value (\d+)', m)
        if r and t < reset_at + 1:
            before[int(r.group(1))] = max(before.get(int(r.group(1)), 0), int(r.group(2)))
        r = re.search(r'Read frame counter: index (\d) value (\d+)', m)
        if r and t > reset_at:
            restored.setdefault(int(r.group(1)), int(r.group(2)))
check(before and all(i in restored and restored[i] > v for i, v in before.items()),
      f'node {rnode} ({"LGTK" if lgtk else "LFN group key"} slots): counters before the reset {before}, restored {restored}')
L = {'oper': [], 'auth': 0, 'tx': [], 'rx': []}
auth_at = None
for t, m in lines('node2.log'):
    if 'LFN operational' in m:
        L['oper'].append(t)
    elif 'LFN authentication start' in m:
        L['auth'] += 1
        auth_at = t
    elif 'Existing keys used' in m and auth_at == t:
        L['auth'] -= 1    # stored keys installed, no EAPOL exchange
    r = re.search(r'test-udp (tx to|rx from) (\S+): (\S+ \d+)', m)
    if r:
        L['tx' if r.group(1) == 'tx to' else 'rx'].append((t, r.group(3)))
check(L['oper'] and L['oper'][0] < reset_at and L['auth'] == 1,
      f"LFN operational at {L['oper'][0]:.1f} s, {L['auth']} EAPOL exchange(s)" if L['oper'] else 'LFN operational')
again = [t for t in L['oper'] if t > reset_at]
if rnode == 2:
    check(again, f'LFN operational again {again[0] - reset_at:.1f} s after its reset' if again else 'LFN operational again')
br = {'tx': [], 'rx': []}
for t, m in lines('br.log'):
    r = re.search(r'test-udp (tx to|rx from) (\S+): (\S+ \d+)', m)
    if r:
        br['tx' if r.group(1) == 'tx to' else 'rx'].append((t, r.group(3)))
oper = L['oper'][0] if L['oper'] else dur
ok_t = lambda t: oper + 15 < t < reset_at or reset_at + recover < t < dur - 15
for name, tx, rx, back in (('BR -> LFN', br['tx'], L['rx'], br['rx']), ('LFN -> BR', L['tx'], br['rx'], L['rx'])):
    q = [p for t, p in tx if p.startswith('Q') and ok_t(t)]
    got = {p for t, p in rx}
    echoed = {p.replace('R', 'Q', 1) for t, p in back if p.startswith('R')}
    lost_during = sum(1 for t, p in tx if p.startswith('Q') and reset_at <= t <= reset_at + recover and p not in got)
    check(q and all(p in got and p in echoed for p in q),
          f'{name}: {len(q)} requests outside the reset, {sum(p in got for p in q)} received, {sum(p in echoed for p in q)} echoed '
          f'({lost_during} lost during the {recover:.0f} s after the reset)')
print('test_lfn_reset: ' + ('PASSED' if not fails else f'FAILED ({len(fails)})'))
sys.exit(1 if fails else 0)
EOF
