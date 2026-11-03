#!/bin/bash
# FAN 1.1 LFN on weak links, in virtual time. The emulated PHY loses a frame
# with the probability 1 - (1 - BER)^bits, BER = 0.5 exp(0.5 (-119.93 - RSSI))
# (app_wshwsim/sl_rf_driver.c): for a 120-byte frame about 0.2 % at -95 dBm,
# 2.5 % at -100, 17 % at -104, 75 % at -108, all lost below -112. The stack's
# minimum sensitivity is -93 dBm (DEVICE_MIN_SENS, RSL 81), a good parent link
# is above -83 dBm (CAND_PARENT_THRESHOLD). Border router 0 and LFN 1, test
# traffic both ways (BR_PERIOD, LFN_PERIOD), each echoed.
#
# MODE=sweep: one run per RSSI of LEVELS, all at once: the LFN joins over a
# -80 dBm link, which weakens to the level at WEAK_AT s (run.sh LINK; an LFN
# does not join a parent below the minimum sensitivity, so the lossy levels
# can only be reached that way). For each level, the figures: frames lost on
# the link (both ways), the LFN's MAC retries (no ACK) and the parent's,
# parent searches and losses, requests delivered both ways from WEAK_AT.
# Checks: the LFN joined by JOIN_MAX s; from STRICT (-100 dBm) up, the LFN
# keeps its parent and every request arrives and is echoed (MAC retries and
# uplink restarts absorb the losses); below it, at least MIN_DELIVERY % of the
# requests are echoed. One more run starts at BELOW (-96 dBm, under the
# minimum sensitivity, though the PHY loses only 0.4 % of the frames there):
# the LFN must not join.
# MODE=fade: the link at RSSI (-60) fades to FADE_RSSI (-115: every frame lost)
# at FADE_AT s for FADE_FOR s (run.sh LINK). Checks: the LFN gives up its
# parent (parent loss declared, 1.5 x 5 sync periods after its last sync
# frame, or a parent search), is operational again within RECOVER s of the
# link's return, and every request from RECOVER s after the return to the
# end arrived and was echoed, as before the fade.
#
# usage: test_lfn_weak.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: MODE (sweep), LEVELS ("-90 -96 -100 -103 -106"), WEAK_AT
# (400), BELOW (-96), STRICT (-100), MIN_DELIVERY (90), RSSI (-60), FADE_RSSI (-115), FADE_AT (600),
# FADE_FOR (600), RECOVER (300), DUR (1800), WSSIM_SEED (1), TUN (none),
# BR_PERIOD (37), LFN_PERIOD (53), JOIN_MAX (300), SIM_OPTS
# (--drift-random=20), LFN_EXTRA, KEEP.
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
MODE=${MODE:-sweep}; LEVELS=${LEVELS:--90 -96 -100 -103 -106}; WEAK_AT=${WEAK_AT:-400}; BELOW=${BELOW:--96}; STRICT=${STRICT:--100}; MIN_DELIVERY=${MIN_DELIVERY:-90}
RSSI=${RSSI:--60}; FADE_RSSI=${FADE_RSSI:--115}; FADE_AT=${FADE_AT:-600}; FADE_FOR=${FADE_FOR:-600}; RECOVER=${RECOVER:-300}
DUR=${DUR:-1800}; TUN=${TUN:-none}; BR_PERIOD=${BR_PERIOD:-37}; LFN_PERIOD=${LFN_PERIOD:-53}; JOIN_MAX=${JOIN_MAX:-300}
SIM_OPTS=${SIM_OPTS---drift-random=20}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_weak: $OUT is not empty" >&2; exit 1; }
BR_ADDR=fd12:3456::1615:16ff:fe17:1819
one() {   # one <dir> <rssi> [LINK spec]
    echo "0,1:$2" > "$1.topo"
    env ${3:+LINK="$3"} \
    NODE_OPTS_1="-o device_type=lfn -o enable_lfn=false -o test_udp_echo=true -o test_udp_dst=$BR_ADDR -o test_udp_period_s=$LFN_PERIOD -o test_udp_start_s=70 ${LFN_EXTRA:-}" \
    BR_OPTS="-o test_udp_echo=true -o test_udp_dst=fd12:3456::302:304:506:1 -o test_udp_period_s=$BR_PERIOD -o test_udp_start_s=60" \
    CONF=lfn TOPO="$1.topo" TUN=$TUN SIM_OPTS="$SIM_OPTS" "$HERE/run.sh" "$BIN" "$WORK" "$1" "$DUR" > "$1.run" 2>&1
    local rc=$?
    rm -f "$1.topo"
    return $rc
}
RUNS=()
case $MODE in
sweep)
    PIDS=()
    for l in $LEVELS; do one "$OUT/rssi$l" -80 "0:1:$WEAK_AT:$l" & PIDS+=($!); RUNS+=("$l"); done
    one "$OUT/below" "$BELOW" & PIDS+=($!)
    RC=0; for p in "${PIDS[@]}"; do wait "$p" || RC=1; done ;;
fade)
    one "$OUT/fade" "$RSSI" "0:1:$FADE_AT:$FADE_RSSI 0:1:$((FADE_AT + FADE_FOR)):$RSSI"; RC=$?; RUNS=(fade) ;;
*) echo "test_lfn_weak: MODE sweep or fade" >&2; exit 2 ;;
esac
[ $RC = 0 ] || { echo "test_lfn_weak: a run failed ($OUT/*.run)"; [ "${KEEP:-0}" = 1 ] || rm -rf "$OUT"; exit 1; }

PYTHONPATH=$HERE python3 - "$OUT" "$MODE" "$DUR" "$JOIN_MAX" "$STRICT" "$MIN_DELIVERY" "$FADE_AT" "$FADE_FOR" "$RECOVER" "$WEAK_AT" "$BELOW" "${RUNS[@]}" <<'PY'
import re, sys
from lfn_lib import Checks, lines, udp, exchange
out, mode = sys.argv[1], sys.argv[2]
dur, join_max, strict, min_del, fade_at, fade_for, recover = (float(x) for x in sys.argv[3:10])
weak_at, below = float(sys.argv[10]), sys.argv[11]
runs = sys.argv[12:]
ck = Checks(f'test_lfn_weak ({mode})')
def stats(d):
    L = {'oper': [], 'lost': [], 'search': [], 'auth': [], 'noack': 0, 'linkloss': 0}
    for t, m in lines(f'{d}/node1.log'):
        if 'LFN operational' in m:
            L['oper'].append(t)
        elif re.search(r'parent .* lost', m):
            L['lost'].append(t)
        elif 'LFN: parent search (' in m:
            L['search'].append(t)
        elif 'LFN authentication start' in m:
            L['auth'].append(t)
        elif m.endswith(' no_ack'):
            L['noack'] += 1
        elif 'was lost due to link quality' in m:
            L['linkloss'] += 1
    B = {'noack': 0, 'linkloss': 0, 'rx': 0}
    for t, m in lines(f'{d}/br.log'):
        if m.endswith(' no_ack'):
            B['noack'] += 1
        elif 'was lost due to link quality' in m:
            B['linkloss'] += 1
    return L, B, udp(f'{d}/br.log'), udp(f'{d}/node1.log')
def deliver(br, lf, a, b):
    d = exchange(br, lf, None, a, b)
    u = exchange(lf, br, None, a, b)
    return d, u
if mode == 'sweep':
    for lvl in sorted(runs, key=float, reverse=True):
        L, B, br, lf = stats(f'{out}/rssi{lvl}')
        oper = L['oper'][0] if L['oper'] else None
        ck(oper is not None and oper <= join_max, f'{lvl} dBm: operational at {oper:.1f} s' if oper else f'{lvl} dBm: not operational')
        if oper is None:
            continue
        (dq, dg, db), (uq, ug, ub) = deliver(br, lf, max(oper + 15, weak_at), dur - 15)
        print(f'info {lvl} dBm: frames lost on the link: {B["linkloss"]} at the border router, {L["linkloss"]} at the LFN; '
              f'MAC retries (no ACK): LFN {L["noack"]}, border router {B["noack"]}; parent searches {len(L["search"])}, '
              f'losses {len(L["lost"])}, EAPOL {len(L["auth"])}')
        txt = f'{lvl} dBm: BR -> LFN {len(db)}/{len(dq)} echoed, LFN -> BR {len(ub)}/{len(uq)} echoed'
        if float(lvl) >= strict:
            ck(dq and uq and len(db) == len(dq) and len(ub) == len(uq) and not L['lost'] and len(L['oper']) == 1,
               txt + f', parent kept ({len(L["lost"])} losses)')
        else:
            ck(dq and uq and len(db) >= min_del / 100 * len(dq) and len(ub) >= min_del / 100 * len(uq),
               txt + f' (at least {min_del:.0f} %)')
    L, B, br, lf = stats(f'{out}/below')
    ck(not L['oper'], f'{below} dBm from the start (under the minimum sensitivity): '
       + (f'joined at {L["oper"][0]:.1f} s' if L['oper'] else f'not joined, {L["linkloss"]} frames lost on the link at the LFN'))
else:
    L, B, br, lf = stats(f'{out}/fade')
    back = fade_at + fade_for
    oper = L['oper'][0] if L['oper'] else None
    ck(oper is not None and oper <= join_max, f'operational at {oper:.1f} s' if oper else 'not operational')
    gave = [t for t in L['lost'] + L['search'] if fade_at < t < back]
    lost = [t for t in L['lost'] if fade_at < t < back]
    ck(gave, f'during the fade: ' + (f'parent lost {lost[0] - fade_at:.1f} s after it began, ' if lost else 'no parent loss, ')
       + f'{len([t for t in L["search"] if fade_at < t < back])} parent searches')
    again = [t for t in L['oper'] if t > back - 1]
    ck(again and again[0] - back <= recover, f'operational again {again[0] - back:.1f} s after the link returned'
       + (f' ({len([t for t in L["auth"] if t > fade_at])} EAPOL)' if again else '') if again else 'operational again')
    print(f'info MAC retries (no ACK): LFN {L["noack"]}, border router {B["noack"]}; frames lost on the link: '
          f'{B["linkloss"]} at the border router, {L["linkloss"]} at the LFN')
    for name, a, b in (('before the fade', (oper or dur) + 15, fade_at), ('after the return', back + recover, dur - 15)):
        (dq, dg, db), (uq, ug, ub) = deliver(br, lf, a, b)
        ck(dq and uq and len(db) == len(dq) and len(ub) == len(uq),
           f'{name} ({a:.0f}-{b:.0f} s): BR -> LFN {len(db)}/{len(dq)} echoed, LFN -> BR {len(ub)}/{len(uq)} echoed')
sys.exit(ck.done())
PY
RC=$?
[ "${KEEP:-0}" = 1 ] || rm -rf "$OUT" "$OUT".run
exit $RC
