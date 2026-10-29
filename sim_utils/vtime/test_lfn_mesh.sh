#!/bin/bash
# FAN 1.1 LFNs at the edge of a mesh, in virtual time (GS8 stage 5; the
# stack's docs/LFN_STACK.md). The dag_simple topology (border router 0,
# routers 1-14, every router an LFN parent: enable_lfn) plus six LFNs
# (15-20), each hearing two or three routers; LFN 20 also hears the border
# router, weakly. Test traffic: the border router sends a UDP request to
# every LFN every BR_PERIOD s, each LFN one to the border router every
# LFN_PERIOD s; each side echoes what it receives.
#
# Checks, from the logs:
#   - every LFN joined (by JOIN_MAX s) and kept its parent, one of its
#     neighbours in the topology;
#   - unicast both ways for every LFN: the requests and their echoes arrived
#     (requests from 15 s after the LFN became operational: the route to it
#     reaches the border router with its parent's DAO, RPL's DAO delay later; to DUR - 15 s);
#   - every frame each LFN received started inside one of its windows
#     (unicast, LFN broadcast, discovery), ACKs to its own frames aside.
# It prints each LFN's parent, its routing cost when chosen, its join time
# and its average current from STEADY_FROM s (EM2 ledger, the profile given).
#
# usage: test_lfn_mesh.sh <bindir> <workdir(holds examples/)> <outdir> [profile]
# Environment: DUR (1800), WSSIM_SEED (1), TUN (tunvt0), BR_PERIOD (60),
# LFN_PERIOD (90), STEADY_FROM (600), JOIN_MAX (400), SIM_OPTS (more
# wssimserver options, e.g. --drift-random=20), LFN_EXTRA, ROUTER_EXTRA.
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
PROFILE=${4:-$WORK/examples/power/efr32fg25-soc.ini}
DUR=${DUR:-1800}; TUN=${TUN:-tunvt0}
BR_PERIOD=${BR_PERIOD:-60}; LFN_PERIOD=${LFN_PERIOD:-90}
STEADY_FROM=${STEADY_FROM:-600}; JOIN_MAX=${JOIN_MAX:-400}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_mesh: $OUT is not empty" >&2; exit 1; }
DAG=$(grep '^DAG_SIMPLE=' "$HERE/run.sh" | cut -d'"' -f2)
LFN_EDGES="15,6:-50 15,4:-70 16,11:-50 16,8:-65 17,12:-50 17,13:-60 18,14:-50 18,10:-60 19,5:-55 19,2:-70 20,3:-55 20,0:-85 20,8:-75"
tr ' ' '\n' <<< "$DAG $LFN_EDGES" > "$OUT.topo"
LFNS="15 16 17 18 19 20"
lfn_addr() { printf 'fd12:3456::302:304:506:%x' "$1"; }
BR_ADDR=fd12:3456::1615:16ff:fe17:1819
BR_TRAFFIC="-o test_udp_echo=true -o test_udp_period_s=$BR_PERIOD -o test_udp_start_s=120"
for i in $LFNS; do BR_TRAFFIC+=" -o test_udp_dst=$(lfn_addr $i)"; done
for i in $LFNS; do
    export NODE_OPTS_$i="-o device_type=lfn -o enable_lfn=false -o test_udp_echo=true -o test_udp_dst=$BR_ADDR -o test_udp_period_s=$LFN_PERIOD -o test_udp_start_s=$((130 + i)) ${LFN_EXTRA:-}"
done
BR_OPTS="$BR_TRAFFIC" \
NODE_OPTS="-o device_type=ffn -o enable_lfn=true ${ROUTER_EXTRA:-}" \
CONF=lfn TOPO="$OUT.topo" TUN=$TUN POWER=$PROFILE POWER_LOG=1 \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > "$OUT.run" 2>&1
RC=$?
rm -f "$OUT.topo"
[ $RC = 0 ] || { echo "test_lfn_mesh: run failed ($OUT.run)"; exit 1; }

python3 - "$OUT" "$DUR" "$STEADY_FROM" "$JOIN_MAX" "$LFNS" <<'EOF'
import json, re, sys, bisect
out, dur, steady_from, join_max = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
lfns = [int(x) for x in sys.argv[5].split()]
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
edges = set()
for l in open(f'{out}/topology.txt'):
    a, b = l.split(':')[0].split(',')
    edges.add(frozenset((int(a), int(b))))
def eui_node(e):
    return int(e.split(':')[-2] + e.split(':')[-1], 16)
br = {'tx': [], 'rx': []}
for t, m in lines('br.log'):
    r = re.search(r'test-udp (tx to|rx from) (\S+): (\S+ \d+)', m)
    if r:
        br['tx' if r.group(1) == 'tx to' else 'rx'].append((t, r.group(2), r.group(3)))
rep = json.load(open(f'{out}/power.json'))
pwr = {}
for line in open(f'{out}/power_log.txt'):
    pass
for n in lfns:
    d = {'disc': 0, 'lost': 0, 'oper': None, 'parent': None, 'cost': None, 'win': [], 'rx': [], 'tx': [], 'utx': [], 'urx': []}
    for t, m in lines(f'node{n}.log'):
        if 'LFN discovery start' in m:
            d['disc'] += 1
        elif re.search(r'parent .* lost', m):
            d['lost'] += 1
        elif 'LFN operational' in m and d['oper'] is None:
            d['oper'] = t
        elif 'parent chosen:' in m:
            r = re.search(r'chosen: (\S+) \(routing cost (\d+)', m)
            d['parent'], d['cost'] = r.group(1), int(r.group(2))
        elif 'window open' in m and 'for ' in m:
            d['win'].append((t, int(re.search(r'for (\d+) us', m).group(1)) / 1e6))
        elif m.startswith('Transmitting ...'):
            d['tx'].append(t)
        elif m.startswith('Receiving ...') and int(re.search(r'pkt_len=(\d+)', m).group(1)) >= 30:
            d['rx'].append(t)
        r = re.search(r'test-udp (tx to|rx from) (\S+): (\S+ \d+)', m)
        if r:
            d['utx' if r.group(1) == 'tx to' else 'urx'].append((t, r.group(2), r.group(3)))
    addr = f'fd12:3456::302:304:506:{n:x}'
    oper = d['oper']
    pn = eui_node(d['parent']) if d['parent'] else None
    check(oper is not None and oper <= join_max and pn is not None and frozenset((n, pn)) in edges and d['disc'] == 1 and not d['lost'],
          f"LFN {n}: operational at {oper if oper is None else round(oper, 1)} s, parent {pn} (routing cost {d['cost']}), "
          f"{d['disc']} discovery, {d['lost']} parent losses")
    last = dur - 15
    if oper is not None:
        q = [p for t, a, p in br['tx'] if a == addr and p.startswith('Q') and oper + 15 < t < last]
        got = {p for t, a, p in d['urx']}
        back = {p.replace('R', 'Q', 1) for t, a, p in br['rx'] if a == addr and p.startswith('R')}
        check(q and all(p in got for p in q) and all(p in back for p in q),
              f"LFN {n}: BR -> LFN {len(q)} requests, {sum(p in got for p in q)} received, {sum(p in back for p in q)} echoes back")
        q = [p for t, a, p in d['utx'] if p.startswith('Q') and oper + 15 < t < last]
        got = {p for t, a, p in br['rx'] if a == addr}
        back = {p.replace('R', 'Q', 1) for t, a, p in d['urx'] if p.startswith('R')}
        check(q and all(p in got for p in q) and all(p in back for p in q),
              f"LFN {n}: LFN -> BR {len(q)} requests, {sum(p in got for p in q)} received, {sum(p in back for p in q)} echoes back")
    win = sorted(d['win'])
    wt = [w[0] for w in win]
    def in_window(t):
        i = bisect.bisect_right(wt, t + 1e-6) - 1
        return i >= 0 and win[i][0] - 1e-6 <= t < win[i][0] + win[i][1]
    txs = sorted(d['tx'])
    def after_own_tx(t):
        i = bisect.bisect_right(txs, t) - 1
        return i >= 0 and t - txs[i] < 0.06
    outside = [t for t in d['rx'] if not in_window(t) and not after_own_tx(t)]
    check(not outside, f"LFN {n}: {len(d['rx'])} frames received, {len(outside)} outside its windows")
    node = next(x for x in rep['nodes'] if x['node'] == n)
    pwr[n] = node
# Average current from steady_from, per LFN, from the power log
ma = {}
for n in lfns:
    x = pwr[n]
    ma[n] = ({('mcu', k): v['ma'] for k, v in x['mcu']['states'].items()} |
             {('radio', k): v['ma'] for k, v in x['radio']['states'].items() if 'ma' in v},
             {lv['dbm']: lv['ma'] for lv in x['radio']['states']['tx']['levels']}, x['mcu']['wake_charge']['uc_each'])
charge = {n: 0.0 for n in lfns}
wakes = {n: 0 for n in lfns}
t0, t1 = steady_from * 1e6, dur * 1e6
for line in open(f'{out}/power_log.txt'):
    if line.startswith('#'):
        continue
    f = line.split()
    node = int(f[2])
    if node not in charge:
        continue
    s, e = float(f[0]), float(f[1])
    if e <= t0 or s >= t1:
        continue
    if f[3] == 'mcu' and f[4] == 'active' and s >= t0:
        wakes[node] += 1
    s, e = max(s, t0), min(e, t1)
    states, tx, _ = ma[node]
    cur = tx[int(f[5])] if f[4] == 'tx' else states.get((f[3], f[4]), 0)
    charge[node] += (e - s) * cur / 1e3
span = (t1 - t0) / 1e6
for n in lfns:
    total = charge[n] + wakes[n] * ma[n][2]
    print(f'info LFN {n}: {total / span:.1f} uA average from {steady_from:.0f} s, {wakes[n] / span:.2f} wakes/s')
print('test_lfn_mesh: ' + ('PASSED' if not fails else f'FAILED ({len(fails)})'))
sys.exit(1 if fails else 0)
EOF
