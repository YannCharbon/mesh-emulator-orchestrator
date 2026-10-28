#!/bin/bash
# Radio off, end to end (GS8 stage 1; docs/POWER_MODEL.md §3.1, §3.3): in a
# dag_simple run, router 1 switches its MAC to RX-off-when-idle at OFF_AT
# seconds (rx_off_when_idle_after). Checks, from its log and the power ledger:
#   - it receives no frame after the switch, and frames are lost while off;
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

NODE_OPTS_1="-o rx_off_when_idle_after=$OFF_AT" POWER=$PROFILE POWER_LOG=1 \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > /dev/null || { echo "test_radio_off: run failed ($OUT)"; exit 1; }

python3 - "$OUT" "$OFF_AT" "$DUR" <<'EOF'
import json, sys
out, off_at, dur = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
t0 = float(open(f'{out}/t0').read())
fails = []

def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        fails.append(what)

rx_before = rx_after = lost = 0
switched = None
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
            rx_after += 1
    if 'rf rx lost: radio off' in msg:
        lost += 1
check(switched is not None and abs(switched - off_at) < 1, f'node 1 switched to RX off when idle at {switched:.2f} s' if switched is not None else 'node 1 switched to RX off when idle')
check(rx_before > 0, f'node 1 received {rx_before} frames before the switch')
check(rx_after == 0, f'node 1 received {rx_after} frames after the switch')
check(lost > 0, f'{lost} frames lost while node 1 was off')

# Time per radio and MCU state after the switch, from the state log
radio, mcu = {}, {}
for line in open(f'{out}/power_log.txt'):
    if line.startswith('#'):
        continue
    f = line.split()
    start, end, node, part, state = float(f[0]), float(f[1]), int(f[2]), f[3], f[4]
    if node != 1 or end <= off_at * 1e6:
        continue
    d = (end - max(start, off_at * 1e6)) / 1e6
    (radio if part == 'radio' else mcu)[state] = (radio if part == 'radio' else mcu).get(state, 0) + d
span = dur - off_at
check(radio.get('off', 0) > 0.9 * span, f'node 1 radio off {radio.get("off", 0):.1f} s of the last {span:.0f} s')
check(mcu.get('deep_sleep', 0) > 0.8 * span, f'node 1 MCU in deep sleep {mcu.get("deep_sleep", 0):.1f} s of the last {span:.0f} s')

report = json.load(open(f'{out}/power.json'))
n2 = next(n for n in report['nodes'] if n['node'] == 2)
check(n2['radio']['states']['off']['time_us'] == 0, 'node 2 (RX on) never off')
sums = all(abs(n['check']['mcu_time_us'] - n['check']['run_time_us']) < 1 and
           abs(n['check']['radio_time_us'] - n['check']['run_time_us']) < 1 for n in report['nodes'])
check(sums, 'state times add up to the run time for every node')
print('test_radio_off: ' + ('PASSED' if not fails else f'FAILED ({len(fails)})'))
sys.exit(1 if fails else 0)
EOF
