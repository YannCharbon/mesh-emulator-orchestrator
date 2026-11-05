#!/bin/bash
# Emulator reference figures for the LFN kit runs (FG25 kits: border router,
# router, an LFN under each; Silicon Labs' Test, Balanced and Eco timers,
# firmware FW_LFN_PROFILE): the LFN tests that mirror the kit scenarios, run
# long in virtual time with the default radio medium (central, docs/MEDIUM.md),
# then one summary of the figures the kit runs are compared with.
#
#   profiles  test_lfn_profiles.sh (border router - router - LFN 2, LFN 3
#             under the border router; sparse traffic, a request each way
#             every 30-45 min): join, unicast delivery and downlink latency,
#             re-registrations, current per profile with the LFN power
#             profile (efr32fg25-lfn.ini, LFNs at +10 dBm). Test 24 h, Balanced
#             and Eco 72 h; current from 3 h (Eco: from 24 h, once the sync
#             frames 10 h apart have given the clock rate).
#   silence   test_lfn_silence.sh: downlinks after 1 h, 3 h, 10 h and 12 h of
#             silence (about 26 h: the defaults' registration, 36 h, is
#             refreshed at about 33 h, which ends a longer silence), under the
#             stack's defaults, Balanced and Eco.
#   restart   test_lfn_restart.sh: the router restarts at 600 s, the LFN at
#             1200 s (stack's defaults).
#   mcast     test_lfn_mcast.sh: group subscriptions under the stack's
#             defaults, Balanced and Eco.
#   better    test_lfn_better.sh: a better parent appears (move) or a
#             marginally better one (stay).
#
# Seeds per case (root CLAUDE.md sizing rule): 3 for the profiles and the
# restarts, 2 for the others; about 6 min of wall time with 12 jobs on a
# 24-core PC (the 72 h runs are the longest).
#
# usage: lfn_kit_reference.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: JOBS (12), ONLY (a regular expression on the run names),
# SIM_OPTS (default --drift-random=20; e.g. add --medium=legacy).
# Output: <outdir>/<name>.txt per run, <outdir>/results.txt (PASS/FAIL),
# <outdir>/summary.txt. The run directories are deleted once their figures
# are extracted (downlink latencies: <outdir>/<name>.lat).
set -u
[ $# = 3 ] || { echo "usage: lfn_kit_reference.sh <bindir> <workdir> <outdir>" >&2; exit 2; }
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "lfn_kit_reference: $OUT is not empty" >&2; exit 1; }
JOBS=${JOBS:-12}
export TUN=none SIM_OPTS=${SIM_OPTS:---drift-random=20}
V=$HERE; LP=$WORK/examples/power/efr32fg25-lfn.ini
prof() { (PROF=$1; . "$V/lfn_profile.sh"; echo "${LFN_PROF//[$'\n ']/@}|${PARENT_PROF//[$'\n ']/@}"); }
{
for s in 1 2 3; do
    echo "profiles_test_s$s|WSSIM_SEED=$s DUR=86400 STEADY_FROM=10800 LFN_POWER=$LP LFN_EXTRA=-o@tx_power=10|$V/test_lfn_profiles.sh $BIN $WORK <out> test"
    echo "profiles_balanced_s$s|WSSIM_SEED=$s DUR=259200 STEADY_FROM=10800 LFN_POWER=$LP LFN_EXTRA=-o@tx_power=10|$V/test_lfn_profiles.sh $BIN $WORK <out> balanced"
    echo "profiles_eco_s$s|WSSIM_SEED=$s DUR=259200 STEADY_FROM=86400 LFN_POWER=$LP LFN_EXTRA=-o@tx_power=10|$V/test_lfn_profiles.sh $BIN $WORK <out> eco"
    echo "restart_s$s|WSSIM_SEED=$s KEEP=1|$V/test_lfn_restart.sh $BIN $WORK <out>"
done
for s in 1 2; do
    echo "silence_default_s$s|WSSIM_SEED=$s KEEP=1 SILENCES=3600@10800@36000@43200|$V/test_lfn_silence.sh $BIN $WORK <out>"
    for p in balanced eco; do
        P=$(prof $p)
        echo "silence_${p}_s$s|WSSIM_SEED=$s KEEP=1 SILENCES=3600@10800@36000@43200 JOIN_REF=1200 JOIN_MAX=3600 LFN_EXTRA=${P%%|*} PARENT_EXTRA=${P#*|}|$V/test_lfn_silence.sh $BIN $WORK <out>"
    done
    echo "mcast_default_s$s|WSSIM_SEED=$s|$V/test_lfn_mcast.sh $BIN $WORK <out>"
    for p in balanced eco; do echo "mcast_${p}_s$s|WSSIM_SEED=$s PROF=$p|$V/test_lfn_mcast.sh $BIN $WORK <out>"; done
    echo "better_move_s$s|WSSIM_SEED=$s EXPECT=move|$V/test_lfn_better.sh $BIN $WORK <out>"
    echo "better_stay_s$s|WSSIM_SEED=$s EXPECT=stay|$V/test_lfn_better.sh $BIN $WORK <out>"
done
} | { [ -n "${ONLY:-}" ] && grep -E "^[^|]*(${ONLY})" || cat; } > "$OUT/jobs.txt"

# Downlink latencies of a run (the border router's requests to each LFN, to
# their arrival): "<lfn> <request> <sent s> <latency s>" per line
latency() {   # latency <run dir>
    python3 - "$1" <<'EOF'
import gzip, re, sys, os
out = sys.argv[1]
def lines(name):
    for p in (f'{out}/{name}.log', f'{out}/{name}.log.gz'):
        if os.path.exists(p):
            with (gzip.open(p, 'rt', errors='replace') if p.endswith('.gz') else open(p, errors='replace')) as f:
                for l in f:
                    t, _, m = l.partition(' ')
                    try:
                        yield float(t) - 1700000000.0, m
                    except ValueError:
                        pass
            return
sent = {}
for t, m in lines('br'):
    if r := re.search(r'test-udp tx to fd12:3456::302:304:506:([0-9a-f]+): (Q \d+)', m):
        sent[(int(r[1], 16), r[2])] = t
nodes = sorted({k[0] for k in sent})
for n in nodes:
    got = {}
    for t, m in lines(f'node{n}'):
        if (r := re.search(r'test-udp rx from \S+: (Q \d+)', m)) and r[1] not in got:
            got[r[1]] = t
    for (node, q), t in sorted(sent.items(), key=lambda x: x[1]):
        if node == n:
            print(n, q.replace(' ', ''), f'{t:.1f}', f'{got[q] - t:.3f}' if q in got else 'lost')
EOF
}
run() {   # run <job line>
    local name=${1%%|*} rest=${1#*|}; local envs=${rest%%|*} cmd=${rest#*|}
    local t0=$SECONDS rc
    cmd=${cmd//<out>/$OUT/$name}
    eval "env ${envs//@/\\ } $cmd" > "$OUT/$name.txt" 2>&1
    rc=$?
    [ -d "$OUT/$name" ] && latency "$OUT/$name" > "$OUT/$name.lat"
    rm -rf "${OUT:?}/$name" "$OUT/$name".run "$OUT/$name".*.run
    if [ $rc = 0 ]; then echo "PASS $name ($((SECONDS - t0)) s)"
    else echo "FAIL $name ($((SECONDS - t0)) s): $(grep -m1 -E '^FAIL|run failed|Error' "$OUT/$name.txt" | cut -c1-160)"; fi
}
export -f run latency; export OUT
T0=$SECONDS
echo "lfn_kit_reference: $(wc -l < "$OUT/jobs.txt") runs, $JOBS at a time, SIM_OPTS=$SIM_OPTS, outputs in $OUT"
tr '\n' '\0' < "$OUT/jobs.txt" | xargs -0 -P "$JOBS" -I{} bash -c 'run "$1"' _ {} | tee "$OUT/results.txt"
NP=$(grep -c '^PASS' "$OUT/results.txt"); NF=$(grep -c '^FAIL' "$OUT/results.txt")
echo "lfn_kit_reference: $NP passed, $NF failed, $((SECONDS - T0)) s wall time"

# The summary
python3 - "$OUT" > "$OUT/summary.txt" <<'EOF'
import collections, glob, os, re, statistics, sys
out = sys.argv[1]
def rng(v, f='{:.1f}'):
    v = sorted(v)
    if not v:
        return '-'
    return (f.format(v[0]) if len(v) == 1 else f'{f.format(v[0])}-{f.format(v[-1])}') + f' (median {f.format(statistics.median(v))}, n={len(v)})'
groups = collections.defaultdict(list)
for p in sorted(glob.glob(f'{out}/*.txt')):
    n = os.path.basename(p)[:-4]
    if n in ('results', 'summary', 'jobs'):
        continue
    groups[re.sub(r'_s\d+$', '', n)].append(p)
for g, files in sorted(groups.items()):
    print(f'== {g} ({len(files)} runs)')
    txt = {f: open(f).read() for f in files}
    lat = []
    for f in files:
        lf = f[:-4] + '.lat'
        if os.path.exists(lf):
            lat += [l.split() for l in open(lf) if l.strip()]
    if g.startswith('profiles'):
        for node, label in ((2, 'LFN 2, under the router'), (3, 'LFN 3, under the border router')):
            joins, cur, uc, regs = [], [], [], []
            for t in txt.values():
                part = t.split(f'--- LFN {node}')[1].split('--- LFN')[0] if f'--- LFN {node}' in t else ''
                if m := re.search(r'operational at ([\d.]+) s', part):
                    joins.append(float(m[1]))
                if m := re.search(r'info current [^:]+: ([\d.]+) uA', part):
                    cur.append(float(m[1]))
                for m in re.finditer(r'(BR -> LFN|LFN -> BR) unicast: (\d+) requests, (\d+) received', part):
                    uc.append((m[1], int(m[2]), int(m[3])))
                if m := re.search(r'address registration \((\d+) AROs', part):
                    regs.append(int(m[1]))
            dl = [float(x[3]) for x in lat if x[0] == str(node) and x[3] != 'lost']
            tot = collections.defaultdict(lambda: [0, 0])
            for d, a, b in uc:
                tot[d][0] += a; tot[d][1] += b
            print(f'  {label}: operational {rng(joins)} s; current {rng(cur, "{:.2f}")} uA; AROs {rng(regs, "{:.0f}")}')
            print('    ' + '; '.join(f'{d} {b}/{a}' for d, (a, b) in tot.items()) +
                  f'; downlink latency {rng(dl, "{:.2f}")} s')
    elif g.startswith('silence'):
        for node in (2, 3):
            joins = [float(m[1]) for t in txt.values() for m in re.finditer(rf'LFN {node} [^:]*: operational at ([\d.]+) s', t)]
            got = [float(m[2]) for t in txt.values() for m in re.finditer(rf'LFN {node}: request Q \d+ at [\d.]+ h, after ([\d.]+) h of silence: received ([\d.]+) s later', t)]
            miss = sum(len(re.findall(rf'FAIL LFN {node}: request', t)) for t in txt.values())
            print(f'  LFN {node}: operational {rng(joins)} s; {len(got)} downlinks after silences, {miss} missed; latency {rng(got, "{:.1f}")} s')
    elif g.startswith('restart'):
        for what, rx in (('LFN operational', r'LFN operational at ([\d.]+) s'),
                         ('router back after its restart', r'router restarted at \d+ s: operational again ([\d.]+) s later'),
                         ('LFN back after its restart', r'LFN restarted at \d+ s: operational again ([\d.]+) s later'),
                         ('first downlink echoed after the router restart', r'after the router restart: first downlink echoed ([\d.]+) s'),
                         ('first uplink echoed after the router restart', r'after the router restart: first uplink echoed ([\d.]+) s'),
                         ('first downlink echoed after the LFN restart', r'after the LFN restart: first downlink echoed ([\d.]+) s'),
                         ('first uplink echoed after the LFN restart', r'after the LFN restart: first uplink echoed ([\d.]+) s')):
            print(f'  {what}: {rng([float(m[1]) for t in txt.values() for m in re.finditer(rx, t)])} s')
        uc = collections.defaultdict(lambda: [0, 0])
        for t in txt.values():
            for m in re.finditer(r'(BR -> LFN|LFN -> BR) ([^:]*): (\d+) requests, (\d+) received', t):
                uc[m[1]][0] += int(m[3]); uc[m[1]][1] += int(m[4])
        dl = [float(x[3]) for x in lat if x[3] != 'lost']
        print('  ' + '; '.join(f'{d} {b}/{a}' for d, (a, b) in uc.items()) + f' (outside the recovery); downlink latency {rng(dl, "{:.2f}")} s')
    elif g.startswith('mcast'):
        joins = [float(m[1]) for t in txt.values() for m in re.finditer(r'operational at ([\d.]+) s', t)]
        got = [(int(m[1]), int(m[2])) for t in txt.values() for m in re.finditer(r'\(subscribed\): (\d+) of (\d+) group requests', t)]
        dup = sum(int(m[1]) for t in txt.values() for m in re.finditer(r'received, (\d+) duplicates', t))
        print(f'  LFNs operational {rng(joins)} s; subscribed LFNs {sum(a for a, b in got)} of {sum(b for a, b in got)} '
              f'group requests, {dup} duplicates')
    elif g.startswith('better'):
        joins = [float(m[1]) for t in txt.values() for m in re.finditer(r'LFN joined router 1 at ([\d.]+) s', t)]
        moved = [float(m[1]) for t in txt.values() for m in re.finditer(r'moved to router 2 at [\d.]+ s, ([\d.]+) s after it appeared', t)]
        rel = [float(m[1]) for t in txt.values() for m in re.finditer(r'router 1 released ([\d.]+) s after the move', t)]
        uc = collections.defaultdict(lambda: [0, 0])
        for t in txt.values():
            for m in re.finditer(r'(BR -> LFN|LFN -> BR): (\d+) requests, (\d+) received', t):
                uc[m[1]][0] += int(m[2]); uc[m[1]][1] += int(m[3])
        print(f'  joined {rng(joins)} s; moved {rng(moved)} s after the better parent appeared; old parent released '
              f'{rng(rel)} s after; ' + '; '.join(f'{d} {b}/{a}' for d, (a, b) in uc.items()))
    print('  results: ' + ' '.join(('PASS' if t.rstrip().endswith('PASSED') else 'FAIL') for t in txt.values()))
EOF
cat "$OUT/summary.txt"
[ "$NF" = 0 ] && [ "$NP" -gt 0 ]
