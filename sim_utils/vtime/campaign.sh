#!/bin/bash
# Several virtual-time runs at once, one per TUN device (tun_setup.sh).
#     campaign.sh <bindir> <workdir> <outdir> <seconds> <seed>...
# Each seed gives <outdir>/seed<N> (run.sh). JOBS (default: the number of
# tunvt devices) runs go at the same time; TOPO, SETARCH and the other run.sh
# options are passed through. Virtual time does not depend on the CPU load, so
# a run gives the same result alone or in a campaign.
#
# Memory check, before starting: a run needs about MB_PER_NODE (5) MB per node,
# plus 20%. If JOBS such runs do not fit in the available memory minus
# RESERVE_GB (4), JOBS is lowered to what fits, and the campaign says so.
# FORCE=1 keeps JOBS as given. The check is made once, at the start.
# Exit status: the number of runs that failed.
set -u
BIN=$1; WORK=$2; OUT=$3; DUR=$4; shift 4
HERE=$(cd "$(dirname "$0")" && pwd)
mapfile -t TUNS < <(ip -o link show | grep -o 'tunvt[0-9]*' | sort -u -V)
JOBS=${JOBS:-${#TUNS[@]}}
(( ${#TUNS[@]} )) || { echo "no tunvt devices: run 'sudo $HERE/tun_setup.sh' first" >&2; exit 1; }
(( JOBS <= ${#TUNS[@]} )) || { echo "JOBS=$JOBS but only ${#TUNS[@]} tunvt devices" >&2; exit 1; }

# Nodes per run, counted as run.sh does: the highest ID in the topology, plus
# the border router (node 0). dag_simple has 15.
if [ -n "${TOPO:-}" ]; then
    NODES=$(grep -v '^\s*\(#\|$\)' "$TOPO" | cut -d: -f1 | tr ',-' '\n\n' | sort -n | tail -1)
    NODES=$((NODES + 1))
else
    NODES=15
fi
MB_PER_NODE=${MB_PER_NODE:-5}
RESERVE_GB=${RESERVE_GB:-4}
run_mb=$(( NODES * MB_PER_NODE * 12 / 10 ))
avail_mb=$(( $(awk '/^MemAvailable:/ { print $2 }' /proc/meminfo) / 1024 - RESERVE_GB * 1024 ))
fit=$(( avail_mb / run_mb ))
need_gb=$(( (JOBS * run_mb + 1023) / 1024 ))
if (( fit < 1 )); then
    echo "campaign: one run of $NODES nodes needs ~$run_mb MB, only $avail_mb MB available (after ${RESERVE_GB} GB reserve)" >&2
    [ "${FORCE:-0}" = 1 ] || { echo "campaign: not started (FORCE=1 to start anyway)" >&2; exit 1; }
elif (( JOBS > fit )); then
    if [ "${FORCE:-0}" = 1 ]; then
        echo "campaign: JOBS=$JOBS needs ~$need_gb GB, $((avail_mb / 1024)) GB usable: keeping $JOBS (FORCE=1)" >&2
    else
        echo "campaign: JOBS=$JOBS needs ~$need_gb GB, $((avail_mb / 1024)) GB usable: running $fit at a time (FORCE=1 to keep $JOBS)" >&2
        JOBS=$fit
    fi
fi
echo "campaign: $# runs of $NODES nodes, $JOBS at a time, ~$run_mb MB each"
mkdir -p "$OUT/out"

declare -A busy        # pid -> tun index
free=()
for (( i = 0; i < JOBS; i++ )); do free+=("$i"); done
failed=0
start=$(date +%s.%N)

reap() {
    local pid st
    wait -n -p pid; st=$?
    [ "$st" -ne 0 ] && { echo "run with pid $pid failed ($st)" >&2; failed=$((failed + 1)); }
    free+=("${busy[$pid]}"); unset "busy[$pid]"
}

for seed in "$@"; do
    (( ${#free[@]} )) || reap
    t=${free[0]}; free=("${free[@]:1}")
    WSSIM_SEED=$seed TUN=${TUNS[$t]} "$HERE/run.sh" "$BIN" "$WORK" "$OUT/seed$seed" "$DUR" \
        > "$OUT/out/seed$seed.out" 2>&1 &
    busy[$!]=$t
done
while (( ${#busy[@]} )); do reap; done
echo "campaign: $# runs, $JOBS at a time, $(echo "$(date +%s.%N) - $start" | bc) s, $failed failed"
exit "$failed"
