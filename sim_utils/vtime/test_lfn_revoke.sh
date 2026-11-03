#!/bin/bash
# LGTK lifecycle on a node revocation, and the LFN key lifetimes, in virtual
# time (the stack's docs/LFN_STACK.md §8). Border router 0, LFNs 1 and 2
# under it. Short LGTK timers so that the run shows a whole cycle: LGTK
# lifetime LIFETIME (3600 s), installed at 50 %, activated lifetime / 3
# before expiry, revocation reduction 30 (lifetime / 30 = 120 s); the LFNs
# fetch a new LGTK 30 s after they see its hash. At REVOKE_AT (600 s) the
# border router revokes LFN 2 (ws_bbr_node_keys_remove(),
# ws_bbr_node_access_revoke_start(): test_revoke_*). Test traffic both ways
# with each LFN.
#
# Checks:
#   - both LFNs joined; the border router gave each a PMK and a PTK with the
#     LFN lifetimes (lpmk_lifetime_s, lptk_lifetime_s: 18 and 12 months);
#   - revocation (Wi-SUN FAN 1.1 6.5.2.5): the active LGTK's lifetime cut to
#     lifetime / 30, the other LGTKs removed, a new LGTK installed;
#   - the active LGTK expired that cut lifetime later and the new one became
#     active; LFN 1 had it before then;
#   - LFN 2's keys were deleted: its next key exchange needed a full
#     authentication (EAP-TLS);
#   - LFN 1's traffic both ways: every request from 15 s after it joined to
#     the end (less 15 s) received and echoed.
#
# usage: test_lfn_revoke.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: DUR (1500), REVOKE_AT (600), LIFETIME (3600), WSSIM_SEED (1),
# TUN (none), SIM_OPTS (--drift-random=20), BR_PERIOD (37), LFN_PERIOD (53),
# KEEP.
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
DUR=${DUR:-1500}; REVOKE_AT=${REVOKE_AT:-600}; LIFETIME=${LIFETIME:-3600}; TUN=${TUN:-none}
BR_PERIOD=${BR_PERIOD:-37}; LFN_PERIOD=${LFN_PERIOD:-53}
SIM_OPTS=${SIM_OPTS---drift-random=20}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_revoke: $OUT is not empty" >&2; exit 1; }
printf '0,1:-50\n0,2:-50\n' > "$OUT.topo"
BR_ADDR=fd12:3456::1615:16ff:fe17:1819
LFN="-o device_type=lfn -o enable_lfn=false -o lfn_lgtk_max_mismatch_s=30 -o test_udp_echo=true -o test_udp_dst=$BR_ADDR -o test_udp_period_s=$LFN_PERIOD -o test_udp_start_s=70"
export NODE_OPTS_1="$LFN" NODE_OPTS_2="$LFN"
BR_OPTS="-o test_udp_echo=true -o test_udp_dst=fd12:3456::302:304:506:1 -o test_udp_dst=fd12:3456::302:304:506:2 -o test_udp_period_s=$BR_PERIOD -o test_udp_start_s=60
 -o lfn_lgtk_lifetime_s=$LIFETIME -o lfn_lgtk_new_install_required=50 -o lfn_lgtk_new_activation_time=3 -o lfn_lgtk_revocation_reduction=30
 -o test_revoke_at_s=$REVOKE_AT -o test_revoke_eui64=01:02:03:04:05:06:00:02" \
CONF=lfn TOPO="$OUT.topo" TUN=$TUN SIM_OPTS="$SIM_OPTS" \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > "$OUT.run" 2>&1
RC=$?
rm -f "$OUT.topo"
[ $RC = 0 ] || { echo "test_lfn_revoke: run failed ($OUT.run)"; exit 1; }
rm -f "$OUT.run"

python3 - "$OUT" "$DUR" "$REVOKE_AT" "$LIFETIME" <<'EOF'
import re, sys
out, dur, revoke_at, lifetime = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
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
br = list(lines('br.log'))
pmk = [int(re.search(r'LFN PMK lifetime (\d+)', m).group(1)) for t, m in br if 'LFN PMK lifetime' in m]
ptk = [int(re.search(r'LFN PTK lifetime (\d+)', m).group(1)) for t, m in br if 'LFN PTK lifetime' in m]
check(len(pmk) >= 2 and all(v == 788400 * 60 for v in pmk) and len(ptk) >= 2 and all(v == 525600 * 60 for v in ptk),
      f'LFN PMK lifetimes {sorted(set(pmk))} s, PTK {sorted(set(ptk))} s (18 and 12 months)')
rev = [(t, m) for t, m in br if 'LGTK revocation' in m]
cut = [(t, int(re.search(r'index (\d) lifetime (\d+)', m).group(2))) for t, m in rev if 'lifetime' in m]
inst = [t for t, m in br if 'LGTK installed:' in m and t >= revoke_at]
check(cut and abs(cut[0][0] - revoke_at) < 2 and cut[0][1] == int(lifetime / 30) and inst and inst[0] - revoke_at < 2,
      f'revocation at {cut[0][0]:.0f} s: LGTK lifetime cut to {cut[0][1]} s, new LGTK installed at {inst[0]:.0f} s' if cut and inst else 'revocation')
act = [t for t, m in br if 'LGTK activated' in m and t > revoke_at]
check(act and abs(act[0] - (revoke_at + lifetime / 30)) < 5,
      f'new LGTK active at {act[0]:.0f} s ({act[0] - revoke_at:.0f} s after the revocation)' if act else 'new LGTK active')
got1 = [t for t, m in lines('node1.log') if re.search(r'LGTK.*(inserted|installed|received)|Keys inserted|NW key set', m) and t > revoke_at]
check(act and got1 and got1[0] < act[0], f'LFN 1 had the new LGTK at {got1[0]:.0f} s' if got1 else 'LFN 1 got the new LGTK')
tls2 = [t for t, m in br if t > revoke_at and 'EAP-TLS' in m and '00:02' in m]
tls2 += [t for t, m in lines('node2.log') if t > revoke_at and re.search(r'EAP-TLS|TLS: start|tls.*start', m, re.I)]
check(tls2, f'LFN 2 (revoked) authenticated again with EAP-TLS at {min(tls2):.0f} s' if tls2 else 'LFN 2 needed EAP-TLS again')
oper = next((t for t, m in lines('node1.log') if 'LFN operational' in m), None)
tx_br = [(t, p) for t, m in br for p in re.findall(r'test-udp tx to fd12:3456::302:304:506:1: (Q \d+)', m)]
rx1 = {p for t, m in lines('node1.log') for p in re.findall(r'test-udp rx from \S+: (Q \d+)', m)}
q = [p for t, p in tx_br if oper and oper + 15 < t < dur - 15]
check(q and all(p in rx1 for p in q), f'BR -> LFN 1: {len(q)} requests, {sum(p in rx1 for p in q)} received')
tx1 = [(t, p) for t, m in lines('node1.log') for p in re.findall(r'test-udp tx to \S+: (Q \d+)', m)]
rxbr = {p for t, m in br for p in re.findall(r'test-udp rx from fd12:3456::302:304:506:1: (Q \d+)', m)}
q = [p for t, p in tx1 if oper and oper + 15 < t < dur - 15]
check(q and all(p in rxbr for p in q), f'LFN 1 -> BR: {len(q)} requests, {sum(p in rxbr for p in q)} received')
print('test_lfn_revoke: ' + ('PASSED' if not fails else f'FAILED ({len(fails)})'))
sys.exit(1 if fails else 0)
EOF
