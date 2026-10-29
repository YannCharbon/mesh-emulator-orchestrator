#!/bin/bash
# FAN 1.1 LFN under the border router, end to end, in virtual time (GS8
# stage 3; the stack's docs/LFN_STACK.md). Node 0 is wsbrd-inproc with LFN
# parenting (examples/wsbrd-lfn.conf), node 1 wsnode-inproc as an LFN
# (examples/wsnode-lfn.conf). Test traffic (app_wsbrd/test_traffic.c): the
# border router sends a UDP request to the LFN's address and one to ff02::1
# every BR_PERIOD s, the LFN one to the border router every LFN_PERIOD s;
# each side echoes what it receives.
#
# Checks, from the logs and the power ledger:
#   - the LFN joined (LPAS -> LPA, EAPOL, LPCS -> LPC, DHCPv6, ARO) and the
#     join took less than JOIN_MAX s;
#   - unicast both ways: the requests and their echoes arrived;
#   - multicast: the ff02::1 requests reached the LFN, each as a MAC-unicast
#     copy from the border router;
#   - every frame the LFN received started inside one of its listen windows;
#   - the LFN kept its parent (no parent loss, no second discovery);
#   - LGTK (docs/LFN_STACK.md §8): the LFN joined with an LGTK (4-way
#     handshake with its NR KDE, LPCS under key index 5-7); for every LGTK
#     the border router installs after the join (LGTK_ROTATE_AT, or short
#     LGTK lifetimes in BR_EXTRA), an LPC brought the new hash, the LFN made
#     its key request lgtk_max_mismatch_s after that LPC, got the LGTK
#     (group-key handshake) before the border router activated it, and took
#     the new active index from the next LPC; the traffic checks above then
#     cover the rotation. The timings are printed (Silicon Labs' LFN: key
#     request 113-117 s after the first LPC with the new hash, black-box item
#     49).
# It prints the LFN's average current from STEADY_FROM s to the end, with
# the share of each part (EM2 ledger, the profile given).
#
# usage: test_lfn_join.sh <bindir> <workdir(holds examples/)> <outdir> [profile]
# Environment: DUR (900), WSSIM_SEED (1), RSSI (-50), TUN (tunvt0),
# BR_PERIOD (37), LFN_PERIOD (53; both 0: no test traffic, idle LFN), STEADY_FROM (300), JOIN_MAX (120),
# TX_POWER (10 dBm, both nodes), LGTK_ROTATE_AT (s, 0: none; the border
# router's lgtk_rotate_at_s, ws_management_lfn_lgtk_rotate()),
# LFN_EXTRA, BR_EXTRA (more "-o key=value" options), SILENT_AT and
# SILENT_FOR (s: the parent stops answering the LFN then, for that long; the
# test then checks the parent-loss rule and the rejoin instead).
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
PROFILE=${4:-$WORK/examples/power/efr32fg25-soc.ini}
DUR=${DUR:-900}; RSSI=${RSSI:--50}; TUN=${TUN:-tunvt0}
BR_PERIOD=${BR_PERIOD:-37}; LFN_PERIOD=${LFN_PERIOD:-53}
STEADY_FROM=${STEADY_FROM:-300}; JOIN_MAX=${JOIN_MAX:-120}; TX_POWER=${TX_POWER:-10}
LGTK_ROTATE_AT=${LGTK_ROTATE_AT:-0}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_join: $OUT is not empty" >&2; exit 1; }
echo "0,1:$RSSI" > "$OUT.topo"
# The LFN's address comes from DHCPv6 (the prefix of the examples and its
# EUI-64); the border router's is the one its TUN gives it
LFN_ADDR=fd12:3456::302:304:506:1
BR_ADDR=fd12:3456::1615:16ff:fe17:1819
TRAFFIC_BR="-o test_udp_echo=true -o test_udp_dst=$LFN_ADDR -o test_udp_dst=ff02::1 -o test_udp_period_s=$BR_PERIOD -o test_udp_start_s=60"
TRAFFIC_LFN="-o test_udp_echo=true -o test_udp_dst=$BR_ADDR -o test_udp_period_s=$LFN_PERIOD -o test_udp_start_s=70"
(( BR_PERIOD > 0 )) || TRAFFIC_BR=
(( LFN_PERIOD > 0 )) || TRAFFIC_LFN=
SILENT=
[ -n "${SILENT_AT:-}" ] && SILENT="-o test_lfn_silent_at_s=$SILENT_AT -o test_lfn_silent_for_s=${SILENT_FOR:-500}"
BR_OPTS="-o tx_power=$TX_POWER $TRAFFIC_BR $SILENT $( (( LGTK_ROTATE_AT > 0 )) && echo "-o lgtk_rotate_at_s=$LGTK_ROTATE_AT") ${BR_EXTRA:-}" \
NODE_OPTS="-o tx_power=$TX_POWER $TRAFFIC_LFN ${LFN_EXTRA:-}" \
CONF=lfn TOPO="$OUT.topo" TUN=$TUN POWER=$PROFILE POWER_LOG=1 \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > "$OUT.run" 2>&1
RC=$?
rm -f "$OUT.topo"
[ $RC = 0 ] || { echo "test_lfn_join: run failed ($OUT.run)"; exit 1; }

python3 - "$OUT" "$DUR" "$STEADY_FROM" "$JOIN_MAX" "$LGTK_ROTATE_AT" "$BR_PERIOD" "${SILENT_AT:-0}" <<'EOF'
import json, re, sys, bisect
out, dur, steady_from, join_max, rot = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
traffic = int(sys.argv[6]) > 0
silent_at = float(sys.argv[7])
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

# --- LFN
lfn = {'disc': [], 'lost': [], 'oper': None, 'oper_all': [], 'auth': [], 'win': [], 'rx': [], 'udp_rx': [], 'udp_tx': [], 'window_us': 5000,
       'lpc': [], 'keyreq': [], 'mismatch': [], 'lgtk_recv': [], 'active': [], 'lpcs_key': []}
for t, m in lines('node1.log'):
    if 'LFN discovery start' in m:
        lfn['disc'].append(t)
    elif 'LFN operational' in m:
        lfn['oper_all'].append(t)
        if lfn['oper'] is None:
            lfn['oper'] = t
    elif 'LFN authentication start' in m:
        lfn['auth'].append(t)
    elif re.search(r'parent .* lost', m):
        lfn['lost'].append(t)
    elif 'LFN window open' in m:
        lfn['win'].append(t)
    elif 'LFN listen schedule:' in m:
        lfn['window_us'] = int(re.search(r'window (\d+) us', m).group(1))
    elif m.startswith('Transmitting ...'):
        lfn.setdefault('tx', []).append(t)
    elif m.startswith('Receiving ...'):
        if int(re.search(r'pkt_len=(\d+)', m).group(1)) >= 30:
            lfn['rx'].append(t)
    elif 'test-udp rx from' in m:
        src, p = re.search(r'from (\S+): (.*)', m).groups()
        lfn['udp_rx'].append((t, src, p))
    elif 'test-udp tx to' in m:
        dst, p = re.search(r'to (\S+): (\S+ \d+)', m).groups()
        lfn['udp_tx'].append((t, dst, p))
    elif 'LFN: LPC' in m or 'LFN version' in m:
        lfn['lpc'].append(t)
    elif 'LFN: parent heard' in m:
        lfn.setdefault('heard', []).append(t)
    elif 'LFN: LGTK key request' in m:
        lfn['keyreq'].append(t)
    elif 'LGTK hash mismatch' in m:
        lfn['mismatch'].append((t, int(re.search(r'key request in (\d+) s', m).group(1))))
    elif 'LGTK recv index' in m:
        lfn['lgtk_recv'].append((t, int(re.search(r'index (-?\d+)', m).group(1))))
    elif 'LFN: active LGTK index' in m:
        lfn['active'].append((t, int(re.search(r'index (\d+)', m).group(1))))
    if 'LFN: LPCS to' in m:
        lfn['lpcs_key'].append(int(re.search(r'\(key (\d+)', m).group(1)))
br = {'udp_rx': [], 'udp_tx': [], 'copies': 0, 'lgtk': [], 'installed': [], 'activated': [], 'expired': [], 'nr4wh': 0}
for t, m in lines('br.log'):
    if 'test-udp rx from' in m:
        src, p = re.search(r'from (\S+): (.*)', m).groups()
        br['udp_rx'].append((t, src, p))
    elif 'test-udp tx to' in m:
        dst, p = re.search(r'to (\S+): (\S+ \d+)', m).groups()
        br['udp_tx'].append((t, dst, p))
    elif 'multicast copy to LFN' in m:
        br['copies'] += 1
    elif 'LGTK rotation:' in m:
        br['lgtk'].append(t)
    elif '[wspa]: LGTK installed:' in m:
        br['installed'].append((t, int(re.search(r'index (\d+)', m).group(1))))
    elif '[wspa]: LGTK activated:' in m:
        br['activated'].append((t, int(re.search(r'index (\d+)', m).group(1))))
    elif '[wspa]: LGTK expired:' in m:
        br['expired'].append(t)
    elif '4WH: Message 2 NR 2' in m:
        br['nr4wh'] += 1

check(lfn['oper'] is not None and lfn['oper'] <= join_max,
      f"LFN operational at {lfn['oper']:.1f} s" if lfn['oper'] else 'LFN operational')
if silent_at <= 0:
    check(len(lfn['disc']) == 1 and not lfn['lost'], f"LFN kept its parent ({len(lfn['disc'])} discovery, {len(lfn['lost'])} parent losses)")
else:
    # The parent went silent: lost 1.5 x maintain_parent_time (5) x sync
    # period (50 s) = 375 s after the last LPC, then a rejoin without EAPOL
    loss = lfn['lost'][0] if lfn['lost'] else None
    heard = [t for t in lfn.get('heard', []) if loss and t < loss]
    check(loss is not None and heard and abs(loss - heard[-1] - 375) < 1.5,
          f"parent lost at {loss:.1f} s, {loss - heard[-1]:.1f} s after the last frame from it (rule: 375 s)" if loss and heard else 'parent lost')
    again = [t for t in lfn['oper_all'] if loss and t > loss]
    check(again and not [t for t in lfn['auth'] if loss and t > loss],
          f"rejoined without EAPOL, operational again at {again[0]:.1f} s" if again else 'rejoined')

last = dur - 15      # requests later than this may still be on their way
def pairs(tx, rx_peer, rx_back, mc=False):
    # requests "Q n" sent by one side, received by the other, echoed back
    # requests sent once the LFN was operational (before, it has no address)
    q = [(t, p) for t, d, p in tx if p.startswith('Q') and (lfn['oper'] or dur) < t < last and (d.startswith('ff02') == mc)]
    got = {p for t, s, p in rx_peer}
    back = {p.replace('R', 'Q', 1) for t, s, p in rx_back if p.startswith('R')}
    return q, [x for x in q if x[1] in got], [x for x in q if x[1] in back]
if traffic:
  q, got, back = pairs(br['udp_tx'], lfn['udp_rx'], br['udp_rx'])
  check(q and len(got) == len(q) and len(back) == len(q), f'BR -> LFN unicast: {len(q)} requests, {len(got)} received, {len(back)} echoes back')
  q, got, back = pairs(lfn['udp_tx'], br['udp_rx'], lfn['udp_rx'])
  check(q and len(got) == len(q) and len(back) == len(q), f'LFN -> BR unicast: {len(q)} requests, {len(got)} received, {len(back)} echoes back')
  q, got, back = pairs(br['udp_tx'], lfn['udp_rx'], br['udp_rx'], mc=True)
  check(q and len(got) == len(q) and br['copies'] >= len(q), f'BR -> ff02::1: {len(q)} requests, {len(got)} received by the LFN, {br["copies"]} MAC-unicast copies')
W = lfn['window_us'] / 1e6
win = sorted(lfn['win'])
def in_window(t):
    i = bisect.bisect_right(win, t + 1e-6) - 1
    return i >= 0 and win[i] - 1e-6 <= t < win[i] + W
# An ACK to the LFN's own frame comes right after it (within 60 ms of its start)
txs = sorted(lfn.get('tx', []))
def after_own_tx(t):
    i = bisect.bisect_right(txs, t) - 1
    return i >= 0 and t - txs[i] < 0.06
acks = [t for t in lfn['rx'] if not in_window(t) and after_own_tx(t)]
outside = [t for t in lfn['rx'] if not in_window(t) and not after_own_tx(t)]
check(lfn['rx'] and not outside, f"{len(lfn['rx'])} frames received by the LFN: {len(outside)} outside its windows, "
      f"{len(acks)} ACKs to its own frames" + (f" (first outside at {outside[0]:.6f} s)" if outside else ''))
# --- LGTK
check(lfn['lgtk_recv'] and lfn['lpcs_key'] and min(lfn['lpcs_key']) >= 5 and br['nr4wh'] > 0,
      f"LFN joined with an LGTK (index {lfn['lgtk_recv'][0][1] if lfn['lgtk_recv'] else '?'}, NR KDE in 4WH message 2, LPCS under key index {lfn['lpcs_key'][0] if lfn['lpcs_key'] else '?'})")
if rot > 0:
    check(br['lgtk'] and br['installed'] and any(t >= br['lgtk'][0] for t, i in br['installed']), f"LGTK rotation at {br['lgtk'][0]:.1f} s installed a new LGTK" if br['lgtk'] else 'LGTK rotation')
oper = lfn['oper'] or dur
for ti, idx in [x for x in br['installed'] if x[0] > oper]:
    # The LPC that brought the new hash, the key request, the key, its activation
    mm = [x for x in lfn['mismatch'] if x[0] > ti]
    req = [t for t in lfn['keyreq'] if t > ti]
    got = [t for t, i in lfn['lgtk_recv'] if t > ti and i == idx]
    act = [t for t, i in br['activated'] if t > ti and i == idx]
    sw = [t for t, i in lfn['active'] if act and t > act[0] and i == idx]
    if act and act[0] > dur - 60:
        act = []      # activated too late in the run to see the LFN switch
    if not mm or not req or not got:
        check(False, f"LGTK {idx} installed at {ti:.1f} s: LPC with the new hash {'yes' if mm else 'no'}, key request {'yes' if req else 'no'}, key {'yes' if got else 'no'}")
        continue
    lpc_t, delay = mm[0]
    check(abs(req[0] - lpc_t - delay) < 1.5 and got[0] < (act[0] if act else dur + 1),
          f"LGTK {idx} installed at {ti:.1f} s: hash at the LFN {lpc_t - ti:.1f} s later (LPC), key request {req[0] - lpc_t:.1f} s after that "
          f"(setting {delay} s; Silicon Labs 113-117 s), LGTK received {got[0] - req[0]:.1f} s after the request"
          + (f", activated by the border router {act[0] - ti:.1f} s after its installation" if act else ''))
    if act:
        check(sw and sw[0] - act[0] < 60, f"LGTK {idx} activated at {act[0]:.1f} s: the LFN took it as active {sw[0] - act[0]:.1f} s later" if sw else f"LGTK {idx} activated at {act[0]:.1f} s: LFN switch")
if br['expired']:
    print(f"info LGTKs expired at the border router: {', '.join(f'{t:.1f}' for t in br['expired'])} s")

# --- Power from steady_from to the end, node 1
rep = json.load(open(f'{out}/power.json'))
n1 = next(n for n in rep['nodes'] if n['node'] == 1)
ma = {('mcu', k): v['ma'] for k, v in n1['mcu']['states'].items()}
ma.update({('radio', k): v['ma'] for k, v in n1['radio']['states'].items() if 'ma' in v})
tx_ma = {lv['dbm']: lv['ma'] for lv in n1['radio']['states']['tx']['levels']}
wake_uc = n1['mcu']['wake_charge']['uc_each']
t0, t1 = steady_from * 1e6, dur * 1e6
parts, wakes = {}, 0
for line in open(f'{out}/power_log.txt'):
    if line.startswith('#'):
        continue
    f = line.split()
    s, e, node, part, state = float(f[0]), float(f[1]), int(f[2]), f[3], f[4]
    if node != 1 or e <= t0 or s >= t1:
        continue
    if part == 'mcu' and state == 'active' and s >= t0:
        wakes += 1
    s, e = max(s, t0), min(e, t1)
    cur = tx_ma[int(f[5])] if state == 'tx' else ma.get((part, state), 0)
    k = f'{part} {state}'
    tm, ch = parts.get(k, (0.0, 0.0))
    parts[k] = (tm + (e - s), ch + (e - s) * cur / 1e3)
span = (t1 - t0) / 1e6
parts['mcu wake (per-wake charge)'] = (0.0, wakes * wake_uc)
total = sum(c for t, c in parts.values())
print(f'info LFN steady state {steady_from:.0f}-{dur:.0f} s: {total / span:.1f} uA average '
      f'(profile {n1["profile"]}, PHY mode {n1.get("phy_mode", "?")}, {wakes / span:.1f} wakes/s)')
for k, (tm, ch) in sorted(parts.items(), key=lambda x: -x[1][1]):
    if ch > 0:
        print(f'info   {k:28s} {ch / span:7.2f} uA  {100 * ch / total:5.1f} %  ({tm / 1e6 / span * 100:.3f} % of the time)')
print('test_lfn_join: ' + ('PASSED' if not fails else f'FAILED ({len(fails)})'))
sys.exit(1 if fails else 0)
EOF
