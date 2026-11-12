#!/bin/bash
# The radio's side of the central medium (docs/MEDIUM.md §6), with real
# nodes: a border router and two routers that do not hear each other (hidden
# terminals at the border router), both at -60 dBm from it. Every router
# sends a UDP request to the border router every second from 150 s, which
# echoes it: their frames to the border router collide (equal power: lost by
# interference). From 400 s, router 2 is at -50 dBm (--vtime-link): 10 dB
# above router 1, its frames can capture the border router. Run of DUR (600)
# virtual seconds, under 2 s of wall time.
#
# Checks, the medium log (--medium-log) against each radio's log:
#   - every frame the radio took ("Receiving ... frame=N") is a frame the
#     server locked it onto (outcome ok, noise, interference, captured, left
#     or tx_abort for that receiver), and every such frame was taken, except
#     those still on air at the end;
#   - every loss line of the radio ("was lost: <cause> ... frame N") has the
#     server's outcome for that frame and receiver (captured, noise,
#     interference; left or tx_abort for a frame left without a verdict);
#   - the radio's exit counters ("rf medium: N received, lost ...") equal the
#     server's outcomes for that receiver; no frame refused, no verdict or
#     frame ignored;
#   - CCA: the radios' queries add up to the server's; every busy answer
#     ("CCA: CANNEL BUSY (E dBm, threshold T dBm)") has E >= T, and E is the
#     sum of the noise and of the powers of a non-empty set of the radio's
#     neighbours (a frame was on air; one on the next channel counts 10 dB
#     weaker, docs/MEDIUM.md §2.1); the MAC set the threshold (the
#     automatic threshold, at most -60 dBm), and the busy answers are at most
#     the server's queries that found a frame on air (before EM5 they were
#     equal, every link being at -60 dBm or stronger; a frame on the next
#     channel now adds 10 dB less, which can stay under the threshold);
#   - collisions: interference outcomes at the border router before 400 s,
#     captures only after 400 s, all of router 1's frames.
#
# usage: test_medium_radio.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: WSSIM_SEED (1), DUR (600), SIM_OPTS (more wssimserver
# options, e.g. --drift-random=20 as lfn_suite.sh sets; --medium=central is
# always given, after them). TUN is always none. The run uses the medium's
# basic preset (docs/MEDIUM.md §14, given before SIM_OPTS): the CCA check
# rebuilds each busy answer's energy from the links' fixed levels, which the
# calibrated default's fading changes frame by frame (test_channel.py checks
# the CCA energy with fading).
# Exit status 0 if every check passes.
set -u
[ $# = 3 ] || { echo "usage: test_medium_radio.sh <bindir> <workdir> <outdir>" >&2; exit 2; }
BIN=$1; WORK=$(realpath "$2"); OUT=$3
DUR=${DUR:-600}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_medium_radio: $OUT is not empty" >&2; exit 1; }
printf '0,1:-60\n0,2:-60\n' > "$OUT/topology.in"

TUN=none TOPO="$OUT/topology.in" LINK="0:2:400:-50" \
    SIM_OPTS="--medium-preset=basic ${SIM_OPTS:-} --medium=central --medium-log=$OUT/run/medium.log" \
    BR_OPTS="-o test_udp_echo=true" \
    NODE_OPTS="-o test_udp_dst=fd12:3456::1615:16ff:fe17:1819 -o test_udp_period_s=1 -o test_udp_start_s=150" \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT/run" "$DUR" > "$OUT/run.txt" 2>&1 ||
    { echo "test_medium_radio: run failed ($OUT/run.txt)"; exit 1; }

python3 - "$OUT/run" "$DUR" <<'EOF'
import collections, itertools, math, re, sys
out, dur = sys.argv[1], float(sys.argv[2])
t0 = float(open(f'{out}/t0').read())
NOISE = -153.930645 + 30 + 4
LINK_AT = 400
fails = []

def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        fails.append(what)

ansi = re.compile(r'\x1b\[[0-9;]*m')
if not any('medium: central' in l for l in open(f'{out}/simserver.log')):
    print('FAIL the server did not run the central medium'); sys.exit(1)

# The server: outcome of (frame, receiver), and its totals
rows = {}
for l in open(f'{out}/medium.log'):
    t_end, src, dst, fid, outcome, dbm, sinr = l.split()[:7]   # more columns when links vary (MEDIUM.md §12.6)
    rows[(int(fid), int(dst))] = (int(t_end), int(src), outcome, float(dbm))
srv = {}
for l in open(f'{out}/simserver.log'):
    m = re.search(r'medium: (\d+) CCA queries, (\d+) with a frame on air', l)
    if m:
        srv['cca'], srv['cca_energy'] = int(m[1]), int(m[2])
check('cca' in srv, 'server CCA summary')

LOCKED = {'ok', 'noise', 'interference', 'captured', 'left', 'tx_abort'}
logs = {0: 'br'} | {i: f'node{i}' for i in (1, 2)}
neighbours = {0: (1, 2), 1: (0,), 2: (0,)}
total_cca = total_busy = 0
for node, name in logs.items():
    took, lost, busy, counters, cca = {}, {}, [], None, None
    for l in open(f'{out}/{name}.log', errors='replace'):
        l = ansi.sub('', l)
        t, _, msg = l.partition(' ')
        try:
            t = float(t) - t0
        except ValueError:
            continue
        if m := re.search(r'Receiving \.\.\. .* frame=(\d+)', msg):
            took[int(m[1])] = t
        elif m := re.search(r'Packet from node (\d+) was lost: (.*) \(RSSI (\S+) dBm.* frame (\d+)\)', msg):
            lost[int(m[4])] = (int(m[1]), m[2], float(m[3]))
        elif m := re.search(r'CCA: CANNEL BUSY \((\S+) dBm, threshold (\S+) dBm\)', msg):
            busy.append((t, float(m[1]), int(m[2])))
        elif m := re.search(r'rf medium: (\d+) received, lost (\d+) noise, (\d+) interference, (\d+) captured, '
                            r'(\d+) without verdict; (\d+) refused, (\d+) verdicts and (\d+) frames ignored', msg):
            counters = [int(x) for x in m.groups()]
        elif m := re.search(r'rf medium: (\d+) CCA, (\d+) busy; CCA threshold (\S+) dBm \((\d+) set by the MAC\)', msg):
            cca = [int(x) for x in m.groups()]
    check(counters is not None and cca is not None, f'{name}: exit lines')
    if counters is None or cca is None:
        continue
    mine = {fid: r for (fid, dst), r in rows.items() if dst == node}
    locked = {fid for fid, r in mine.items() if r[2] in LOCKED}
    # Frames still on air at the end: taken, no outcome
    tail = {fid for fid, t in took.items() if fid not in mine and t > dur - 1}
    check(set(took) - tail == locked, f'{name}: {len(locked)} frames locked, {len(took)} taken '
          f'({len(tail)} at the end; differences {sorted((set(took) - tail) ^ locked)[:5]})')
    def cause(text):     # the radio's wording -> the server's outcome
        if text.startswith('captured by'):
            return 'captured'
        return text if text in ('noise', 'interference') else 'left'
    bad = [(fid, v, mine.get(fid)) for fid, v in lost.items()
           if fid not in tail and (fid not in mine or mine[fid][1] != v[0] or abs(mine[fid][3] - v[2]) > 0.01 or
                                   cause(v[1]) != {'tx_abort': 'left'}.get(mine[fid][2], mine[fid][2]))]
    check(not bad, f'{name}: {len(lost)} loss lines, each with the server\'s outcome, sender and power {bad[:3]}')
    n = collections.Counter(r[2] for r in mine.values())
    srv_counts = [n['ok'], n['noise'], n['interference'], n['captured'], n['left'] + n['tx_abort'], 0, 0, 0]
    check(counters == srv_counts, f'{name}: radio counters {counters} = server {srv_counts} '
          '(received, noise, interference, captured, without verdict, refused, ignored)')
    # CCA: every busy answer is a frame on air, at or above the threshold
    powers = lambda t: [(-50 if t >= LINK_AT and {node, nb} == {0, 2} else -60) for nb in neighbours[node]]
    # A neighbour's frame on the CCA's channel, or on the next channel 10 dB
    # weaker (docs/MEDIUM.md §2.1, adjacent channel since EM5)
    def on_air(t, e):
        p = powers(t)
        return any(abs(10 * math.log10(10 ** (NOISE / 10) + sum(10 ** ((x - a) / 10) for x, a in zip(s, adj))) - e) < 0.06
                   for k in range(1, len(p) + 1) for s in itertools.combinations(p, k)
                   for adj in itertools.product((0, 10), repeat=k))
    badb = [b for b in busy if b[1] < b[2] or not on_air(*b[:2])]
    check(len(busy) == cca[1] and not badb, f'{name}: {cca[1]} CCA busy, each a frame on air at or above the '
          f'threshold {badb[:3]}')
    check(cca[3] > 0 and -100 <= cca[2] <= -60, f'{name}: CCA threshold {cca[2]} dBm, set {cca[3]} times by the MAC')
    total_cca += cca[0]; total_busy += cca[1]
check(total_cca == srv.get('cca'), f'CCA queries: radios {total_cca}, server {srv.get("cca")}')
check(0 < total_busy <= srv.get('cca_energy', 0),
      f'CCA busy: radios {total_busy}, at most the server queries with a frame on air {srv.get("cca_energy")}')
# Collisions and captures at the border router
br = [r for (fid, dst), r in rows.items() if dst == 0]
intf = sum(1 for r in br if r[2] == 'interference' and r[0] < LINK_AT * 1e6)
caps = [r for r in br if r[2] == 'captured']
check(intf > 0, f'{intf} frames lost by interference at the border router before {LINK_AT} s')
check(caps and all(r[0] > LINK_AT * 1e6 and r[1] == 1 for r in caps),
      f'{len(caps)} captures at the border router, all after {LINK_AT} s, all of router 1\'s frames')
print('test_medium_radio: ' + ('PASSED' if not fails else f'FAILED ({len(fails)})'))
sys.exit(1 if fails else 0)
EOF
