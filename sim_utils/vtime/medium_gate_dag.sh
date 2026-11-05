#!/bin/bash
# EM4 gate item 3 (docs/MEDIUM.md §8): the central medium against legacy
# under light traffic. dag_simple (15 nodes, FAN 1.0, no test traffic), SEEDS
# (1-6) x DUR (600) virtual seconds in each mode of MODES ("legacy central"),
# wssimserver --medium=<mode> (central: also --medium-log=<run>/medium.log),
# no TUN device (TUN=none), JOBS (12) runs at once. Then medium_stats.py
# compares the modes: join milestones, routers ready (count, median and last
# ready time) of each central run against the legacy runs' range, and the
# central runs' medium outcomes.
#
# What it must show: every central run inside the legacy spread for the
# routers ready and the join times (MEDIUM.md §8 item 3). 600 s, not the
# reference runs' 300 s: dag_simple's deepest routers join after 250 to
# 300 s, so a 300 s run does not show every router ready.
#
# usage: medium_gate_dag.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: SEEDS ("1 2 3 4 5 6"), DUR (600), MODES ("legacy central"),
# JOBS (12), TUN (none), SIM_OPTS (more wssimserver options, both modes),
# REUSE_LEGACY=<dir>: take the legacy runs from <dir>/seed<N> (a previous
# run of this script's <outdir>/legacy) instead of running them,
# PRE_EM4=1: no --medium option in legacy mode (a build without the medium,
# to try this script).
# Wall time: about 8 s per legacy run (0.013 wall s per simulated second for
# dag_simple); all 12 runs at once: well under a minute, unless the central
# mode is much slower.
set -u
[ $# = 3 ] || { echo "usage: medium_gate_dag.sh <bindir> <workdir> <outdir>" >&2; exit 2; }
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
HERE=$(cd "$(dirname "$0")" && pwd)
SEEDS=${SEEDS:-1 2 3 4 5 6}; DUR=${DUR:-600}; MODES=${MODES:-legacy central}; JOBS=${JOBS:-12}
TUN=${TUN:-none}
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "medium_gate_dag: $OUT is not empty" >&2; exit 1; }
T0=$SECONDS

one() {   # one <mode> <seed>
    local m=$1 s=$2 opts
    case $m in
    legacy) opts=$([ "${PRE_EM4:-0}" = 1 ] || echo "--medium=legacy") ;;
    central) opts="--medium=central --medium-log=$OUT/$m/seed$s/medium.log" ;;
    *) echo "FAIL $m seed $s: unknown mode"; return ;;
    esac
    mkdir -p "$OUT/$m"
    if WSSIM_SEED=$s TUN=$TUN SIM_OPTS="${SIM_OPTS:-} $opts" \
        "$HERE/run.sh" "$BIN" "$WORK" "$OUT/$m/seed$s" "$DUR" > "$OUT/$m/seed$s.run" 2>&1; then
        echo "done $m seed $s"
    else
        echo "FAIL $m seed $s: run failed ($OUT/$m/seed$s.run)"
    fi
}
export -f one; export OUT BIN WORK DUR TUN HERE PRE_EM4 SIM_OPTS

JOBLIST=()
for m in $MODES; do
    if [ "$m" = legacy ] && [ -n "${REUSE_LEGACY:-}" ]; then
        for s in $SEEDS; do
            [ -d "$REUSE_LEGACY/seed$s" ] || { echo "medium_gate_dag: no $REUSE_LEGACY/seed$s" >&2; exit 2; }
            grep -q "vtime_end=$DUR " "$REUSE_LEGACY/seed$s/run_info.txt" ||
                { echo "medium_gate_dag: $REUSE_LEGACY/seed$s is not a $DUR s run" >&2; exit 2; }
        done
        ln -s "$(realpath "$REUSE_LEGACY")" "$OUT/legacy"
        continue
    fi
    for s in $SEEDS; do JOBLIST+=("$m $s"); done
done
echo "medium_gate_dag: ${#JOBLIST[@]} runs of $DUR s, $JOBS at a time, in $OUT"
: > "$OUT/results.txt"
[ ${#JOBLIST[@]} -gt 0 ] && printf '%s\n' "${JOBLIST[@]}" | xargs -P "$JOBS" -L1 bash -c 'one "$@"' _ | tee "$OUT/results.txt"
NF=$(grep -c '^FAIL' "$OUT/results.txt")

ARGS=(); NAMES=
for m in $MODES; do
    [ ${#ARGS[@]} -gt 0 ] && ARGS+=(--)
    for s in $SEEDS; do ARGS+=("$OUT/$m/seed$s"); done
    NAMES+=${NAMES:+,}$m
done
if [ "$NF" = 0 ]; then
    python3 "$HERE/medium_stats.py" --names="$NAMES" "${ARGS[@]}" | tee "$OUT/stats.txt"
fi
echo "medium_gate_dag: $NF failed runs, $((SECONDS - T0)) s wall time; statistics in $OUT/stats.txt"
[ "$NF" = 0 ]
