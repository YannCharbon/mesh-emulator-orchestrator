#!/bin/bash
# FAN 1.1 LFNs with Silicon Labs' LFN profiles (Test, Balanced, Eco), over
# long virtual-time runs (the stack's docs/LFN_STACK.md, "Profiles"). Node 0
# is the border router, node 1 an FFN router, node 2 an LFN that hears only
# the router, node 3 an LFN that hears only the border router. Both parents
# and both LFNs take the profile's values. Sparse test traffic: the border
# router sends a UDP request to each LFN (and to ff02::1) every BR_PERIOD s,
# each LFN one to the border router every LFN_PERIOD s; each side echoes.
#
# Checks, from the logs and the power ledger, for each LFN:
#   - joined by JOIN_MAX s, with its expected parent, and kept it (one
#     discovery, no parent loss, no parent search);
#   - address registration: every ARO, the interval between them and the
#     margin before the lifetime would expire; no registration may lapse
#     before the run ends;
#   - unicast both ways for the whole run: requests and echoes;
#   - every frame it received started inside one of its windows (as in
#     test_lfn_join.sh);
#   - sync: sync frames received against the sync slots of the run, the
#     largest phase error once the rate is measured, the rate measured;
#   - LGTK: every LGTK installed after the join reached the LFN before the
#     border router activated it, and the LFN took the new active index;
#   - average current from STEADY_FROM s to the end (EM2 ledger).
#
# usage: test_lfn_profiles.sh <bindir> <workdir(holds examples/)> <outdir> <test|balanced|eco> [power profile]
# Environment: DUR (172800), WSSIM_SEED (1), TUN (none: no TUN device),
# RSSI (-50), BR_PERIOD (1800), LFN_PERIOD (2700), STEADY_FROM (10800),
# JOIN_MAX (3600), DHCP_LIFETIME (the border router's DHCPv6 address lifetime,
# s: default 4294967295, infinite, as wsbrd 2.x; 0: the network size's, 2 h
# for a small network, which the LFNs renew every 30 min), SIM_OPTS (default --drift-random=20), LFN_EXTRA, PARENT_EXTRA
# (more "-o key=value" options for the LFNs, for both parents), BR_EXTRA,
# LFN_POWER (a power profile for the two LFNs only, e.g.
# examples/power/efr32fg25-lfn.ini; the parents keep [power profile]),
# KEEP_LOGS (1: keep the full logs; by default only the lines the checks
# read are kept, the rest is deleted after the checks).
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3; PROF=$4
POWER_PROFILE=${5:-$WORK/examples/power/efr32fg25-soc.ini}
DUR=${DUR:-172800}; TUN=${TUN:-none}; RSSI=${RSSI:--50}
BR_PERIOD=${BR_PERIOD:-1800}; LFN_PERIOD=${LFN_PERIOD:-2700}
STEADY_FROM=${STEADY_FROM:-10800}; JOIN_MAX=${JOIN_MAX:-3600}
SIM_OPTS=${SIM_OPTS---drift-random=20}; DHCP_LIFETIME=${DHCP_LIFETIME:-4294967295}
HERE=$(cd "$(dirname "$0")" && pwd)

. "$HERE/lfn_profile.sh"

mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_profiles: $OUT is not empty" >&2; exit 1; }
printf '0,1:%s\n1,2:%s\n0,3:%s\n' "$RSSI" "$RSSI" "$RSSI" > "$OUT.topo"
lfn_addr() { printf 'fd12:3456::302:304:506:%x' "$1"; }
BR_ADDR=fd12:3456::1615:16ff:fe17:1819
TRAFFIC_BR="-o test_udp_echo=true -o test_udp_dst=$(lfn_addr 2) -o test_udp_dst=$(lfn_addr 3) -o test_udp_dst=ff02::1 -o test_udp_period_s=$BR_PERIOD -o test_udp_start_s=700"
lfn_opts() { echo "$LFN_PROF -o test_udp_echo=true -o test_udp_dst=$BR_ADDR -o test_udp_period_s=$LFN_PERIOD -o test_udp_start_s=$((900 + 60 * $1)) ${LFN_EXTRA:-}"; }
export NODE_OPTS_1="-o device_type=ffn -o enable_lfn=true $PARENT_PROF ${PARENT_EXTRA:-}"
export NODE_OPTS_2="-o device_type=lfn -o enable_lfn=false $(lfn_opts 2)"
export NODE_OPTS_3="-o device_type=lfn -o enable_lfn=false $(lfn_opts 3)"

# The power log of the LFNs only, from STEADY_FROM (the parents' would be GBs)
FIFO=$OUT.plog.fifo
rm -f "$FIFO"; mkfifo "$FIFO"
awk -v t0="$STEADY_FROM" '$1 ~ /^#/ || (($3 == 2 || $3 == 3) && $2 > t0 * 1e6)' < "$FIFO" > "$OUT.power_lfn.txt" &
AWK=$!
BR_OPTS="$TRAFFIC_BR -o dhcp_lifetime_s=$DHCP_LIFETIME $PARENT_PROF ${PARENT_EXTRA:-} ${BR_EXTRA:-}" \
CONF=lfn TOPO="$OUT.topo" TUN=$TUN POWER=$POWER_PROFILE POWER_OPTS="--power-log=$FIFO ${LFN_POWER:+--power-node=2,3:$(realpath "$LFN_POWER")}" SIM_OPTS="$SIM_OPTS" \
    "$HERE/run.sh" "$BIN" "$WORK" "$OUT" "$DUR" > "$OUT.run" 2>&1
RC=$?
wait $AWK
rm -f "$OUT.topo" "$FIFO"
mv "$OUT.power_lfn.txt" "$OUT/power_lfn.txt"
[ $RC = 0 ] || { echo "test_lfn_profiles: run failed ($OUT.run)"; exit 1; }
rm -f "$OUT.run"

python3 - "$OUT" "$DUR" "$STEADY_FROM" "$JOIN_MAX" "$PROF" "$REG" "$BCI" "$BCSP" <<'EOF'
import json, re, sys, bisect
out, dur, steady_from, join_max = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
prof, reg_min, bci, bcsp = sys.argv[5], int(sys.argv[6]), int(sys.argv[7]), int(sys.argv[8])
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
mac = lambda i: f'01:02:03:04:05:06:00:{i:02x}'
lfn_addr = lambda i: f'fd12:3456::302:304:506:{i:x}'
print(f'info profile {prof}: registration {reg_min} min, LFN broadcast interval {bci / 1000:g} s, sync period {bcsp} '
      f'({bci * bcsp / 1000:g} s), run {dur / 3600:g} h')

br = {'udp_rx': [], 'udp_tx': [], 'installed': [], 'activated': [], 'expired': [], 'copies': []}
for t, m in lines('br.log'):
    if 'test-udp rx from' in m:
        src, p = re.search(r'from (\S+): (.*)', m).groups()
        br['udp_rx'].append((t, src, p))
    elif 'test-udp tx to' in m:
        dst, p = re.search(r'to (\S+): (\S+ \d+)', m).groups()
        br['udp_tx'].append((t, dst, p))
    elif '[wspa]: LGTK installed:' in m:
        br['installed'].append((t, int(re.search(r'index (\d+)', m).group(1))))
    elif '[wspa]: LGTK activated:' in m:
        br['activated'].append((t, int(re.search(r'index (\d+)', m).group(1))))
    elif '[wspa]: LGTK expired:' in m:
        br['expired'].append(t)
    elif 'multicast copy to LFN' in m:
        br['copies'].append(t)

for node, parent in ((2, 1), (3, 0)):
    print(f'--- LFN {node} (parent: {"the router" if parent else "the border router"})')
    L = {'disc': [], 'lost': [], 'search': [], 'oper': [], 'aro': [], 'win': [], 'rx': [], 'rxe': [], 'tx': [],
         'udp_rx': [], 'udp_tx': [], 'sync': [], 'keyreq': [], 'mismatch': [], 'lgtk_recv': [], 'active': [], 'parent': None,
         'interval': None}
    for t, m in lines(f'node{node}.log'):
        if 'LFN discovery start' in m:
            L['disc'].append(t)
        elif 'LFN operational' in m:
            L['oper'].append(t)
        elif re.search(r'parent .* lost', m):
            L['lost'].append(t)
        elif 'LFN: parent search' in m:
            L['search'].append(t)
        elif 'LFN: ARO ' in m:
            L['aro'].append((t, int(re.search(r', (\d+) min', m).group(1))))
        elif 'window open: slot' in m or 'discovery window open' in m:
            L['win'].append((t, int(re.search(r'for (\d+) us', m).group(1)) / 1e6))
        elif m.startswith('Transmitting ...'):
            L['tx'].append((t, t + int(re.search(r'len=(\d+)', m).group(1)) * 8 / 50000))
        elif m.startswith('Receiving ...'):
            n = int(re.search(r'pkt_len=(\d+)', m).group(1))
            if n >= 30:
                L['rx'].append(t)
                L['rxe'].append((t, t + n * 8 / 50000))
        elif 'test-udp rx from' in m:
            src, p = re.search(r'from (\S+): (.*)', m).groups()
            L['udp_rx'].append((t, src, p))
        elif 'test-udp tx to' in m:
            dst, p = re.search(r'to (\S+): (\S+ \d+)', m).groups()
            L['udp_tx'].append((t, dst, p))
        elif 'LFN sync: slot' in m:
            r = re.search(r'slot (\d+), error (-?\d+) us, rate (-?\d+) ppb', m)
            L['sync'].append((t, int(r.group(1)), int(r.group(2)), int(r.group(3))))
        elif 'LFN: LGTK key request' in m:
            L['keyreq'].append(t)
        elif 'LGTK hash mismatch' in m:
            L['mismatch'].append((t, int(re.search(r'key request in (\d+) s', m).group(1))))
        elif 'LGTK recv index' in m:
            L['lgtk_recv'].append((t, int(re.search(r'index (-?\d+)', m).group(1))))
        elif 'LFN: active LGTK index' in m:
            L['active'].append((t, int(re.search(r'index (\d+)', m).group(1))))
        elif 'LFN listen schedule:' in m:
            r = re.search(r'interval (\d+) ms', m)
            if r:
                L['interval'] = int(r.group(1))
        r = re.search(r'LFN: parent (\S+), PAN', m)
        if r:
            L['parent'] = r.group(1)
    oper = L['oper'][0] if L['oper'] else None
    check(oper is not None and oper <= join_max and L['parent'] == mac(parent),
          f"operational at {oper:.1f} s, parent {L['parent']}" if oper else 'operational')
    check(len(L['disc']) == 1 and not L['lost'] and not L['search'],
          f"kept its parent ({len(L['disc'])} discovery, {len(L['lost'])} parent losses, {len(L['search'])} parent searches)")
    if L['interval']:
        print(f"info listen interval {L['interval']} ms")
    # Address registration: each ARO's lifetime must reach the next ARO (or the end)
    aros = L['aro']
    ok, txt = bool(aros), []
    for k, (t, life) in enumerate(aros):
        nxt = aros[k + 1][0] if k + 1 < len(aros) else None
        expiry = t + life * 60
        if nxt is not None:
            txt.append(f"ARO at {t / 3600:.2f} h, next {(nxt - t) / 3600:.2f} h later (margin {(expiry - nxt) / 3600:.2f} h)")
            ok &= nxt < expiry
        else:
            txt.append(f"last ARO at {t / 3600:.2f} h, lifetime to {expiry / 3600:.2f} h")
            ok &= expiry > dur
    check(ok, f"address registration ({len(aros)} AROs, lifetime {aros[0][1] if aros else '?'} min): " + '; '.join(txt))
    # Traffic
    last = dur - 2 * 300 - 60
    def pairs(tx, rx_peer, rx_back, dst):
        q = [(t, p) for t, d, p in tx if p.startswith('Q') and d == dst and (oper or dur) + 60 < t < last]
        got = {p for t, s, p in rx_peer}
        back = {p.replace('R', 'Q', 1) for t, s, p in rx_back if p.startswith('R')}
        return q, [x for x in q if x[1] in got], [x for x in q if x[1] in back]
    q, got, back = pairs(br['udp_tx'], L['udp_rx'], br['udp_rx'], lfn_addr(node))
    lost = [f'{t / 3600:.2f} h' for t, p in q if (t, p) not in back]
    check(q and len(got) == len(q) and len(back) == len(q),
          f'BR -> LFN unicast: {len(q)} requests, {len(got)} received, {len(back)} echoes back' + (f' (lost: {", ".join(lost[:8])})' if lost else ''))
    q, got, back = pairs(L['udp_tx'], br['udp_rx'], L['udp_rx'], 'fd12:3456::1615:16ff:fe17:1819')
    lost = [f'{t / 3600:.2f} h' for t, p in q if (t, p) not in back]
    check(q and len(got) == len(q) and len(back) == len(q),
          f'LFN -> BR unicast: {len(q)} requests, {len(got)} received, {len(back)} echoes back' + (f' (lost: {", ".join(lost[:8])})' if lost else ''))
    if parent == 0:
        q = [(t, p) for t, d, p in br['udp_tx'] if d == 'ff02::1' and p.startswith('Q') and (oper or dur) + 60 < t < last]
        got = {p for t, s, p in L['udp_rx']}
        check(q and all(p in got for t, p in q), f"BR -> ff02::1: {len(q)} requests, {sum(p in got for t, p in q)} received by the LFN")
    # Frames inside the windows
    win = sorted(L['win']); win_t = [w[0] for w in win]
    def in_window(t):
        i = bisect.bisect_right(win_t, t + 1e-6) - 1
        return i >= 0 and win[i][0] - 1e-6 <= t <= win[i][0] + win[i][1] + 1e-6
    txl = sorted(L['tx']); txs = [x[0] for x in txl]
    def after_own_tx(t):
        i = bisect.bisect_right(txs, t) - 1
        return i >= 0 and t - txl[i][1] < 0.06
    rxe = sorted(L['rxe']); rxs = [x[0] for x in rxe]
    def during_ack(t):
        i = bisect.bisect_left(rxs, t) - 1
        while i >= 0 and rxe[i][0] > t - 1:
            if t - rxe[i][1] < 0.01:
                return True
            i -= 1
        return False
    outside = [t for t in L['rx'] if not in_window(t) and not after_own_tx(t) and not during_ack(t)]
    check(L['rx'] and not outside, f"{len(L['rx'])} frames received: {len(outside)} outside its windows"
          + (f" (first at {outside[0]:.6f} s)" if outside else ''))
    # Sync: the frames that set the LFN's phase (LTS or broadcast LPC at a
    # slot start: the 'LFN sync' lines) after the join. A parent may send a
    # sync frame one slot late (its MAC busy with a PAN advertisement or
    # configuration sweep): the check allows one missed sync period
    sp = bci * bcsp / 1000
    s_after = [s for s in L['sync'] if oper and s[0] > oper]
    periods = {int((s[0] - oper) // sp) for s in s_after}
    expected = int((dur - (oper or dur)) // sp)
    missed = expected - len([k for k in periods if k < expected])
    measured = [s for s in s_after if s[3] != 0]
    errs = [abs(s[2]) for s in s_after[2:]]
    times = [oper] + [s[0] for s in s_after] + [dur] if oper else []
    gaps = [b - a for a, b in zip(times, times[1:])]
    check(s_after and max(gaps) < 2.2 * sp,
          f"sync: {len(s_after)} sync frames after the join, {missed} of {expected} sync periods without one "
          f"(longest gap {max(gaps) / 3600 if gaps else 0:.2f} h, sync period {sp / 3600:.3f} h)")
    print(f"info rate measured: {'%d ppb' % measured[-1][3] if measured else 'never'}; "
          f"largest phase error from the third sync: {max(errs) if errs else '-'} us; last {s_after[-1][2] if s_after else '-'} us")
    # LGTK
    check(L['lgtk_recv'], f"joined with an LGTK (index {L['lgtk_recv'][0][1] if L['lgtk_recv'] else '?'})")
    for ti, idx in [x for x in br['installed'] if oper and x[0] > oper]:
        mm = [x for x in L['mismatch'] if x[0] > ti]
        req = [t for t in L['keyreq'] if t > ti]
        got = [t for t, i in L['lgtk_recv'] if t > ti and i == idx]
        act = [t for t, i in br['activated'] if t > ti and i == idx]
        if act and act[0] > dur - 1.5 * sp:
            act = []
        sw = [t for t, i in L['active'] if act and t > act[0] and i == idx]
        check(mm and req and got and got[0] < (act[0] if act else dur + 1),
              f"LGTK {idx} installed at {ti / 3600:.2f} h: hash at the LFN {(mm[0][0] - ti) / 3600:.2f} h later, key request "
              f"{(req[0] - mm[0][0]) / 60:.1f} min after that, key {(got[0] - req[0]):.1f} s after the request"
              + (f", activated {(act[0] - ti) / 3600:.2f} h after its installation" if act else '')
              if mm and req and got else f"LGTK {idx} installed at {ti / 3600:.2f} h: hash {'yes' if mm else 'no'}, request {'yes' if req else 'no'}, key {'yes' if got else 'no'}")
        if act:
            check(sw, f"LGTK {idx} activated at {act[0] / 3600:.2f} h: the LFN took it {(sw[0] - act[0]) / 3600:.2f} h later" if sw else f"LGTK {idx} activated: LFN switch")
    # Power, from steady_from to the end
    rep = json.load(open(f'{out}/power.json'))
    n = next(x for x in rep['nodes'] if x['node'] == node)
    ma = {('mcu', k): v['ma'] for k, v in n['mcu']['states'].items()}
    ma.update({('radio', k): v['ma'] for k, v in n['radio']['states'].items() if 'ma' in v})
    tx_ma = {lv['dbm']: lv['ma'] for lv in n['radio']['states']['tx']['levels']}
    t0, t1 = steady_from * 1e6, dur * 1e6
    parts, wakes = {}, 0
    for line in open(f'{out}/power_lfn.txt'):
        if line.startswith('#'):
            continue
        f = line.split()
        s, e, nd, part, state = float(f[0]), float(f[1]), int(f[2]), f[3], f[4]
        if nd != node or e <= t0 or s >= t1:
            continue
        if part == 'mcu' and state == 'active' and s >= t0:
            wakes += 1
        s, e = max(s, t0), min(e, t1)
        cur = tx_ma[int(f[5])] if state == 'tx' else ma.get((part, state), 0)
        k = f'{part} {state}'
        tm, ch = parts.get(k, (0.0, 0.0))
        parts[k] = (tm + (e - s), ch + (e - s) * cur / 1e3)
    span = (t1 - t0) / 1e6
    parts['mcu wake (per-wake charge)'] = (0.0, wakes * n['mcu']['wake_charge']['uc_each'])
    total = sum(c for t, c in parts.values())
    print(f"info current {steady_from / 3600:g}-{dur / 3600:g} h: {total / span:.2f} uA ({wakes / span:.3f} wakes/s); whole run {n['avg_current_ma'] * 1000:.2f} uA")
    for k, (tm, ch) in sorted(parts.items(), key=lambda x: -x[1][1])[:5]:
        if ch > 0:
            print(f'info   {k:28s} {ch / span:7.2f} uA')
if br['expired']:
    print(f"info LGTKs expired at the border router: {', '.join(f'{t / 3600:.2f} h' for t in br['expired'])}")
print('test_lfn_profiles: ' + ('PASSED' if not fails else f'FAILED ({len(fails)})'))
sys.exit(1 if fails else 0)
EOF
RC=$?
if [ "${KEEP_LOGS:-0}" != 1 ]; then
    # Keep what the checks read, compressed
    for f in "$OUT"/node*.log "$OUT"/br.log; do
        grep -E 'LFN|ARO|LGTK|test-udp|wspa|Receiving|Transmitting|parent|lost|expired|DHCP|dhcp' "$f" | gzip > "$f.gz"
        rm -f "$f"
    done
    rm -f "$OUT/log_wsbr"
    gzip -f "$OUT/power_lfn.txt"
fi
exit $RC
