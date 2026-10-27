#!/bin/bash
# Several virtual-time runs at once, one per TUN device (tun_setup.sh).
#     campaign.sh <bindir> <workdir> <outdir> <seconds> <seed>...
# Each seed gives <outdir>/seed<N> (run.sh). JOBS (default: the number of
# tunvt devices) runs go at the same time; TOPO, SETARCH and the other run.sh
# options are passed through. Virtual time does not depend on the CPU load, so
# a run gives the same result alone or in a campaign.
# Exit status: the number of runs that failed.
set -u
BIN=$1; WORK=$2; OUT=$3; DUR=$4; shift 4
HERE=$(cd "$(dirname "$0")" && pwd)
mapfile -t TUNS < <(ip -o link show | grep -o 'tunvt[0-9]*' | sort -u -V)
JOBS=${JOBS:-${#TUNS[@]}}
(( ${#TUNS[@]} )) || { echo "no tunvt devices: run 'sudo $HERE/tun_setup.sh' first" >&2; exit 1; }
(( JOBS <= ${#TUNS[@]} )) || { echo "JOBS=$JOBS but only ${#TUNS[@]} tunvt devices" >&2; exit 1; }
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
