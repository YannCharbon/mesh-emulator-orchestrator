#!/bin/bash
# FAN 1.1 LFN unicast mechanics in the MAC and FHSS, end to end on the
# emulated radio in virtual time (GS8 stage 2; the stack's docs/LFN_MAC.md).
# Two wslfntest-inproc nodes: node 0 is the parent (FFN, unicast hopping and
# a broadcast schedule, LFN parenting on), node 1 the LFN (unicast listen
# schedule, receiver on only in its windows). The program plays the LLC: the
# LFN solicits on every channel with LUTT/LUS until the parent answers in its
# window; then the parent sends to the LFN every few seconds (held for the
# LFN's windows) and the LFN sends to the parent every 10 s.
#
# Checks, from the logs and the power ledger:
#   - the parent learnt the LFN's schedule from its solicit, and the LFN the
#     parent's from the answer;
#   - every frame the LFN received started inside one of its windows
#     ([slot start - guard, slot start - guard + window));
#   - the parent sent each frame on the LFN's channel for that slot (the
#     channel the LFN's window used);
#   - the parent's frames reached the LFN (confirmed, received once each), and
#     each waited at most about one listen interval;
#   - the LFN's frames reached the parent, sent at once (TX, ACK, radio off);
#   - the LFN's radio was on only in its windows (plus a deferred close) and
#     for its own transmissions, and off most of the time; its MCU slept
#     deep most of the time;
#   - the windows opened once per slot, on time.
#
# usage: test_lfn_mac.sh <bindir> <workdir(holds examples/)> <outdir> [profile]
#   bindir   a virtual-time build with wssimserver and wslfntest-inproc
#   profile  power profile (default <workdir>/examples/power/efr32fg25-soc.ini)
# Environment: DUR (300), WSSIM_SEED (1), RSSI (-50), LFN_OPTS, PARENT_OPTS
# (more wslfntest-inproc options for one node, e.g.
# LFN_OPTS="--listen-interval-ms=2000 --rx-window-us=3000").
# Exit status 0 if every check passes.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
PROFILE=${4:-$WORK/examples/power/efr32fg25-soc.ini}
DUR=${DUR:-300}; RSSI=${RSSI:--50}
export WSSIM_SEED=${WSSIM_SEED:-1}
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_lfn_mac: $OUT is not empty" >&2; exit 1; }
SOCK=$(mktemp -u --suffix=.sock /tmp/wssim-lfn-XXXXXXXX)
export DBUS_STARTER_BUS_TYPE=user DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent/wssim-vt-no-dbus
mac() { printf '01:02:03:04:05:06:%02X:%02X' $(($1 >> 8)) $(($1 & 0xFF)); }
PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; rm -f "$SOCK"; }
trap 'cleanup; exit 1' INT TERM HUP

cd "$OUT" || exit 1
echo 1700000000 > t0
"$BIN/wssimserver" -g "0,1:$RSSI" --vtime=2 --vtime-end="$DUR" --power="$PROFILE" \
    --power-out="$OUT/power.json" --power-log="$OUT/power_log.txt" "$SOCK" > simserver.log 2>&1 &
SIM=$!; PIDS+=($SIM)
for (( i = 0; i < 100; i++ )); do [ -S "$SOCK" ] && break; sleep 0.05; done
[ -S "$SOCK" ] || { echo "test_lfn_mac: no socket" >&2; cleanup; exit 1; }
"$BIN/wslfntest-inproc" --eui64="$(mac 0)" -u "$SOCK" --role=parent --peer="$(mac 1)" ${PARENT_OPTS:-} > parent.log 2>&1 &
PIDS+=($!)
"$BIN/wslfntest-inproc" --eui64="$(mac 1)" -u "$SOCK" --role=lfn --peer="$(mac 0)" ${LFN_OPTS:-} > lfn.log 2>&1 &
PIDS+=($!)
wait $SIM; RC=$?
wait
rm -f "$SOCK"
[ $RC = 0 ] || { echo "test_lfn_mac: wssimserver exited with $RC ($OUT/simserver.log)"; exit 1; }

python3 - "$OUT" "$DUR" <<'EOF'
import json, re, sys
out, dur = sys.argv[1], float(sys.argv[2])
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

# --- LFN log
sched = None
rx_frames = []          # frame start times of frames the LFN received (data, not ACKs)
opens = {}              # slot -> (time, channel)
lfn_req, lfn_conf, lfn_rx = {}, {}, {}
lfn_tx_spans = []       # own transmissions: (request, confirm)
lfn_tx_starts = []      # start of every frame the LFN sent
lfn_data_tx = []        # the same, without ACKs
learnt = None
stats = None
pbc = None              # parent broadcast schedule: (interval start, interval, dwell) in s
lto = None              # (time, offset ms, interval ms, ret)
for t, m in lines('lfn.log'):
    if 'lfntest lfn: schedule started' in m:
        v = dict(kv.split('=') for kv in m.split()[4:])
        sched = {k: int(x) for k, x in v.items()}
        sched['t_start'] = t
    elif m.startswith('Receiving ...'):
        n = int(re.search(r'pkt_len=(\d+)', m).group(1))
        if n >= 30:
            rx_frames.append(t)
    elif m.startswith('Transmitting ...'):
        lfn_tx_starts.append(t)
        if int(re.search(r'len=(\d+)', m).group(1)) >= 30:
            lfn_data_tx.append(t)         # not an ACK
    elif m.startswith('LFN window open:'):
        s, ch = re.search(r'slot (\d+) ch (\d+)', m).groups()
        opens[int(s)] = (t, int(ch))
    elif 'lfntest tx-req' in m:
        k, seq, h = re.search(r'tx-req (\w) seq=(\d+) handle=(\d+)', m).groups()
        lfn_req[int(h)] = (k, int(seq), t)
    elif 'lfntest conf' in m:
        h, st = re.search(r'handle=(\d+) status=(\d+)', m).groups()
        lfn_conf[int(h)] = (int(st), t)
    elif 'lfntest rx' in m:
        k, seq = re.search(r'rx (\w) seq=(\d+)', m).groups()
        lfn_rx.setdefault(k, set()).add(int(seq))
    elif 'parent schedule learnt' in m:
        learnt = t
    elif 'lfntest lfn: parent bc' in m:
        v = dict(kv.split('=') for kv in m.split()[4:])
        pbc = (t - int(v['offset']) / 1e3, int(v['interval']) / 1e3, int(v['dwell']) / 1e3)
    elif 'lfntest lfn: lto' in m:
        v = dict(kv.split('=') for kv in m.split()[3:])
        lto = (t, int(v['offset']), int(v['interval']), int(v['ret']))
    elif 'lfntest stats' in m:
        stats = dict((a, int(b)) for a, b in re.findall(r'(\w+)=(\d+)', m))
for h, (k, seq, tr) in lfn_req.items():
    if h in lfn_conf:
        lfn_tx_spans.append((tr, lfn_conf[h][1]))

check(sched is not None, 'LFN schedule started')
I = sched['interval'] / 1e3; W = sched['window'] / 1e6; G = sched['guard'] / 1e6
slot0 = sched['t_start'] + sched['first_slot_delay'] / 1e6
I0 = I
# With an LTO, the grid after it: anchored on the first window opened after
# it (checked below against the LTO's offset and interval)
win_opens = []           # (open time, slot), in order, from the log
for t, m in lines('lfn.log'):
    if m.startswith('LFN window open:'):
        win_opens.append((t, int(re.search(r'slot (\d+)', m).group(1))))
win_t = [t for t, s in win_opens]
import bisect

def slot_of(t):          # the window (actually opened) that contains t, or None
    i = bisect.bisect_right(win_t, t + 1e-6) - 1
    if i >= 0 and win_t[i] - 1e-6 <= t < win_t[i] + W:
        return win_opens[i][1]
    return None

# --- parent log
par_req, par_conf, par_rx = {}, {}, {}
lfn_tx_ch = []
p_learnt = None
for t, m in lines('parent.log'):
    if 'lfntest tx-req' in m:
        k, seq, h = re.search(r'tx-req (\w) seq=(\d+) handle=(\d+)', m).groups()
        par_req[int(h)] = (k, int(seq), t)
    elif 'lfntest conf' in m:
        h, st, r = re.search(r'handle=(\d+) status=(\d+) tx_retries=(\d+)', m).groups()
        par_conf[int(h)] = (int(st), t, int(r))
    elif 'lfntest rx' in m:
        k, seq = re.search(r'rx (\w) seq=(\d+)', m).groups()
        par_rx.setdefault(k, set()).add(int(seq))
    elif m.startswith('LFN TX channel:'):
        ch, s = re.search(r'channel: (\d+) slot (\d+)', m).groups()
        lfn_tx_ch.append((t, int(s), int(ch)))
    elif 'lfntest parent: lfn schedule' in m and p_learnt is None:
        p_learnt = t

check(p_learnt is not None, f'parent learnt the LFN schedule from its solicit at {p_learnt:.3f} s' if p_learnt else 'parent learnt the LFN schedule')
check(learnt is not None, f'LFN learnt the parent schedule from its answer at {learnt:.3f} s' if learnt else 'LFN learnt the parent schedule')

# Frames the LFN received: all inside a window
outside = [t for t in rx_frames if slot_of(t) is None]
check(len(rx_frames) > 0 and not outside,
      f'{len(rx_frames)} frames received by the LFN, {len(outside)} outside its windows' + (f' (first at {outside[0]:.6f} s)' if outside else ''))

# Channel: the parent's choice equals the LFN's window channel for that slot
mism = [(t, s, ch) for t, s, ch in lfn_tx_ch if s not in opens or opens[s][1] != ch]
check(lfn_tx_ch and not mism, f'{len(lfn_tx_ch)} parent CCA/TX on the LFN channel of the slot, {len(mism)} mismatches')

# Downlink: data frames confirmed, received once each, delay bounded
d = [(h, seq, tr) for h, (k, seq, tr) in par_req.items() if k == 'D' and tr < dur - 2 * I - 1]
ok = [(h, seq, tr) for h, seq, tr in d if h in par_conf and par_conf[h][0] == 0]
got = lfn_rx.get('D', set())
check(len(d) > 0 and len(ok) >= 0.95 * len(d),
      f'parent -> LFN: {len(ok)}/{len(d)} data frames acknowledged')
check(all(seq in got for h, seq, tr in ok), f'every acknowledged frame received by the LFN ({len(got)} distinct)')
delays = [par_conf[h][1] - tr for h, seq, tr in ok if par_conf[h][2] == 0]
check(delays and max(delays) <= I + 0.05,
      f'first-attempt delay request -> confirm: max {max(delays):.3f} s, mean {sum(delays)/len(delays):.3f} s (interval {I:.3f} s)' if delays else 'delays')
retried = [(h, tr) for h, seq, tr in ok if par_conf[h][2] > 0]
late = [h for h, tr in retried if par_conf[h][1] - tr > (par_conf[h][2] + 1) * I + 0.05]
check(not late, f'{len(retried)} downlink frames retried, one attempt per window ({len(late)} later than that)')

# Uplink
u = [(h, seq, tr) for h, (k, seq, tr) in lfn_req.items() if k == 'U' and tr < dur - 1]
uok = [x for x in u if x[0] in lfn_conf and lfn_conf[x[0]][0] == 0]
check(len(u) > 0 and len(uok) >= 0.8 * len(u) and all(x[1] in par_rx.get('U', set()) for x in uok),
      f'LFN -> parent: {len(uok)}/{len(u)} data frames acknowledged and received')

# Windows: one per slot, on time (on the LTO's grid after an LTO)
def expected_open(t_open, s):
    if lto and t_open > lto[0]:
        # slot n of the old grid nearest to the LTO frame, moved by the offset
        n = round((lto[0] - slot0) / I0)
        base = slot0 + n * I0 + lto[1] / 1e3
        return base + ((s - n) % 65536) * (lto[2] / 1e3 if lto[2] else I0) - G
    return slot0 + s * I0 - G
late = [(t, s) for t, s in win_opens if abs(t - expected_open(t, s)) > 1e-6]
gaps = [b[0] - a[0] for a, b in zip(win_opens, win_opens[1:]) if not (lto and a[0] < lto[0] < b[0] + 1)]
I_after = (lto[2] / 1e3 if lto and lto[2] else I0)
check(win_opens and not late and all(abs(g - I0) < 1e-6 or abs(g - I_after) < 1e-6 for g in gaps),
      f'{len(win_opens)} windows opened, one per slot, {len(late)} off time' + (f' (first {late[0]})' if late else ''))
if lto:
    check(lto[3] == 0, f'LFN applied the LTO (offset {lto[1]} ms, interval {lto[2]} ms) at {lto[0]:.3f} s')
# Uplinks clear of the parent's broadcast dwell (TX start to start + 5 ms)
if pbc:
    bs, bi, bd = pbc
    up = [t for t in lfn_data_tx if learnt and t > learnt]
    # 2 ms of slack: the BT-IE's offset is in whole ms
    in_dwell = [t for t in up if ((t - bs) % bi) < bd - 0.002 or ((t - bs) % bi) + 0.005 > bi + 0.002]
    check(up and not in_dwell, f'{len(up)} LFN uplink transmissions, {len(in_dwell)} in or into the parent broadcast dwell')
if stats:
    print(f"info LFN FHSS: windows_opened={stats['windows_opened']} skipped={stats['windows_skipped']} closes_deferred={stats['closes_deferred']}")

# Radio: on only in windows (with a deferred close) and around its own
# transmissions: the CCA before (the backoff does not listen: idle), the
# frame and the ACK wait after (at most 30 ms); off while a failed frame waits
# for its restart
CLOSE_SLACK = 0.02
import bisect
lfn_tx_starts.sort()
def near_tx(s, e):
    i = bisect.bisect_left(lfn_tx_starts, s)
    return i < len(lfn_tx_starts) and lfn_tx_starts[i] - 0.1 <= s and e <= lfn_tx_starts[i] + 0.03 or \
        (i > 0 and lfn_tx_starts[i - 1] <= s and e <= lfn_tx_starts[i - 1] + 0.03)
on_outside = 0.0
outside_iv = []
radio, mcu = {}, {}
first = slot0 - G
for line in open(f'{out}/power_log.txt'):
    if line.startswith('#'):
        continue
    f = line.split()
    s, e, node, part, state = float(f[0]) / 1e6, float(f[1]) / 1e6, int(f[2]), f[3], f[4]
    if node != 1 or e <= first:
        continue
    s = max(s, first)
    (radio if part == 'radio' else mcu)[state] = (radio if part == 'radio' else mcu).get(state, 0) + (e - s)
    if part != 'radio' or state in ('off', 'idle'):
        continue           # idle: a CSMA backoff outside the windows, not listening
    i = bisect.bisect_right(win_t, s + 1e-6) - 1
    in_win = i >= 0 and win_t[i] - 1e-6 <= s and e <= win_t[i] + W + CLOSE_SLACK
    in_tx = near_tx(s, e)
    if not in_win and not in_tx:
        on_outside += e - s
        outside_iv.append((s, e, state))
span = dur - first
off = radio.get('off', 0)
check(on_outside < 1e-3, f'LFN radio listening or sending outside its windows and its transmissions (TX, ACK, off): {on_outside*1e3:.3f} ms'
      + (f' (first: {outside_iv[0][0]:.6f}-{outside_iv[0][1]:.6f} s {outside_iv[0][2]})' if outside_iv else ''))
check(off + radio.get('idle', 0) > (1 - W / I - 0.02) * span, f'LFN radio off {off:.1f} s, idle {radio.get("idle", 0):.2f} s of {span:.1f} s ({100*off/span:.2f} % off; windows are {100*W/I:.2f} % of the time)')
check(mcu.get('deep_sleep', 0) > 0.9 * span, f'LFN MCU in deep sleep {mcu.get("deep_sleep", 0):.1f} s of {span:.1f} s')

report = json.load(open(f'{out}/power.json'))
n1 = next(n for n in report['nodes'] if n['node'] == 1)
n0 = next(n for n in report['nodes'] if n['node'] == 0)
print(f"info LFN average current {n1['avg_current_ma']*1e3:.1f} uA, parent {n0['avg_current_ma']:.3f} mA (profile {n1['profile']})")
check(n0['radio']['states']['off']['time_us'] == 0, 'parent (FFN) radio never off')
sums = all(abs(n['check']['mcu_time_us'] - n['check']['run_time_us']) < 1 and
           abs(n['check']['radio_time_us'] - n['check']['run_time_us']) < 1 for n in report['nodes'])
check(sums, 'state times add up to the run time for both nodes')
print('test_lfn_mac: ' + ('PASSED' if not fails else f'FAILED ({len(fails)})'))
sys.exit(1 if fails else 0)
EOF
