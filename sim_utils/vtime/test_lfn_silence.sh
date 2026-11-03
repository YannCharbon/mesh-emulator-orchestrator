#!/bin/bash
# FAN 1.1 LFNs that send nothing for hours, in virtual time: downlinks after
# 1 h, 3 h and 10 h of silence. Border router 0, FFN router 1 (LFN parent),
# LFN 2 under the router (hears only it), LFN 3 under the border router (hears
# only it). DHCPv6 lease infinite (dhcp_lifetime_s, as wsbrd 2.x), so the LFNs
# never renew it; their address registration (36 h) outlasts the run. The LFNs
# send no test traffic and do not echo (test_udp_listen: the socket is open,
# nothing is answered), and make no better-parent check: after their join
# they transmit only ACKs. (test_lfn_silent.sh: the same with an echo after
# each request, every PERIOD s, and the parent's re-anchors counted.) The border router sends one UDP request to each LFN at each of
# SILENCES (test_udp_at_s): the first 1 h after the later LFN join, then 3 h,
# then 10 h later.
#
# Checks, for each LFN:
#   - joined by JOIN_MAX s and kept its parent (one discovery, no loss);
#   - each request reached the LFN; the silence before it (the time since
#     the LFN's last frame other than an ACK, from frames.txt) is printed
#     and must be at least 95 % of the nominal one.
#
# usage: test_lfn_silence.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: WSSIM_SEED (1), TUN (none), SILENCES ("3600 10800 36000": the
# gaps before each request, the first one counted from JOIN_REF, 300 s: the
# requests go at 3 900, 14 700 and 50 700 s), JOIN_MAX (600), SIM_OPTS (--drift-random=20),
# LFN_EXTRA, PARENT_EXTRA, KEEP (1: keep the run directory).
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
TUN=${TUN:-none}; JOIN_MAX=${JOIN_MAX:-600}; SIM_OPTS=${SIM_OPTS---drift-random=20}
SILENCES=${SILENCES:-3600 10800 36000}; JOIN_REF=${JOIN_REF:-300}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_silence: $OUT is not empty" >&2; exit 1; }
printf '0,1:-50\n1,2:-50\n0,3:-50\n' > "$OUT.topo"
AT=; t=$JOIN_REF
for s in $SILENCES; do t=$((t + s)); AT+=" -o test_udp_at_s=$t"; done
DUR=$((t + 300))
LFN="-o device_type=lfn -o enable_lfn=false -o test_udp_listen=true -o lfn_parent_check_s=0 -o lfn_parent_check_weak_s=0 ${LFN_EXTRA:-}"
export NODE_OPTS_1="-o device_type=ffn -o enable_lfn=true ${PARENT_EXTRA:-}"
export NODE_OPTS_2="$LFN" NODE_OPTS_3="$LFN"
BR_OPTS="-o dhcp_lifetime_s=4294967295 -o test_udp_dst=fd12:3456::302:304:506:2 -o test_udp_dst=fd12:3456::302:304:506:3 $AT ${PARENT_EXTRA:-}" \
CONF=lfn TOPO="$OUT.topo" TUN=$TUN FRAMES=1 SIM_OPTS="$SIM_OPTS" \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > "$OUT.run" 2>&1
RC=$?
rm -f "$OUT.topo"
[ $RC = 0 ] || { echo "test_lfn_silence: run failed ($OUT.run)"; exit 1; }

PYTHONPATH=$HERE python3 - "$OUT" "$JOIN_MAX" "$JOIN_REF $SILENCES" <<'PY'
import re, sys
from lfn_lib import Checks, lines, frames, udp, mac
out, join_max = sys.argv[1], float(sys.argv[2])
gaps = [int(x) for x in sys.argv[3].split()]
ck = Checks('test_lfn_silence')
br = udp(f'{out}/br.log')
sent = sorted({t for t, a, p in br['tx']})
# the LFNs' frames other than ACKs (type 2)
own = {2: [], 3: []}
for t, src, ch, f in frames(f'{out}/frames.txt'):
    if src in own and f['type'] != 2:
        own[src].append(t)
for node, parent in ((2, 1), (3, 0)):
    oper, disc, lost, search, par = None, 0, 0, 0, None
    for t, m in lines(f'{out}/node{node}.log'):
        if 'LFN operational' in m and oper is None:
            oper = t
        elif 'LFN discovery start' in m:
            disc += 1
        elif re.search(r'parent .* lost', m):
            lost += 1
        elif 'LFN: parent search' in m:
            search += 1
        r = re.search(r'LFN: parent (\S+), PAN', m)
        if r:
            par = r.group(1)
    where = 'the router' if parent else 'the border router'
    ck(oper is not None and oper <= join_max and par == mac(parent) and disc == 1 and not lost,
       f"LFN {node} under {where}: operational at {oper:.1f} s, {disc} discovery, {lost} parent losses, {search} parent searches"
       if oper else f"LFN {node} operational")
    rx = {p: t for t, a, p in udp(f'{out}/node{node}.log')['rx']}
    addr = f'fd12:3456::302:304:506:{node:x}'
    for k, (t, a, p) in enumerate(x for x in br['tx'] if x[1] == addr):
        before = [x for x in own[node] if x < t]
        silence = t - before[-1] if before else t
        nominal = min(gaps[k + 1] if k + 1 < len(gaps) else 0, t - (oper or 0))
        got = rx.get(p)
        ck(got is not None and silence >= 0.95 * nominal,
           f"LFN {node}: request {p} at {t / 3600:.2f} h, after {silence / 3600:.2f} h of silence: "
           + (f"received {got - t:.1f} s later" if got is not None else "NOT received"))
sys.exit(ck.done())
PY
RC=$?
[ "${KEEP:-0}" = 1 ] || rm -rf "$OUT" "$OUT.run"
exit $RC
