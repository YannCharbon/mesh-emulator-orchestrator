#!/bin/bash
# Radio off, end to end (GS8 stage 1; docs/POWER_MODEL.md §3.1, §3.3): in a
# dag_simple run, router 1 switches its MAC to RX-off-when-idle at OFF_AT
# seconds (rx_off_when_idle_after). Checks, from its log and the power ledger:
#   - after the switch its radio is on only for its own transmissions: every
#     interval with the radio on (not "off" in the state log) holds a TX of
#     node 1 (its CSMA, the TX, the wait for an ACK), and every frame it
#     receives then falls in such an interval (an ACK, or a frame heard
#     during its own CSMA backoff); frames sent to it are lost while it is
#     off: in legacy mode, the radio's "rf rx lost: radio off" lines; with the
#     central medium (the default), the medium log's not_listening outcomes
#     at node 1 (the server delivers nothing to a radio that is off);
#   - before the switch it received frames as usual;
#   - its radio is off for most of the time after the switch, and its MCU
#     sleeps deep then;
#   - a neighbour that keeps RX on (node 2) never has its radio off;
#   - state times add up to the run time for every node.
#
# usage: test_radio_off.sh <bindir> <workdir(holds examples/)> <outdir> [profile]
#   bindir   a virtual-time build (wssimserver, wsbrd-inproc, wsnode-inproc)
#   profile  power profile (default <workdir>/examples/power/efr32fg25-soc.ini)
# Environment: TUN (default tun0), WSSIM_SEED (default 1), DUR (300), OFF_AT (150).
# Exit status 0 if every check passes.
set -u
BIN=$1; WORK=$(realpath "$2"); OUT=$3
PROFILE=${4:-$WORK/examples/power/efr32fg25-soc.ini}
DUR=${DUR:-300}; OFF_AT=${OFF_AT:-150}
HERE=$(cd "$(dirname "$0")" && pwd)

# The medium: central unless the server refuses the medium's options (legacy,
# by SIM_OPTS or a wrapper of wssimserver: medium_lfn_suite.sh)
MLOG=
if ! "$BIN/wssimserver" ${SIM_OPTS:-} --vtime=1 --medium-log=/dev/null 2>&1 | grep -q "need the central medium"; then
    mkdir -p "$OUT" && MLOG=$(realpath "$OUT")/medium.log
fi
NODE_OPTS_1="-o rx_off_when_idle_after=$OFF_AT" POWER=$PROFILE POWER_LOG=1 FRAMES=1 \
    SIM_OPTS="${SIM_OPTS:-}${MLOG:+ --medium-log=$MLOG}" \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > /dev/null || { echo "test_radio_off: run failed ($OUT)"; exit 1; }

python3 - "$OUT" "$OFF_AT" "$DUR" "$MLOG" <<'EOF'
import bisect, json, sys
out, off_at, dur, mlog = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
t0 = float(open(f'{out}/t0').read())
fails = []

def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        fails.append(what)

rx_before = lost = 0
rx_after = []
switched = None
central = False
for line in open(f'{out}/node1.log', errors='replace'):
    t, _, msg = line.partition(' ')
    try:
        t = float(t) - t0
    except ValueError:
        continue
    if 'RX off when idle (rx_off_when_idle_after' in msg:
        switched = t
    if 'Receiving ...' in msg:
        if switched is None:
            rx_before += 1
        else:
            rx_after.append(t)
    if 'rf rx lost: radio off' in msg:
        lost += 1
    if 'rf medium: central' in msg:
        central = True
# With the central medium (docs/MEDIUM.md) the server delivers nothing to a
# radio that is off, so the radio has nothing to drop: the medium log has
# those frames as not_listening at node 1
check(central == bool(mlog), 'central medium' if central else 'legacy medium')
if central and switched is not None:
    for l in open(mlog):
        t_end, src, dst, fid, outcome = l.split()[:5]
        if dst == '1' and outcome == 'not_listening' and int(t_end) / 1e6 > switched:
            lost += 1
check(switched is not None and abs(switched - off_at) < 1, f'node 1 switched to RX off when idle at {switched:.2f} s' if switched is not None else 'node 1 switched to RX off when idle')
check(rx_before > 0, f'node 1 received {rx_before} frames before the switch')
check(lost > 0, f'{lost} frames to node 1 lost while its radio was off' +
      (' (not_listening in the medium log)' if central else ' (its "rf rx lost: radio off" lines)'))

# Time per radio and MCU state after the switch, from the state log; the
# intervals with node 1's radio on, and whether each holds a TX of its own
radio, mcu = {}, {}
on = []         # [start, end, has_tx] in us, merged
for line in open(f'{out}/power_log.txt'):
    if line.startswith('#'):
        continue
    f = line.split()
    start, end, node, part, state = float(f[0]), float(f[1]), int(f[2]), f[3], f[4]
    if node != 1 or end <= off_at * 1e6:
        continue
    if part == 'radio' and state != 'off' and end > switched * 1e6:
        if on and start <= on[-1][1]:
            on[-1][1] = max(on[-1][1], end)
            on[-1][2] |= state == 'tx'
        else:
            on.append([start, end, state == 'tx'])
    d = (end - max(start, off_at * 1e6)) / 1e6
    (radio if part == 'radio' else mcu)[state] = (radio if part == 'radio' else mcu).get(state, 0) + d
span = dur - off_at
check(radio.get('off', 0) > 0.9 * span, f'node 1 radio off {radio.get("off", 0):.1f} s of the last {span:.0f} s')
check(mcu.get('deep_sleep', 0) > 0.8 * span, f'node 1 MCU in deep sleep {mcu.get("deep_sleep", 0):.1f} s of the last {span:.0f} s')
# The first interval is the radio still on from before the switch, until
# the MAC turns it off; the last one may still be open at the end of the run
no_tx = [(a / 1e6, b / 1e6) for a, b, tx in on if not tx and a >= switched * 1e6 and b < dur * 1e6]
check(on and not no_tx, f'after the switch, node 1 radio on {len(on)} times, each for a TX of its own '
      f'(without one: {no_tx[:3]})')
starts = [a for a, b, tx in on]
outside = [t for t in rx_after if not (i := bisect.bisect_right(starts, t * 1e6) - 1) >= 0
           or t * 1e6 > on[i][1] + 1]
check(not outside, f'node 1 received {len(rx_after)} frames after the switch, all while its radio was on '
      f'for its own TX (outside: {outside[:3]})')

report = json.load(open(f'{out}/power.json'))
n2 = next(n for n in report['nodes'] if n['node'] == 2)
check(n2['radio']['states']['off']['time_us'] == 0, 'node 2 (RX on) never off')
sums = all(abs(n['check']['mcu_time_us'] - n['check']['run_time_us']) < 1 and
           abs(n['check']['radio_time_us'] - n['check']['run_time_us']) < 1 for n in report['nodes'])
check(sums, 'state times add up to the run time for every node')
print('test_radio_off: ' + ('PASSED' if not fails else f'FAILED ({len(fails)})'))
sys.exit(1 if fails else 0)
EOF
