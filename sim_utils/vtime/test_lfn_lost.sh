#!/bin/bash
# Lost registration answers and lost releases of an LFN, in virtual time.
# Each test makes a pilot run, finds the frame to lose in it (virtual-time
# runs are deterministic: the run that follows is the same up to that frame),
# then makes the run with the loss (run.sh DROP or LINK).
#
# MODE=na: border router 0, LFN 1 under it. The NA that answers the LFN's
#   address registration (EARO), the first frame the border router sends the
#   LFN after it, and its MAC retries for 25 listen intervals, are lost
#   (DROP). Checks: no NA within
#   na_wait_s, so the LFN registers again ("no NA ... registering again",
#   na_wait_s after its registration, within a listen interval), that
#   registration is answered (no parent search follows), and the traffic both
#   ways is complete from the second registration on.
# MODE=na2: the NAs of both registrations are lost (a second pilot finds the
#   second): the LFN starts a parent search na_wait_s after its second
#   registration, finds its parent again, registers, and the traffic is
#   complete again from SETTLE s after the search.
# MODE=release: border router 0, routers 1 and 2, LFN 3 under router 1 (router
#   2 answers LFNs only from 400 s). The link router 1 - LFN goes dead
#   (LINK -140 dBm) from MOVE_AT (600) to MOVE_AT + 90 s, so the LFN moves to
#   router 2; it then releases router 1 (NS with lifetime 0, acknowledged at
#   the MAC, sent again every 10 s, 4 tries). The link is dead again around
#   the first LOST (2) tries and their MAC retries (until 0.5 s before the
#   next try). Checks: the tries are 10 s apart, the release is
#   acknowledged at try LOST + 1 and router 1 drops the LFN (LOST < 4), or it
#   is given up after 4 tries (LOST >= 4); in both cases the border router's
#   route to the LFN never goes back to router 1 (no "External target ...
#   now from" router 1 after the move; router 1's later DAOs are stale), and
#   every request from 30 s after the move to the end is echoed.
#
# PROF (test, balanced or eco; default: the stack's defaults, the Test
# profile's values) gives the LFN and its parents a Silicon Labs profile
# (lfn_profile.sh), with its NA wait (5 min, 60 min, 120 min).
#
# usage: test_lfn_lost.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: MODE (na), PROF, WSSIM_SEED (1), TUN (none), LOST (2),
# MOVE_AT (600), SETTLE (600), BR_PERIOD (37; balanced 90, eco 450),
# LFN_PERIOD (53; balanced 135, eco 675), SIM_OPTS (--drift-random=20), KEEP.
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
MODE=${MODE:-na}; TUN=${TUN:-none}; LOST=${LOST:-2}; MOVE_AT=${MOVE_AT:-600}; SETTLE=${SETTLE:-600}
SIM_OPTS=${SIM_OPTS---drift-random=20}
HERE=$(cd "$(dirname "$0")" && pwd)
LFN_PROF=; PARENT_PROF=; NAW=300; P=(37 53); LI=6000
if [ -n "${PROF:-}" ]; then
    . "$HERE/lfn_profile.sh"
    case $PROF in balanced) P=(90 135) ;; eco) P=(450 675) ;; esac
fi
# The lost NA's MAC retries go on for the frame's TTL (tx_ttl_intervals, 20
# listen intervals): drop them for 25
DROPWIN=$(( 25 * LI / 1000 > 120 ? 25 * LI / 1000 : 120 ))
BR_PERIOD=${BR_PERIOD:-${P[0]}}; LFN_PERIOD=${LFN_PERIOD:-${P[1]}}
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_lost: $OUT is not empty" >&2; exit 1; }
BR_ADDR=fd12:3456::1615:16ff:fe17:1819
case $MODE in
na|na2) LFN=1; DUR=$((1200 + 3 * NAW + SETTLE)); printf '0,1:-50\n' > "$OUT/topo" ;;
release) LFN=3; DUR=$((MOVE_AT + (LOST >= 4 ? 2400 : 1500))); printf '0,1:-50\n0,2:-50\n1,3:-50\n2,3:-60\n' > "$OUT/topo"
    export NODE_OPTS_1="-o device_type=ffn -o enable_lfn=true $PARENT_PROF"
    export NODE_OPTS_2="$NODE_OPTS_1 -o test_lfn_silent_at_s=1 -o test_lfn_silent_for_s=399" ;;
*) echo "test_lfn_lost: MODE na, na2 or release" >&2; exit 2 ;;
esac
export NODE_OPTS_$LFN="-o device_type=lfn -o enable_lfn=false $LFN_PROF -o test_udp_echo=true -o test_udp_dst=$BR_ADDR -o test_udp_period_s=$LFN_PERIOD -o test_udp_start_s=70"
export BR_OPTS="-o test_udp_echo=true -o test_udp_dst=fd12:3456::302:304:506:$LFN -o test_udp_period_s=$BR_PERIOD -o test_udp_start_s=60 $PARENT_PROF"
run() {   # run <dir> [DROP/LINK assignments]
    local d=$1; shift
    env "$@" CONF=lfn TOPO="$OUT/topo" TUN=$TUN FRAMES=1 SIM_OPTS="$SIM_OPTS" \
        "$HERE/run.sh" "$BIN" "$WORK" "$d" "$DUR" > "$d.run" 2>&1 || { echo "test_lfn_lost: run failed ($d.run)"; exit 1; }
}
finish() { [ "${KEEP:-0}" = 1 ] || rm -rf "$OUT"; exit "$1"; }
# The frame to lose: the first data frame from <src> to the LFN after the
# n-th "LFN: ARO" of <dir>'s LFN log: "<seq> <time>"
pick() {   # pick <dir> <n>
    PYTHONPATH=$HERE python3 - "$1" "$2" "$LFN" <<'PY'
import sys
from lfn_lib import lines, frames, mac
d, n, lfn = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
aro = [t for t, m in lines(f'{d}/node{lfn}.log') if 'LFN: ARO ' in m and ' 0 min' not in m]
if len(aro) < n:
    sys.exit(1)
dst = mac(lfn).replace(':', '')
f = next(((t, x) for t, s, ch, x in frames(f'{d}/frames.txt') if t > aro[n - 1] and x['dst'] == dst and x['type'] == 1), None)
if not f:
    sys.exit(1)
print(f[1]['seq'], f'{aro[n - 1]:.6f}')
PY
}
EV=()
case $MODE in
na|na2)
    run "$OUT/pilot1"
    read -r S1 T1 < <(pick "$OUT/pilot1" 1) || { echo "test_lfn_lost: no registration in the pilot"; finish 1; }
    EV=(DROP="0:$LFN:$S1:$T1:$(echo "$T1 + $DROPWIN" | bc)")
    if [ $MODE = na2 ]; then
        run "$OUT/pilot2" "${EV[@]}"
        read -r S2 T2 < <(pick "$OUT/pilot2" 2) || { echo "test_lfn_lost: no second registration in the pilot"; finish 1; }
        EV=(DROP="0:$LFN:$S1:$T1:$(echo "$T1 + $DROPWIN" | bc) 0:$LFN:$S2:$T2:$(echo "$T2 + $DROPWIN" | bc)")
    fi ;;
release)
    L1="1:3:$MOVE_AT:-140 1:3:$((MOVE_AT + 90)):-50"
    run "$OUT/pilot1" LINK="$L1"
    TR=$(sed 's/\x1b\[[0-9;]*m//g' "$OUT/pilot1/node3.log" | awk '/old parent .* released \(try 1\)/ {print $1 - 1700000000; exit}')
    [ -n "$TR" ] || { echo "test_lfn_lost: no release in the pilot"; finish 1; }
    EV=(LINK="$L1 1:3:$(echo "$TR - 1" | bc):-140 1:3:$(echo "$TR + 10 * $LOST - 0.5" | bc):-50") ;;
esac
run "$OUT/run" "${EV[@]}"
echo "info events: ${EV[*]}"

PYTHONPATH=$HERE python3 - "$OUT/run" "$MODE" "$DUR" "$LFN" "$NAW" "$SETTLE" "$MOVE_AT" "$LOST" <<'PY'
import re, sys
from lfn_lib import Checks, lines, udp, exchange, listen_interval_s, mac
out, mode = sys.argv[1], sys.argv[2]
dur = float(sys.argv[3]); lfn = int(sys.argv[4]); naw, settle, move_at = (float(x) for x in sys.argv[5:8]); lost = int(sys.argv[8])
ck = Checks(f'test_lfn_lost ({mode})')
iv = listen_interval_s(f'{out}/node{lfn}.log')
L = {k: [] for k in ('aro', 'again', 'search', 'oper', 'auth', 'lost', 'rel', 'relok', 'relfail', 'parents')}
for t, m in lines(f'{out}/node{lfn}.log'):
    if 'LFN: ARO ' in m and ' 0 min' not in m:
        L['aro'].append(t)
    elif 'no NA for our registration in' in m:
        L['again'].append(t)
    elif 'LFN: parent search (' in m or 'no NA for our registration: parent search' in m:
        L['search'].append((t, m))
    elif 'LFN operational' in m:
        L['oper'].append(t)
    elif 'LFN authentication start' in m:
        L['auth'].append(t)
    elif re.search(r'parent .* lost', m):
        L['lost'].append(t)
    elif re.search(r'old parent \S+ released \(try (\d+)\)', m):
        L['rel'].append((t, int(re.search(r'try (\d+)', m).group(1))))
    elif 'released (acknowledged' in m:
        L['relok'].append((t, int(re.search(r'acknowledged, (\d+) tries', m).group(1))))
    elif 'release not acknowledged' in m:
        L['relfail'].append(t)
    r = re.search(r'LFN: parent (\S+), PAN', m)
    if r:
        L['parents'].append((t, r.group(1)))
ck(L['oper'], f"LFN operational at {L['oper'][0]:.1f} s" if L['oper'] else 'LFN operational')
br, lf = udp(f'{out}/br.log'), udp(f'{out}/node{lfn}.log')
end = dur - 15 - iv
def traffic(name, a):
    for way, tx, rx, peer in (('BR -> LFN', br, lf, None), ('LFN -> BR', lf, br, None)):
        q, got, back = exchange(tx, rx, peer, a, end)
        ck(q and len(back) == len(q), f'{way} {name} ({a:.0f}-{end:.0f} s): {len(q)} requests, {len(back)} echoed')
if mode in ('na', 'na2'):
    a1 = L['aro'][0] if L['aro'] else None
    ag = L['again']
    ck(a1 is not None and ag and abs(ag[0] - a1 - naw) <= iv + 1,
       f"no NA: registered again {ag[0] - a1:.1f} s after the first registration (na_wait_s {naw:.0f} s)" if a1 and ag else 'registered again')
    a2 = [t for t in L['aro'] if ag and t >= ag[0]]
    srch = [t for t, m in L['search'] if 'no NA' in m]
    if mode == 'na':
        ck(a2 and not L['search'] and not L['lost'] and len(ag) == 1,
           f"second registration at {a2[0]:.1f} s answered: no parent search, {len(L['lost'])} parent losses" if a2 else 'second registration')
        traffic('from the second registration', (a2[0] if a2 else dur) + 15)
    else:
        ck(a2 and srch and abs(srch[0] - a2[0] - naw) <= iv + 1,
           f"second NA lost too: parent search {srch[0] - a2[0]:.1f} s after the second registration" if a2 and srch else f"parent search after the second registration ({len(a2)} registrations, searches {[round(t) for t, m in L['search']]})")
        later = [t for t in L['aro'] if srch and t > srch[0]]
        ck(later and len(L['auth']) == 1, f"registered again {later[0] - srch[0]:.1f} s after the search, {len(L['auth'])} EAPOL in all" if later else 'registered after the search')
        traffic(f'from {settle:.0f} s after the search', (srch[0] if srch else dur) + settle)
else:
    R1, R2 = mac(1), mac(2)
    mv = [t for t, p in L['parents'] if t > move_at and p == R2]
    ck(mv, f"moved to router 2 {mv[0] - move_at:.1f} s after the link to router 1 went dead" if mv else 'moved to router 2')
    tries = [t for t, n in L['rel']]
    gaps = [b - a for a, b in zip(tries, tries[1:])]
    print(f"info release tries at {', '.join(f'{t:.1f}' for t in tries)} s")
    if lost < 4:
        ok = L['relok'] and L['relok'][0][1] == lost + 1 and len(tries) == lost + 1 and all(abs(g - 10) < 1.5 for g in gaps)
        ck(ok, f"release: {len(tries)} tries, gaps {', '.join(f'{g:.1f}' for g in gaps)} s, "
           + (f"acknowledged at try {L['relok'][0][1]}" if L['relok'] else 'not acknowledged') + f" (expected at try {lost + 1})")
        r1 = [t for t, m in lines(f'{out}/node1.log') if 'released its registrations' in m]
        ck(r1, f"router 1 dropped the LFN's registrations at {r1[0]:.1f} s" if r1 else "router 1 dropped the LFN's registrations")
    else:
        ck(len(tries) == 4 and L['relfail'] and all(abs(g - 10) < 1.5 for g in gaps),
           f"release given up after {len(tries)} tries (gaps {', '.join(f'{g:.1f}' for g in gaps)} s)")
    addr = f'fd12:3456::302:304:506:{lfn:x}'
    back = [(t, m) for t, m in lines(f'{out}/br.log') if f'External target {addr}' in m and 'now from' in m and m.rstrip().endswith('506:1') and mv and t > mv[0]]
    stale = sum(1 for t, m in lines(f'{out}/br.log') if 'Ignoring stale path' in m and addr in m)
    daos = sum(1 for t, m in lines(f'{out}/br.log') if mv and t > mv[0] + 60 and m.endswith('DAO from fd12:3456::302:304:506:1'))
    ck(not back and (lost < 4 or daos > 0), f"the border router's route to the LFN stayed on router 2 ({daos} DAOs from router 1 "
       f"after the move, {stale} stale paths to the LFN ignored)" + (f": back to router 1 at {back[0][0]:.1f} s" if back else '')
       + ('' if lost < 4 or daos else ': no DAO from router 1 to test it with'))
    traffic('after the move', (mv[0] if mv else dur) + 30)
sys.exit(ck.done())
PY
finish $?
