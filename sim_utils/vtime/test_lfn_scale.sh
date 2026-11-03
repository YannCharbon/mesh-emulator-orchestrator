#!/bin/bash
# Many FAN 1.1 LFNs, in virtual time. SCALE=star: border router 0, an FFN
# router 1 (LFN parent), LFNS (20) LFNs 2.. hearing only the router.
# SCALE=mesh: dag_simple (border router 0, routers 1-14, all LFN parents) and
# LFNS (50) LFNs 15.., each hearing two routers (-50 and -65 dBm; the routers
# are spread over the 14 by the LFN's number). Every LFN subscribes to GROUP.
# Test traffic: the border router sends a UDP request to each LFN and one to
# GROUP every BR_PERIOD s, GAP_MS apart (test_udp_gap_ms), each LFN one to the
# border router every LFN_PERIOD s (starts spread over a period); each side
# echoes the unicast requests.
#
# Checks:
#   - every LFN joined by JOIN_MAX s, with a parent among its neighbours, and
#     kept it (one discovery, no parent loss);
#   - unicast both ways, for every LFN: the requests from 15 s after it became
#     operational (and TRAFFIC_FROM) to the end (less 30 s) arrived and were
#     echoed;
#   - group: every GROUP request reached every LFN once;
#   - every frame each LFN received started inside one of its windows (ACKs
#     to its own frames aside) and each received at least MIN_RX frames;
#   - parents' queues: no parent's adaptation-layer queue reached 20 frames
#     (the stack traces it then: "Adaptation layer TX queue size"), and the
#     downlinks' delay (border router's request to the LFN's reception) stays
#     under DELAY_MAX s; the delays are printed (median, 95th percentile,
#     largest).
#
# usage: test_lfn_scale.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: SCALE (star), LFNS (20 star, 50 mesh), DUR (2400 star, 3000
# mesh), WSSIM_SEED (1), TUN (none), BR_PERIOD (120 star, 300 mesh), GAP_MS
# (1000 star, 2000 mesh), LFN_PERIOD (as BR_PERIOD), TRAFFIC_FROM (600 star,
# 1200 mesh), JOIN_MAX (900 star, 1500 mesh), GROUP (ff05::1234), MIN_RX
# (20), DELAY_MAX (60), SIM_OPTS (--drift-random=20), LFN_EXTRA,
# ROUTER_EXTRA, KEEP.
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
SCALE=${SCALE:-star}; TUN=${TUN:-none}; GROUP=${GROUP:-ff05::1234}; SIM_OPTS=${SIM_OPTS---drift-random=20}
case $SCALE in
star) D=(20 2400 120 1000 600 900) ;;
mesh) D=(50 3000 300 2000 1200 1500) ;;
*) echo "test_lfn_scale: SCALE star or mesh" >&2; exit 2 ;;
esac
LFNS=${LFNS:-${D[0]}}; DUR=${DUR:-${D[1]}}; BR_PERIOD=${BR_PERIOD:-${D[2]}}; GAP_MS=${GAP_MS:-${D[3]}}
LFN_PERIOD=${LFN_PERIOD:-$BR_PERIOD}; TRAFFIC_FROM=${TRAFFIC_FROM:-${D[4]}}; JOIN_MAX=${JOIN_MAX:-${D[5]}}
MIN_RX=${MIN_RX:-20}; DELAY_MAX=${DELAY_MAX:-60}
(( LFNS < 64 )) || { echo "test_lfn_scale: at most 63 LFNs (test_udp_dst)" >&2; exit 2; }
(( (LFNS + 1) * GAP_MS < BR_PERIOD * 1000 )) || { echo "test_lfn_scale: a round of requests must fit in BR_PERIOD" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_scale: $OUT is not empty" >&2; exit 1; }
lfn_addr() { printf 'fd12:3456::302:304:506:%x' "$1"; }
BR_ADDR=fd12:3456::1615:16ff:fe17:1819
if [ "$SCALE" = star ]; then
    FIRST=2; ROUTERS="1"; echo "0,1:-50" > "$OUT.topo"
    for (( i = FIRST; i < FIRST + LFNS; i++ )); do echo "1,$i:-50" >> "$OUT.topo"; done
else
    FIRST=15; ROUTERS="$(seq -s ' ' 1 14)"
    grep '^DAG_SIMPLE=' "$HERE/run.sh" | cut -d'"' -f2 | tr ' ' '\n' > "$OUT.topo"
    for (( k = 0; k < LFNS; k++ )); do
        i=$((FIRST + k)); a=$((k % 14 + 1)); b=$(((k * 5 + 3) % 14 + 1)); [ $b = $a ] && b=$((a % 14 + 1))
        printf '%d,%d:-50\n%d,%d:-65\n' $a $i $b $i >> "$OUT.topo"
    done
fi
LAST=$((FIRST + LFNS - 1))
BR_T="-o test_udp_echo=true -o test_udp_period_s=$BR_PERIOD -o test_udp_start_s=$TRAFFIC_FROM -o test_udp_gap_ms=$GAP_MS"
for (( i = FIRST; i <= LAST; i++ )); do BR_T+=" -o test_udp_dst=$(lfn_addr $i)"; done
BR_T+=" -o test_udp_dst=$GROUP"
for r in $ROUTERS; do export NODE_OPTS_$r="-o device_type=ffn -o enable_lfn=true ${ROUTER_EXTRA:-}"; done
for (( i = FIRST; i <= LAST; i++ )); do
    export NODE_OPTS_$i="-o device_type=lfn -o enable_lfn=false -o test_udp_echo=true -o test_udp_group=$GROUP -o test_udp_dst=$BR_ADDR -o test_udp_period_s=$LFN_PERIOD -o test_udp_start_s=$((TRAFFIC_FROM + 30 + (i - FIRST) * LFN_PERIOD / LFNS)) ${LFN_EXTRA:-}"
done
BR_OPTS="$BR_T" CONF=lfn TOPO="$OUT.topo" TUN=$TUN SIM_OPTS="$SIM_OPTS" \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > "$OUT.run" 2>&1
RC=$?
rm -f "$OUT.topo"
[ $RC = 0 ] || { echo "test_lfn_scale: run failed ($OUT.run)"; exit 1; }

PYTHONPATH=$HERE python3 - "$OUT" "$SCALE" "$FIRST" "$LAST" "$DUR" "$JOIN_MAX" "$TRAFFIC_FROM" "$GROUP" "$MIN_RX" "$DELAY_MAX" "$ROUTERS" <<'PY'
import ipaddress, re, statistics, sys
from lfn_lib import Checks, lines, udp, exchange, lfn_reception, eui_node, lfn_addr, listen_interval_s, mpl_seq_of_requests, mpl_received
out, scale = sys.argv[1], sys.argv[2]
first, last = int(sys.argv[3]), int(sys.argv[4])
dur, join_max, t_from = float(sys.argv[5]), float(sys.argv[6]), float(sys.argv[7])
group = str(ipaddress.ip_address(sys.argv[8]))
min_rx, delay_max = int(sys.argv[9]), float(sys.argv[10])
routers = [int(x) for x in sys.argv[11].split()]
ck = Checks(f'test_lfn_scale ({scale})')
edges = set()
for l in open(f'{out}/topology.txt'):
    a, b = l.split(':')[0].split(',')
    edges.add(frozenset((int(a), int(b))))
br = udp(f'{out}/br.log')
gsent = [(t, p) for t, d, p in br['tx'] if str(ipaddress.ip_address(d)) == group]
end0 = dur - 30
# A group request counts against an LFN only if its parent got the request's
# MPL message (or is the border router): MPL losses between FFNs are printed
seq = mpl_seq_of_requests(f'{out}/br.log', group)
mpl_got = {r: mpl_received(f'{out}/node{r}.log') for r in routers}
ffn_lost = {}
bad = {k: [] for k in ('join', 'down', 'up', 'group', 'win')}
opers, delays, nrx, parents = [], [], [], {}
for n in range(first, last + 1):
    oper, disc, lost, par = None, 0, 0, None
    for t, m in lines(f'{out}/node{n}.log'):
        if 'LFN operational' in m and oper is None:
            oper = t
        elif 'LFN discovery start' in m:
            disc += 1
        elif re.search(r'parent .* lost', m):
            lost += 1
        r = re.search(r'LFN: parent (\S+), PAN', m)
        if r:
            par = eui_node(r.group(1))
    parents[n] = par
    if oper is None or oper > join_max or par is None or frozenset((n, par)) not in edges or disc != 1 or lost:
        bad['join'].append(f'{n} (operational {oper}, parent {par}, {disc} discoveries, {lost} losses)')
        continue
    opers.append(oper)
    lf = udp(f'{out}/node{n}.log')
    end = end0 - listen_interval_s(f'{out}/node{n}.log')   # downlinks wait for a window
    a = max(oper + 15, t_from)
    q, got, back = exchange(br, lf, None, a, end, dst=lfn_addr(n))
    if not q or len(got) < len(q) or len(back) < len(q):
        bad['down'].append(f'{n} ({len(q)} requests, {len(got)} received, {len(back)} echoed)')
    rxt = {p: t for t, s, p in lf['rx']}
    delays += [rxt[p] - t for t, p in got]
    q, got, back = exchange(lf, br, lfn_addr(n), a, end)
    if not q or len(got) < len(q) or len(back) < len(q):
        bad['up'].append(f'{n} ({len(q)} requests, {len(got)} received, {len(back)} echoed)')
    gw = [p for t, p in gsent if a < t < end]
    if par in mpl_got:
        lost = [p for p in gw if seq.get(p) not in mpl_got[par]]
        if lost:
            ffn_lost[n] = (par, lost)
        gw = [p for p in gw if p not in lost]
    gr = [p for t, s, p in lf['rx'] if p in set(gw)]
    if not gw or set(gr) != set(gw) or len(gr) != len(set(gr)):
        bad['group'].append(f'{n} ({len(set(gr))} of {len(gw)}, {len(gr) - len(set(gr))} duplicates)')
    rec = lfn_reception(f'{out}/node{n}.log')
    nrx.append(len(rec['rx']))
    if len(rec['rx']) < min_rx or rec['outside']:
        bad['win'].append(f"{n} ({len(rec['rx'])} frames, {len(rec['outside'])} outside)")
N = last - first + 1
def show(lst):
    return ': ' + '; '.join(lst[:6]) + (' ...' if len(lst) > 6 else '') if lst else ''
ck(not bad['join'], f'{N - len(bad["join"])} of {N} LFNs joined by {join_max:.0f} s and kept a parent among their neighbours'
   + (f' (operational {min(opers):.0f}-{max(opers):.0f} s)' if opers else '') + show(bad['join']))
ck(not bad['down'], f'BR -> LFN unicast complete for {N - len(bad["down"])} of {N} LFNs' + show(bad['down']))
ck(not bad['up'], f'LFN -> BR unicast complete for {N - len(bad["up"])} of {N} LFNs' + show(bad['up']))
if ffn_lost:
    ck.info(f'{group}: requests a parent router never got by MPL (FFN loss, not counted against the LFNs): '
            + '; '.join(f'LFN {n} under {p}: {len(l)}' for n, (p, l) in sorted(ffn_lost.items())))
ck(not bad['group'], f'{group}: every request that reached the parent received once by {N - len(bad["group"])} of {N} LFNs ({len(gsent)} sent)' + show(bad['group']))
ck(not bad['win'], f'frames inside the windows for {N - len(bad["win"])} of {N} LFNs ({min(nrx) if nrx else 0}-{max(nrx) if nrx else 0} frames each, at least {min_rx})' + show(bad['win']))
load = {}
for n, p in parents.items():
    load[p] = load.get(p, 0) + 1
ck.info('LFNs per parent: ' + ', '.join(f'{p}: {c}' for p, c in sorted(load.items(), key=lambda x: (x[0] is None, x[0] or 0))))
qmax = {}
for r in [0] + routers:
    for t, m in lines(f'{out}/{"br" if r == 0 else f"node{r}"}.log'):
        x = re.search(r'Adaptation layer TX queue size (\d+)', m)
        if x:
            qmax[r] = max(qmax.get(r, 0), int(x.group(1)))
ck(not qmax, 'no adaptation-layer queue reached 20 frames' + (': ' + ', '.join(f'node {r}: {q}' for r, q in sorted(qmax.items())) if qmax else ''))
if delays:
    d = sorted(delays)
    ck(d[-1] <= delay_max, f'downlink delay (BR request to the LFN): median {statistics.median(d):.1f} s, '
       f'95th percentile {d[int(0.95 * (len(d) - 1))]:.1f} s, largest {d[-1]:.1f} s (limit {delay_max:.0f} s), {len(d)} downlinks')
sys.exit(ck.done())
PY
RC=$?
[ "${KEEP:-0}" = 1 ] || rm -rf "$OUT" "$OUT.run"
exit $RC
