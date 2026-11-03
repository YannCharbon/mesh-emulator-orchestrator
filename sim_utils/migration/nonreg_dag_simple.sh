#!/bin/bash
# FAN 1.0 non-regression in virtual time: dag_simple, SEEDS (1-10) x DUR
# (300) virtual seconds, each run's logs compared byte for byte with the
# reference run of the same seed (sim_utils/vtime/compare_logs.py
# --mask-pointers --mask-source-lines: printed heap addresses and the
# programs' own function():line trace prefixes masked). Runs at once
# (JOBS, 10), no TUN device (TUN=none) unless TUN is given. One line per
# seed, exit status 1 on any difference or failed run.
#
# usage: nonreg_dag_simple.sh <bindir> <workdir(holds examples/)> <reference> [outdir]
#   bindir     a virtual-time build (wssimserver, wsbrd-inproc, wsnode-inproc)
#   reference  a directory with seed1 ... seedN, e.g. the permanent reference
#              runs (sim_utils/reference-runs.md): dag_simple_b1b9cc5
#   outdir     where the runs go (default: a new temporary directory, deleted
#              at the end unless KEEP=1)
# Environment: SEEDS ("1 2 ... 10"), DUR (300), JOBS (10), TUN (none), KEEP.
set -u
[ $# -ge 3 ] || { echo "usage: nonreg_dag_simple.sh <bindir> <workdir> <reference> [outdir]" >&2; exit 2; }
BIN=$(realpath "$1"); WORK=$(realpath "$2"); REF=$(realpath "$3")
HERE=$(cd "$(dirname "$0")/.." && pwd)
SEEDS=${SEEDS:-$(seq 1 10)}; DUR=${DUR:-300}; JOBS=${JOBS:-10}; TUN=${TUN:-none}
if [ $# -ge 4 ]; then OUT=$4; mkdir -p "$OUT"; OUT=$(realpath "$OUT"); TMP=0
else OUT=$(mktemp -d /tmp/nonreg_dag_simple.XXXXXX); TMP=1; fi
for s in $SEEDS; do
    [ -d "$REF/seed$s" ] || { echo "nonreg_dag_simple: no $REF/seed$s" >&2; exit 2; }
    [ ! -e "$OUT/seed$s" ] || { echo "nonreg_dag_simple: $OUT/seed$s exists" >&2; exit 2; }
done
T0=$SECONDS
one() {
    local s=$1
    if ! WSSIM_SEED=$s TUN=$TUN SETARCH=1 "$HERE/vtime/run.sh" "$BIN" "$WORK" "$OUT/seed$s" "$DUR" > "$OUT/seed$s.run" 2>&1; then
        echo "FAIL seed $s: run failed ($OUT/seed$s.run)"; return
    fi
    local r
    r=$(python3 "$HERE/vtime/compare_logs.py" --mask-pointers --mask-source-lines "$REF/seed$s" "$OUT/seed$s" 2>&1)
    if [ $? = 0 ]; then echo "PASS seed $s: $(tail -1 <<< "$r")"
    else echo "FAIL seed $s: $(tail -3 <<< "$r" | tr '\n' ' ' | cut -c1-300)"; fi
}
export -f one; export OUT BIN WORK DUR TUN HERE REF
printf '%s\n' $SEEDS | xargs -P "$JOBS" -I{} bash -c 'one {}' | sort -k3 -n | tee "$OUT/results.txt"
NF=$(grep -c '^FAIL' "$OUT/results.txt"); NP=$(grep -c '^PASS' "$OUT/results.txt")
echo "nonreg_dag_simple: $NP identical, $NF different or failed ($((SECONDS - T0)) s wall time; reference $REF)"
[ "${KEEP:-0}" = 1 ] || [ $TMP = 0 ] || rm -rf "$OUT"
[ "$NF" = 0 ] && [ "$NP" -gt 0 ]
