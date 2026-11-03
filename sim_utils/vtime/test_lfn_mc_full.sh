#!/bin/bash
# More RFC 9685 multicast registrations than an LFN parent keeps (the stack's
# docs/LFN_STACK.md §4), in virtual time. Node 0 is the border router, nodes
# 1..LFNS LFNs that hear only it; each LFN joins NGROUPS groups (test_udp_group)
# besides those the stack joins (ff03::1 and others), and registers them all. The border router keeps
# CAPACITY (32) registrations in all (lfn_mc_reg_max; the default is 2048,
# and 16 per LFN child).
#
# Checks:
#   - every LFN joined;
#   - the border router accepted CAPACITY registrations and refused the others
#     with EARO status 2 (Neighbor Cache Full, RFC 8505), and each refusal
#     reached the LFN (its NA with the status is reported);
#   - no registration was accepted beyond the capacity;
#   - a refusal does not cost the LFN its parent (the NA with status 2 used
#     to remove the neighbour, as for a failed address registration).
#
# usage: test_lfn_mc_full.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: LFNS (9), NGROUPS (4), CAPACITY (32), DUR (600), WSSIM_SEED (1),
# TUN (none), SIM_OPTS (--drift-random=20).
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
LFNS=${LFNS:-9}; GROUPS_N=${NGROUPS:-4}; CAPACITY=${CAPACITY:-32}; DUR=${DUR:-600}; TUN=${TUN:-none}
SIM_OPTS=${SIM_OPTS---drift-random=20}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_mc_full: $OUT is not empty" >&2; exit 1; }
: > "$OUT.topo"
for i in $(seq 1 "$LFNS"); do
    echo "0,$i:-50" >> "$OUT.topo"
    G=""
    for g in $(seq 1 "$GROUPS_N"); do G="$G -o test_udp_group=ff05::$i:$g"; done
    export NODE_OPTS_$i="-o device_type=lfn -o enable_lfn=false -o lfn_parent_check_s=0 $G"
done
BR_OPTS="-o lfn_mc_reg_max=$CAPACITY" CONF=lfn TOPO="$OUT.topo" TUN=$TUN SIM_OPTS="$SIM_OPTS" \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > "$OUT.run" 2>&1
RC=$?
rm -f "$OUT.topo"
[ $RC = 0 ] || { echo "test_lfn_mc_full: run failed ($OUT.run)"; exit 1; }
rm -f "$OUT.run"

python3 - "$OUT" "$LFNS" "$GROUPS_N" "$CAPACITY" <<'EOF'
import re, sys
out, lfns, groups, cap = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
fails = []
def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        fails.append(what)
def lines(name):
    for line in open(f'{out}/{name}', errors='replace'):
        yield re.sub(r'\x1b\[[0-9;]*m', '', line.rstrip('\n'))
joined = sum(any('LFN operational' in m for m in lines(f'node{i}.log')) for i in range(1, lfns + 1))
check(joined == lfns, f'{joined} of {lfns} LFNs operational')
seen, refused = set(), set()
for m in lines('br.log'):
    r = re.search(r'EARO multicast (\S+) from (\S+)', m)
    if r:
        seen.add((r.group(1), r.group(2)))
    r = re.search(r'LFN multicast registrations full: (\S+) from (\S+) refused', m)
    if r:
        refused.add((r.group(1), r.group(2)))
accepted = seen - refused
mac = lambda i: f'01:02:03:04:05:06:00:{i:02x}'
sent = set()
for i in range(1, lfns + 1):
    for m in lines(f'node{i}.log'):
        r = re.search(r'LFN: EARO multicast (\S+) to \S+, (\d+) min', m)
        if r and r.group(2) != '0':
            sent.add((r.group(1), mac(i)))
want = len(sent)
check(want >= lfns * (groups + 1) and len(seen) == want and len(accepted) == min(cap, want) and len(refused) == max(0, want - cap),
      f'{len(seen)} groups registered by the LFNs (expected {want}): {len(accepted)} kept (capacity {cap}), {len(refused)} refused with status 2')
told = 0
for i in range(1, lfns + 1):
    told += sum(1 for m in lines(f'node{i}.log') if re.search(r'EARO multicast \S+ refused by \S+: status 2', m))
check(told >= len(refused), f'{told} refusals reported by the LFNs (NA with status 2)')
removed = sum(1 for m in lines('br.log') if 'Neighbour removed for negative response' in m or re.search(r'neighbor remove fe80::302:304:506:', m))
lost = sum(sum(1 for m in lines(f'node{i}.log') if re.search(r'parent .* lost|LFN discovery start', m)) for i in range(1, lfns + 1))
check(not removed and lost == lfns, f'LFN children removed by the border router: {removed}; discoveries {lost} (one per LFN)')
print('test_lfn_mc_full: ' + ('PASSED' if not fails else f'FAILED ({len(fails)})'))
sys.exit(1 if fails else 0)
EOF
