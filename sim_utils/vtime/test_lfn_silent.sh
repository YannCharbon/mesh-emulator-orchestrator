#!/bin/bash
# FAN 1.1 LFNs that stay silent for hours: their parents must still reach
# them (the stack's docs/LFN_MAC.md §4). A parent's view of an LFN child's
# slots dates from the LFN's last LUTT-IE; on the 32-bit µs clock it must be
# re-anchored before it is 2^31 µs (35.8 min) old, also when the parent sends
# nothing to the LFN. Node 0 is the border router, node 1 an FFN router,
# node 2 an LFN under the router, node 3 an LFN under the border router. The
# LFNs send nothing of their own but the echo of each request (test_udp_echo:
# the test program listens only then): no test traffic, no better-parent
# check, a DHCPv6 lease that never ends (dhcp_lifetime_s infinite) and an
# address registration lifetime longer than the run. The border router sends
# a UDP request to each LFN every PERIOD s, from PERIOD s on, so each request
# follows PERIOD s (less the echo's delay) without a frame from or to the
# LFN.
#
# Checks, for each LFN:
#   - joined its expected parent and kept it (one discovery, no parent loss,
#     no parent search);
#   - received every request;
#   - between two requests (and from its registration to the first one) its
#     parent re-anchored its schedule every 2^30 µs (counted from the
#     parent's "schedule re-anchored" traces): nothing else refreshed it.
#
# usage: test_lfn_silent.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: PERIOD (3600), COUNT (2: requests per LFN; DUR = PERIOD x
# COUNT + 60), WSSIM_SEED (1), TUN (none), SIM_OPTS (--drift-random=20),
# LFN_EXTRA, PARENT_EXTRA.
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
PERIOD=${PERIOD:-3600}; COUNT=${COUNT:-2}; TUN=${TUN:-none}
SIM_OPTS=${SIM_OPTS---drift-random=20}
DUR=$((PERIOD * COUNT + 60))
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_silent: $OUT is not empty" >&2; exit 1; }
printf '0,1:-50\n1,2:-50\n0,3:-50\n' > "$OUT.topo"
lfn_addr() { printf 'fd12:3456::302:304:506:%x' "$1"; }
LFN_OPTS="-o device_type=lfn -o enable_lfn=false -o test_udp_echo=true -o lfn_parent_check_s=0 -o lfn_parent_check_weak_s=0 -o lfn_registration_lifetime_min=5040 ${LFN_EXTRA:-}"
export NODE_OPTS_1="-o device_type=ffn -o enable_lfn=true -o lfn_registration_lifetime_min=5040 ${PARENT_EXTRA:-}"
export NODE_OPTS_2="$LFN_OPTS"
export NODE_OPTS_3="$LFN_OPTS"
BR_OPTS="-o test_udp_dst=$(lfn_addr 2) -o test_udp_dst=$(lfn_addr 3) -o test_udp_period_s=$PERIOD -o test_udp_start_s=$PERIOD -o dhcp_lifetime_s=4294967295 -o lfn_registration_lifetime_min=5040 ${PARENT_EXTRA:-}" \
CONF=lfn TOPO="$OUT.topo" TUN=$TUN SIM_OPTS="$SIM_OPTS" \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > "$OUT.run" 2>&1
RC=$?
rm -f "$OUT.topo"
[ $RC = 0 ] || { echo "test_lfn_silent: run failed ($OUT.run)"; exit 1; }
rm -f "$OUT.run"

python3 - "$OUT" "$DUR" "$PERIOD" <<'EOF'
import re, sys
out, dur, period = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
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
mac = lambda i: f'01:02:03:04:05:06:00:{i:02x}'
lfn_addr = lambda i: f'fd12:3456::302:304:506:{i:x}'
br_tx = {2: [], 3: []}
for t, m in lines('br.log'):
    r = re.search(r'test-udp tx to (\S+): (\S+ \d+)', m)
    if r:
        for i in (2, 3):
            if r.group(1) == lfn_addr(i):
                br_tx[i].append((t, r.group(2)))
reanchor = {1: [], 0: []}
for parent, log in ((1, 'node1.log'), (0, 'br.log')):
    for t, m in lines(log):
        if 'schedule re-anchored' in m:
            reanchor[parent].append((t, m))
for node, parent in ((2, 1), (3, 0)):
    print(f'--- LFN {node} (parent: {"the router" if parent else "the border router"})')
    disc = lost = search = 0
    oper = aro = None
    rx = set()
    for t, m in lines(f'node{node}.log'):
        if 'LFN discovery start' in m:
            disc += 1
        elif 'LFN operational' in m and oper is None:
            oper = t
        elif re.search(r'parent .* lost', m):
            lost += 1
        elif 'LFN: parent search' in m:
            search += 1
        r = re.search(r'test-udp rx from \S+: (\S+ \d+)', m)
        if r:
            rx.add(r.group(1))
    check(oper is not None and disc == 1 and not lost and not search,
          f'operational at {oper:.0f} s, parent kept ({disc} discovery, {lost} losses, {search} searches)' if oper else 'joined')
    q = br_tx[node]
    check(len(q) >= int((dur - period) // period) + 1 and all(p in rx for t, p in q),
          f'{len(q)} requests after {period:.0f} s of silence each, {sum(p in rx for t, p in q)} received')
    ra = [t for t, m in reanchor[parent] if mac(node) in m]
    prev = oper or 0
    for t, p in q:
        n = sum(prev < x < t for x in ra)
        exp = int((t - prev) / 1073.741824)
        check(exp - 1 <= n <= exp,
              f'{t - prev:.0f} s without a frame to {p}: {n} re-anchors by the parent (2^30 us apart: {exp} expected)')
        prev = t
print('test_lfn_silent: ' + ('PASSED' if not fails else f'FAILED ({len(fails)})'))
sys.exit(1 if fails else 0)
EOF
