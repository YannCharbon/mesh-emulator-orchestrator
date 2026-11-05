#!/bin/bash
# A router's LGTK key request answered by a full EAP-TLS, in virtual time
# (FG25 kits, 2026-11-03: a router cold-started with its keys in flash got its
# LGTK 521 s after its reset). Border router 0, FFN router 1 (LFN parent), LFN
# 2 under the router. At 590 s the border router forgets the router's PMK and
# PTK (test_keys_remove_at_s, as a border router restarted without its
# supplicants' keys) and installs a new LGTK (lgtk_rotate_at_s); at 600 s the
# router restarts with its key storage (run.sh RESTART). The router sees the
# LGTK hash mismatch during its join and sends a key request; the border
# router answers with a full EAP-TLS. Around it:
#   CASE=race:   the border router's first EAP-TLS frame to the router is
#                dropped with its MAC retries for DROP_FOR (16) s (DROP), so
#                that its EAP-TLS comes again after the router's bootstrap has
#                ended, which removes the
#                supplicant's EAPOL target: before the fix (stack 3adcae6)
#                the supplicant refused it ("Incoming KMP rejected, auth not
#                ongoing") and the LGTK came with a request minutes later;
#                the fix's branch logs "Incoming KMP for our key request";
#   CASE=txfail: the request is lost at the MAC (the link is dead, LINK
#                -140 dBm, 654.6-665.3 s): before the fix the next one
#                waited for the retry timer (180-420 s).
# The race's drop is found by a probe run first (the same run without the
# drop, to 720 s, FRAMES=1; deterministic for the seed): the frame from the
# border router to the router that ends last before the router logs
# "EAP-TLS init" gives the MAC sequence number and the time, whatever the
# stack's timing (seed 3 with stack 52024b4: sequence number 216 at
# 654.65 s). The txfail windows are set for seed 3 (the router, restarted at
# 600 s with its clock from then, sends its request at 654.4 s in that
# seed). The router's restart
# started its clock at 0 before protocol version 4 (a node frozen during its
# boot): the request then went out at 653.1 s, and the race came from link
# windows; with the clock from the restart, the bootstrap ends ~14 s after
# the EAP-TLS starts and no link window reproduces it.
#
# Checks: no incoming KMP refused by the router, and its LGTK installed at
# most LGTK_MAX (60) s after the link's return (race: the end of the drop window).
#
# usage: test_lfn_keyreq.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: CASE (race; race or txfail), WSSIM_SEED (3), TUN (none),
# LGTK_MAX (60), DROP_FOR (16 s, race), KEEP (1: keep the run directory).
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
CASE=${CASE:-race}; TUN=${TUN:-none}; LGTK_MAX=${LGTK_MAX:-60}
export WSSIM_SEED=${WSSIM_SEED:-3}
HERE=$(cd "$(dirname "$0")" && pwd)
DROP_FOR=${DROP_FOR:-16}
case $CASE in
race)   LINKS=""; DROPS=probe ;;
txfail) LINKS="0:1:654.6:-140 0:1:665.3:-50"; DROPS=""; BACK=665.3 ;;
*) echo "test_lfn_keyreq: CASE race or txfail" >&2; exit 2 ;;
esac
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_keyreq: $OUT is not empty" >&2; exit 1; }
printf '0,1:-50\n1,2:-50\n' > "$OUT.topo"
export NODE_OPTS_1="-o device_type=ffn -o enable_lfn=true"
export NODE_OPTS_2="-o device_type=lfn -o enable_lfn=false"
if [ "$DROPS" = probe ]; then
    # the probe: the same run without the drop, to 720 s
    BR_OPTS="-o test_keys_remove_at_s=590 -o lgtk_rotate_at_s=590 -o test_revoke_eui64=01:02:03:04:05:06:00:01" \
    CONF=lfn TOPO="$OUT.topo" TUN=$TUN SIM_OPTS="--drift-random=20" RESTART="1:600" FRAMES=1 \
        "$HERE/run.sh" "$BIN" "$WORK" "$OUT/probe" 720 > "$OUT.probe.run" 2>&1 ||
        { echo "test_lfn_keyreq: probe run failed ($OUT.probe.run)"; exit 1; }
    P=$(PYTHONPATH=$HERE python3 - "$OUT/probe" <<'PY'
import sys
from lfn_lib import lines, frames, mac
out = sys.argv[1]
eap = [t for t, m in lines(f'{out}/node1.r1.log') if 'EAP-TLS init' in m and t > 600]
if not eap:
    sys.exit(1)
dst = mac(1).replace(':', '')
fr = [(t, f['seq']) for t, src, ch, f in frames(f'{out}/frames.txt')
      if src == 0 and f['dst'] == dst and f['type'] == 1 and f['seq'] is not None and t < eap[0]]
t, seq = fr[-1]
print(seq, f'{t:.3f}')
PY
) || { echo "test_lfn_keyreq: the probe found no EAP-TLS start at the router"; exit 1; }
    rm -rf "$OUT/probe" "$OUT.probe.run"
    SEQ=${P% *}; AT=${P#* }
    FROM=$(python3 -c "print(f'{$AT - 0.05:.3f}')"); BACK=$(python3 -c "print(f'{$AT + $DROP_FOR:.3f}')")
    DROPS="0:1:$SEQ:$FROM:$BACK"
    echo "info probe: the border router's EAP-TLS start to the router, MAC sequence number $SEQ at $AT s: DROP=$DROPS"
fi
BR_OPTS="-o test_keys_remove_at_s=590 -o lgtk_rotate_at_s=590 -o test_revoke_eui64=01:02:03:04:05:06:00:01" \
CONF=lfn TOPO="$OUT.topo" TUN=$TUN SIM_OPTS="--drift-random=20" RESTART="1:600" LINK="$LINKS" DROP="$DROPS" \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" 1300 > "$OUT.run" 2>&1
RC=$?
rm -f "$OUT.topo"
[ $RC = 0 ] || { echo "test_lfn_keyreq: run failed ($OUT.run)"; exit 1; }
rm -f "$OUT.run"

PYTHONPATH=$HERE python3 - "$OUT" "$BACK" "$LGTK_MAX" "$CASE" <<'PY'
import sys
from lfn_lib import Checks, lines
out, back, lgtk_max, case = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
ck = Checks('test_lfn_keyreq')
log = list(lines(f'{out}/node1.r1.log'))
req = [t for t, m in log if 'Initial EAPOL-Key send' in m]
eap = [t for t, m in log if 'EAP-TLS init' in m]
rej = [t for t, m in log if 'Incoming KMP rejected' in m]
acc = [t for t, m in log if 'Incoming KMP for our key request' in m]
done = [t for t, m in log if 'Bootstrap Done' in m]
lgtk = [t for t, m in log if 'LGTK recv index' in m and t > 600]
ck.info(f"case {case}: key requests at {', '.join(f'{t:.1f}' for t in req[:4])} s; EAP-TLS at {', '.join(f'{t:.1f}' for t in eap[:3])} s; "
        f"bootstrap done at {', '.join(f'{t:.1f}' for t in done[:2])} s; link back at {back:.0f} s")
if case == 'race':
    ck(acc, f"the border router's EAP-TLS after the bootstrap accepted at {acc[0]:.1f} s" if acc else "EAP-TLS after the bootstrap accepted")
ck(not rej, f"no incoming KMP refused by the router" if not rej else f"incoming KMP refused at {', '.join(f'{t:.0f}' for t in rej)} s")
ck(lgtk and lgtk[0] - back <= lgtk_max,
   f"LGTK at {lgtk[0]:.1f} s, {lgtk[0] - back:.1f} s after the link's return" if lgtk else 'LGTK received')
sys.exit(ck.done())
PY
RC=$?
[ "${KEEP:-0}" = 1 ] || rm -rf "$OUT"
exit $RC
