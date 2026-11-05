#!/bin/bash
# EM4 gate item 4 (docs/MEDIUM.md §8): where the central medium matters. Many
# children of one parent: a border router and ROUTERS (40) FAN 1.0 routers,
# all in its range (gen_topology.py --shape star, topology seed TOPO_SEED 1,
# border-router links -78 to -40 dBm), in two topologies:
#   hidden  the routers in HIDDEN_GROUPS (4) groups of consecutive IDs that do not
#           hear each other (hidden terminals at the border router)
#   clique  every router hears every other one (same border-router links):
#           the control, where CCA can see every other sender
# Test traffic: every router sends a UDP request to the border router every
# PERIOD (30) s from START (300) s, router i at START + (i - 1) % 10 s: each
# second, one router of each group sends (mutually hidden in "hidden"); the
# border router echoes each request. The routers have all joined by about
# 250 s in a legacy pilot (seed 1).
# SEEDS (1 2 3) x DUR (900) virtual seconds, in each mode of MODES ("legacy
# central") and each topology of TOPOS ("hidden clique"); central runs write
# <run>/medium.log. JOBS (12) at a time, no TUN device.
#
# What each central run must show (against the legacy run of the same seed
# and topology): interference, captured and busy outcomes at the border
# router, more interference in "hidden" than in "clique", CCA busy answers
# (more in "clique": its senders hear each other); then, reported (not
# judged): the requests received and echoed, the join times (routers ready,
# last ready), the medium's delivery rate per receiver and link.
# The statistics (medium_stats.py, traffic counted from START) go to
# <outdir>/stats_<topology>.txt, legacy against central.
#
# usage: medium_gate_star.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: ROUTERS (40), HIDDEN_GROUPS (4), TOPO_SEED (1), SEEDS ("1 2 3"),
# DUR (900), PERIOD (30), START (300), MODES, TOPOS, JOBS (12), TUN (none),
# SIM_OPTS (more wssimserver options, every run), PRE_EM4=1 (no --medium
# option in legacy mode: a build without the medium, to try this script).
# Wall time: a legacy run of 41 nodes and 900 s takes about 14 s (pilot);
# the 12 runs at once, about a minute if the central mode is up to 3 times
# slower.
set -u
[ $# = 3 ] || { echo "usage: medium_gate_star.sh <bindir> <workdir> <outdir>" >&2; exit 2; }
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
HERE=$(cd "$(dirname "$0")" && pwd)
ROUTERS=${ROUTERS:-40}; GROUPS_N=${HIDDEN_GROUPS:-4}; TOPO_SEED=${TOPO_SEED:-1}
SEEDS=${SEEDS:-1 2 3}; DUR=${DUR:-900}; PERIOD=${PERIOD:-30}; START=${START:-300}
MODES=${MODES:-legacy central}; TOPOS=${TOPOS:-hidden clique}; JOBS=${JOBS:-12}; TUN=${TUN:-none}
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "medium_gate_star: $OUT is not empty" >&2; exit 1; }
T0=$SECONDS

N=$((ROUTERS + 1))
for t in $TOPOS; do
    case $t in
    hidden) g=$GROUPS_N ;;
    clique) g=1 ;;
    *) echo "medium_gate_star: unknown topology $t (hidden, clique)" >&2; exit 2 ;;
    esac
    python3 "$HERE/gen_topology.py" $N --shape star --groups $g --seed "$TOPO_SEED" -o "$OUT/$t.topo" 2> "$OUT/$t.topo.txt" ||
        { echo "medium_gate_star: gen_topology.py failed" >&2; exit 1; }
    echo "$t: $(cat "$OUT/$t.topo.txt")"
done

one() {   # one <topology> <mode> <seed>
    local t=$1 m=$2 s=$3 opts d=$OUT/$1/$2
    case $m in
    legacy) opts=$([ "${PRE_EM4:-0}" = 1 ] || echo "--medium=legacy") ;;
    central) opts="--medium=central --medium-log=$d/seed$s/medium.log" ;;
    *) echo "FAIL $t $m seed $s: unknown mode"; return ;;
    esac
    mkdir -p "$d"
    # the routers' requests: router i starts at START + (i - 1) % 10 s
    local i env=()
    for (( i = 1; i <= ROUTERS; i++ )); do
        env+=("NODE_OPTS_$i=-o test_udp_start_s=$((START + (i - 1) % 10))")
    done
    if env "${env[@]}" WSSIM_SEED=$s TUN=$TUN TOPO="$OUT/$t.topo" SIM_OPTS="${SIM_OPTS:-} $opts" \
        BR_OPTS="-o test_udp_echo=true" \
        NODE_OPTS="-o test_udp_dst=fd12:3456::1615:16ff:fe17:1819 -o test_udp_period_s=$PERIOD" \
        "$HERE/run.sh" "$BIN" "$WORK" "$d/seed$s" "$DUR" > "$d/seed$s.run" 2>&1; then
        echo "done $t $m seed $s"
    else
        echo "FAIL $t $m seed $s: run failed ($d/seed$s.run)"
    fi
}
export -f one; export OUT BIN WORK DUR TUN HERE PRE_EM4 SIM_OPTS ROUTERS START PERIOD

JOBLIST=()
for t in $TOPOS; do for m in $MODES; do for s in $SEEDS; do JOBLIST+=("$t $m $s"); done; done; done
echo "medium_gate_star: ${#JOBLIST[@]} runs of $N nodes and $DUR s, $JOBS at a time, in $OUT"
printf '%s\n' "${JOBLIST[@]}" | xargs -P "$JOBS" -L1 bash -c 'one "$@"' _ | tee "$OUT/results.txt"
NF=$(grep -c '^FAIL' "$OUT/results.txt")

if [ "$NF" = 0 ]; then
    for t in $TOPOS; do
        ARGS=(); NAMES=
        for m in $MODES; do
            [ ${#ARGS[@]} -gt 0 ] && ARGS+=(--)
            for s in $SEEDS; do ARGS+=("$OUT/$t/$m/seed$s"); done
            NAMES+=${NAMES:+,}$m
        done
        echo; echo "##### $t"
        python3 "$HERE/medium_stats.py" --names="$NAMES" --traffic-from="$START" "${ARGS[@]}" | tee "$OUT/stats_$t.txt"
    done
fi
echo "medium_gate_star: $NF failed runs, $((SECONDS - T0)) s wall time; statistics in $OUT/stats_*.txt"
[ "$NF" = 0 ]
