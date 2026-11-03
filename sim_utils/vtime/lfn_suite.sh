#!/bin/bash
# The LFN test suite, in virtual time, many runs at once (TUN=none: no TUN
# device, no sudo). One line per test run, "PASS" or "FAIL", then a summary;
# exit status 1 if any run failed.
#
# usage: lfn_suite.sh [quick|full] <bindir> <workdir(holds examples/)> <outdir>
#   quick  one run of each test (seed 1; the profile runs 12 h): 49 runs,
#          about 70 s of wall time with 12 jobs on a 24-core PC
#   full   the documented counts, at most 6 runs per check (the root
#          CLAUDE.md sizing rule): 3 seeds per check, 6 for the checks that
#          vary most (join with traffic, better parent, restart), 2 for the
#          profile runs (48 h Balanced, 72 h Eco), one Eco run with a 4 h
#          listen interval over 72 h, test_lfn_keyreq.sh on its seed 3:
#          145 runs, about 6 minutes with 12 jobs
# Environment: JOBS (12: runs at once), ONLY (a regular expression: only
# the runs whose name matches), KEEP (1: keep the run directories; by
# default only each run's output, <outdir>/<name>.txt, is kept).
#
# Every run uses each node's clock drawn within +-20 ppm (--drift-random=20)
# unless its test sets otherwise. The per-test options are in the job list
# below; the tests are documented in README.md.
set -u
MODE=${1:-}; case $MODE in quick|full) shift ;; *) MODE=quick ;; esac
[ $# = 3 ] || { echo "usage: lfn_suite.sh [quick|full] <bindir> <workdir> <outdir>" >&2; exit 2; }
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "lfn_suite: $OUT is not empty" >&2; exit 1; }
JOBS=${JOBS:-12}
V=$HERE
export TUN=none SIM_OPTS=--drift-random=20

# Job list: "<name>|<environment>|<script and arguments, <out> for the run
# directory>". seeds <n> gives seeds 1..n in full mode, 1 in quick mode.
seeds() { [ $MODE = full ] && seq 1 "$1" || echo 1; }
LGTK_SHORT="-o@lfn_lgtk_lifetime_s=900@-o@lfn_lgtk_new_install_required=50@-o@lfn_lgtk_new_activation_time=3"
J="$V/test_lfn_join.sh $BIN $WORK <out>"
{
for s in $(seeds 6); do echo "join_traffic_s$s|WSSIM_SEED=$s DUR=1800|$J"; done
for s in $(seeds 3); do
    echo "join_idle_s$s|WSSIM_SEED=$s DUR=3600 BR_PERIOD=0 LFN_PERIOD=0|$J"
    echo "join_parentloss_s$s|WSSIM_SEED=$s DUR=1500 BR_PERIOD=0 LFN_PERIOD=0 SILENT_AT=600 SILENT_FOR=800|$J"
    echo "join_lgtk_rotate_s$s|WSSIM_SEED=$s DUR=1500 LGTK_ROTATE_AT=600|$J"
    echo "join_lgtk_short_s$s|WSSIM_SEED=$s DUR=2400 BR_EXTRA=$LGTK_SHORT|$J"
    echo "join_phy1_s$s|WSSIM_SEED=$s DUR=1800 BR_EXTRA=-o@mode=1a@-o@chan_plan_id=32 LFN_EXTRA=-o@mode=1a@-o@chan_plan_id=32|$J"
    for m in 1 2 3; do echo "join_mesh${m}_s$s|WSSIM_SEED=$s MESH=$m DUR=1800|$J"; done
    echo "join_mesh1_rotate_s$s|WSSIM_SEED=$s MESH=1 DUR=1800 LGTK_ROTATE_AT=600|$J"
    echo "join_mesh3_change_s$s|WSSIM_SEED=$s MESH=3 DUR=1800 SILENT_AT=600 SILENT_FOR=1000|$J"
    echo "mesh_s$s|WSSIM_SEED=$s DUR=1800|$V/test_lfn_mesh.sh $BIN $WORK <out>"
    echo "mcast_s$s|WSSIM_SEED=$s|$V/test_lfn_mcast.sh $BIN $WORK <out>"
    echo "mcast_balanced_s$s|WSSIM_SEED=$s PROF=balanced|$V/test_lfn_mcast.sh $BIN $WORK <out>"
    echo "mcast_eco_s$s|WSSIM_SEED=$s PROF=eco|$V/test_lfn_mcast.sh $BIN $WORK <out>"
    echo "better_stay_s$s|WSSIM_SEED=$s EXPECT=stay|$V/test_lfn_better.sh $BIN $WORK <out>"
    echo "better_stay_unusable_s$s|WSSIM_SEED=$s EXPECT=stay ROUTER2_EXTRA=-o@test_lfn_lpa_unusable=1|$V/test_lfn_better.sh $BIN $WORK <out>"
    echo "mc_full_s$s|WSSIM_SEED=$s|$V/test_lfn_mc_full.sh $BIN $WORK <out>"
    echo "revoke_s$s|WSSIM_SEED=$s|$V/test_lfn_revoke.sh $BIN $WORK <out>"
    echo "lost_release_stale_s$s|WSSIM_SEED=$s MODE=release LOST=4 REG_MIN=30|$V/test_lfn_lost.sh $BIN $WORK <out>"
    echo "silence_s$s|WSSIM_SEED=$s|$V/test_lfn_silence.sh $BIN $WORK <out>"
    echo "silent_s$s|WSSIM_SEED=$s|$V/test_lfn_silent.sh $BIN $WORK <out>"
    for m in traffic idle uplink; do echo "parent_gone_${m}_s$s|WSSIM_SEED=$s MODE=$m|$V/test_lfn_parent_gone.sh $BIN $WORK <out>"; done
    echo "scale_star_s$s|WSSIM_SEED=$s SCALE=star|$V/test_lfn_scale.sh $BIN $WORK <out>"
    echo "scale_mesh_s$s|WSSIM_SEED=$s SCALE=mesh|$V/test_lfn_scale.sh $BIN $WORK <out>"
    echo "weak_sweep_s$s|WSSIM_SEED=$s MODE=sweep|$V/test_lfn_weak.sh $BIN $WORK <out>"
    echo "weak_fade_s$s|WSSIM_SEED=$s MODE=fade|$V/test_lfn_weak.sh $BIN $WORK <out>"
    for r in router lfn; do echo "reset_${r}_s$s|WSSIM_SEED=$s RESET=$r|$V/test_lfn_reset.sh $BIN $WORK <out>"; done
    echo "mac_s$s|WSSIM_SEED=$s|$V/test_lfn_mac.sh $BIN $WORK <out>"
    for m in na na2; do
        echo "lost_${m}_s$s|WSSIM_SEED=$s MODE=$m|$V/test_lfn_lost.sh $BIN $WORK <out>"
        for p in balanced eco; do echo "lost_${m}_${p}_s$s|WSSIM_SEED=$s MODE=$m PROF=$p|$V/test_lfn_lost.sh $BIN $WORK <out>"; done
    done
    echo "lost_release_s$s|WSSIM_SEED=$s MODE=release LOST=2|$V/test_lfn_lost.sh $BIN $WORK <out>"
    echo "lost_release_all_s$s|WSSIM_SEED=$s MODE=release LOST=4|$V/test_lfn_lost.sh $BIN $WORK <out>"
done
for s in $(seeds 6); do
    echo "better_move_s$s|WSSIM_SEED=$s EXPECT=move|$V/test_lfn_better.sh $BIN $WORK <out>"
    echo "restart_s$s|WSSIM_SEED=$s|$V/test_lfn_restart.sh $BIN $WORK <out>"
done
# Profiles over long runs: 12 h in quick mode, 48 h (Balanced) and 72 h (Eco) in full
for s in $(seeds 2); do
    for p in balanced eco; do
        d=43200; [ $MODE = full ] && { [ $p = eco ] && d=259200 || d=172800; }
        echo "profiles_${p}_s$s|WSSIM_SEED=$s DUR=$d STEADY_FROM=$((d / 4))|$V/test_lfn_profiles.sh $BIN $WORK <out> $p"
    done
done
# A router's key request answered by a full EAP-TLS: the timing is set for
# seed 3 (the test's default), so one run of each case
for c in race txfail; do echo "keyreq_${c}_s3|WSSIM_SEED=3 CASE=$c|$V/test_lfn_keyreq.sh $BIN $WORK <out>"; done
# A 4 h listen interval over 72 h (Eco, 10 h sync period; LFN_STACK.md §12), full mode only
[ $MODE = full ] && echo "profiles_eco_4h_s1|WSSIM_SEED=1 BR_PERIOD=18500 LFN_PERIOD=27000 DUR=259200 STEADY_FROM=86400 LFN_EXTRA=-o@lfn_listen_interval_ms=14400000@-o@lfn_listen_interval_max_ms=16777215|$V/test_lfn_profiles.sh $BIN $WORK <out> eco"
echo "radio_off|WSSIM_SEED=1|$V/test_radio_off.sh $BIN $WORK <out>"
echo "conductor||python3 $V/test_conductor.py $BIN/wssimserver --sock /tmp/lfn_suite_$$.sock"
echo "power||python3 $V/test_power.py $BIN/wssimserver"
} | { [ -n "${ONLY:-}" ] && grep -E "^[^|]*(${ONLY})" || cat; } > "$OUT/jobs.txt"

run() {   # run <job line>
    local name=${1%%|*} rest=${1#*|}; local envs=${rest%%|*} cmd=${rest#*|}
    local t0=$SECONDS rc
    cmd=${cmd//<out>/$OUT/$name}
    eval "env ${envs//@/\\ } $cmd" > "$OUT/$name.txt" 2>&1
    rc=$?
    [ "${KEEP:-0}" = 1 ] || rm -rf "${OUT:?}/$name" "$OUT/$name".run "$OUT/$name".*.run
    if [ $rc = 0 ]; then echo "PASS $name ($((SECONDS - t0)) s)"
    else echo "FAIL $name ($((SECONDS - t0)) s): $(grep -m1 -E '^FAIL|run failed|Error|^test_lfn_[a-z_]+: no ' "$OUT/$name.txt" | cut -c1-160)"; fi
}
export -f run; export OUT KEEP
T0=$SECONDS
echo "lfn_suite: $MODE, $(wc -l < "$OUT/jobs.txt") runs, $JOBS at a time, outputs in $OUT"
tr '\n' '\0' < "$OUT/jobs.txt" | xargs -0 -P "$JOBS" -I{} bash -c 'run "$1"' _ {} | tee "$OUT/results.txt"
NP=$(grep -c '^PASS' "$OUT/results.txt"); NF=$(grep -c '^FAIL' "$OUT/results.txt")
echo "lfn_suite: $NP passed, $NF failed, $((SECONDS - T0)) s wall time"
[ "$NF" = 0 ] && [ "$NP" -gt 0 ]
