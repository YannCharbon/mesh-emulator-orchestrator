#!/bin/bash
# Routers that collide at the border router must not retry in step (stack
# GS12, its docs/MAC_RETRY.md): a border router and two FAN 1.0 routers that
# hear it at -55 dBm but not each other (hidden terminals), central medium.
# Each router sends a UDP request to the border router every PERIOD (5) s
# from 200 s, both at the same instant, so their first attempts collide;
# the border router echoes them. SEEDS (1-6) x DUR (900) virtual seconds,
# all at once, no TUN device.
#
# Checks, over all runs (retry_lockstep.py): no request lost, and at most
# MAX_REPEAT (0.5) of the collisions at the border router repeat one less
# than 200 ms before (a retry colliding again). Before the stack fix
# (8c5677e, or a build with NANOSTACK_MAC_RETRY_SUN_BACKOFF=OFF): 74 of 1608
# requests lost, 0.74 repeats; after: none lost, 0.32. Prints the delay
# (request to its reception at the border router) and the longest chain.
#
# usage: test_retry_lockstep.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: SEEDS, DUR, PERIOD, MAX_REPEAT, SIM_OPTS, KEEP (1: keep the
# run directories). About 3 s of wall time.
set -u
[ $# = 3 ] || { echo "usage: test_retry_lockstep.sh <bindir> <workdir> <outdir>" >&2; exit 2; }
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
HERE=$(cd "$(dirname "$0")" && pwd)
SEEDS=${SEEDS:-1 2 3 4 5 6}; DUR=${DUR:-900}; PERIOD=${PERIOD:-5}; MAX_REPEAT=${MAX_REPEAT:-0.5}
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "test_retry_lockstep: $OUT is not empty" >&2; exit 1; }
printf '0,1:-55\n0,2:-55\n' > "$OUT/hidden2.topo"
T0=$SECONDS
one() {
    local s=$1 d=$OUT/seed$1
    mkdir -p "$d"
    if WSSIM_SEED=$s TUN=none TOPO="$OUT/hidden2.topo" SIM_OPTS="--medium-log=$d/medium.log ${SIM_OPTS:-}" \
        BR_OPTS="-o test_udp_echo=true" \
        NODE_OPTS="-o test_udp_dst=fd12:3456::1615:16ff:fe17:1819 -o test_udp_period_s=$PERIOD -o test_udp_start_s=200" \
        "$HERE/run.sh" "$BIN" "$WORK" "$d" "$DUR" > "$d.run" 2>&1; then
        echo "done seed $s"
    else
        echo "FAIL seed $s: run failed ($d.run)"
    fi
}
export -f one; export OUT BIN WORK DUR PERIOD HERE SIM_OPTS
printf '%s\n' $SEEDS | xargs -P 12 -I{} bash -c 'one {}' > "$OUT/results.txt"
if grep -q '^FAIL' "$OUT/results.txt"; then
    grep '^FAIL' "$OUT/results.txt"; echo "test_retry_lockstep: FAILED (a run failed)"; exit 1
fi
RUNS=(); for s in $SEEDS; do RUNS+=("$OUT/seed$s"); done
python3 "$HERE/retry_lockstep.py" --names=runs "${RUNS[@]}" | tee "$OUT/stats.txt"
read -r SENT LOST EV REP <<< "$(awk 'NR == 2 { print $3, $4, $9, $10 }' "$OUT/stats.txt")"
OK=1
if [ "$LOST" = 0 ]; then echo "ok   no request lost ($SENT sent)"
else echo "FAIL $LOST of $SENT requests lost"; OK=0; fi
if awk -v r="$REP" -v e="$EV" -v m="$MAX_REPEAT" 'BEGIN { exit !(e > 0 && r / e <= m) }'; then
    echo "ok   $REP of $EV collisions at the border router repeat one (at most $MAX_REPEAT)"
else echo "FAIL $REP of $EV collisions at the border router repeat one (more than $MAX_REPEAT)"; OK=0; fi
[ "${KEEP:-0}" = 1 ] || rm -rf "${RUNS[@]}"
if [ $OK = 1 ]; then echo "test_retry_lockstep: PASSED ($((SECONDS - T0)) s wall time)"; exit 0; fi
echo "test_retry_lockstep: FAILED ($((SECONDS - T0)) s wall time)"; exit 1
