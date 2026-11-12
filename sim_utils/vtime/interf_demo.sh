#!/bin/bash
# Network-level demonstration of an external interferer (docs/MEDIUM.md
# §13.6): the hidden star of medium_gate_star.sh (a border router and 40
# routers in 4 groups that do not hear each other, UDP requests to the
# border router every 30 s from 300 s), central medium, SEEDS (1 2 3) x DUR
# (900) virtual seconds, without and with INTERFERER. The two groups are
# compared by medium_stats.py (the first is the baseline); then, from the
# medium logs, the frames heard at the border router during an on-interval
# of the interferer (eighth column) and how they ended.
#
# usage: interf_demo.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: SEEDS, DUR, JOBS (6), INTERFERER (default: a noise-like
# device on channels 0-9 of the 35, on 200 ms every second, -70 dBm at
# every node), more as medium_gate_star.sh.
# Wall time: about 15 s per run, 3 runs at once: under a minute per group.
set -u
[ $# = 3 ] || { echo "usage: interf_demo.sh <bindir> <workdir> <outdir>" >&2; exit 2; }
BIN=$1; WORK=$2; OUT=$3
HERE=$(cd "$(dirname "$0")" && pwd)
INTERFERER=${INTERFERER:-name=dc,chan=0-9,level=-70,pattern=periodic:1s:200ms}
export SEEDS=${SEEDS:-1 2 3} DUR=${DUR:-900} JOBS=${JOBS:-6} MODES=central TOPOS=hidden
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "interf_demo: $OUT is not empty" >&2; exit 1; }
T0=$SECONDS
"$HERE/medium_gate_star.sh" "$BIN" "$WORK" "$OUT/without" > "$OUT/without.txt" 2>&1 ||
    { echo "interf_demo: baseline runs failed ($OUT/without.txt)" >&2; exit 1; }
SIM_OPTS="${SIM_OPTS:-} --interferer=$INTERFERER" "$HERE/medium_gate_star.sh" "$BIN" "$WORK" "$OUT/with" \
    > "$OUT/with.txt" 2>&1 || { echo "interf_demo: runs with the interferer failed ($OUT/with.txt)" >&2; exit 1; }
ARGS=()
for s in $SEEDS; do ARGS+=("$OUT/without/hidden/central/seed$s"); done
ARGS+=(--)
for s in $SEEDS; do ARGS+=("$OUT/with/hidden/central/seed$s"); done
echo "interferer: $INTERFERER"
python3 "$HERE/medium_stats.py" --names=without,with --traffic-from="${START:-300}" --receivers 3 --links 5 "${ARGS[@]}"
echo
echo "frames heard at the border router (node 0) while listening, with the interferer on during the frame (per run):"
for s in $SEEDS; do
    awk -v s="$s" '$3 == 0 && $5 != "not_listening" { n++; if ($8 != "-") { e++; c[$5]++ } }
        END { printf "  seed %s: %d heard, %d with the interferer on:", s, n, e;
              for (k in c) printf " %s %d", k, c[k]; printf "\n" }' "$OUT/with/hidden/central/seed$s/medium.log"
done
grep -h "medium: interferer\|external interferer on at" "$OUT"/with/hidden/central/seed*/simserver.log | sed 's/^/  /'
echo "interf_demo: $((SECONDS - T0)) s wall time"
