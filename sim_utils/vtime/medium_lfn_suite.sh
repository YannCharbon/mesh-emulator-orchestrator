#!/bin/bash
# EM4 gate item 5 (docs/MEDIUM.md §8): the LFN suite (lfn_suite.sh) with the
# central medium, the tests unchanged. Every test starts wssimserver from
# <bindir>, through run.sh or directly (test_lfn_mac.sh), and some set
# SIM_OPTS themselves (test_lfn_keyreq.sh): so the medium is chosen by a
# bindir of our own, <outdir>/bin, with links to <bindir>'s programs and a
# wssimserver wrapper that adds MEDIUM_ARGS (--medium=central) to every
# call. With MEDIUM_LOG=1, a run of run.sh also writes <run>/medium.log
# (the wrapper runs in the run's directory, which already holds
# simserver.log); keep the runs with KEEP=1 to read them (medium_stats.py).
#
# The suite's two unit tests are left out (they copy and run the server
# binary with fake nodes: test_conductor.py, test_power.py; gate item 2
# covers the medium's own unit tests): ONLY defaults to every other test.
#
# usage: medium_lfn_suite.sh [quick|full] <bindir> <workdir(holds examples/)> <outdir>
# Environment: MEDIUM_ARGS ("--medium=central"; e.g. add
# "--medium-capture-db=10"), MEDIUM_LOG (0), LEGACY=1: also run the same
# suite with the plain <bindir> (legacy, into <outdir>/legacy) and print the
# runs whose result differs between the two; JOBS, ONLY, KEEP as lfn_suite.sh.
# Output: <outdir>/central/results.txt (and legacy/results.txt), one PASS or
# FAIL line per run; <outdir>/differences.txt with LEGACY=1.
# Wall time: as lfn_suite.sh (quick about 1.5 min, full about 7 min with 12
# jobs) times the central mode's slowdown; LEGACY=1 adds one legacy suite.
# Exit status 0 if every central run passed.
set -u
MODE=${1:-}; case $MODE in quick|full) shift ;; *) MODE=quick ;; esac
[ $# = 3 ] || { echo "usage: medium_lfn_suite.sh [quick|full] <bindir> <workdir> <outdir>" >&2; exit 2; }
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "medium_lfn_suite: $OUT is not empty" >&2; exit 1; }
[ -x "$BIN/wssimserver" ] || { echo "medium_lfn_suite: no $BIN/wssimserver" >&2; exit 2; }
export ONLY=${ONLY:-join|mesh|mcast|better|mc_full|revoke|lost|silen|parent_gone|scale|weak|reset|mac_|restart|profiles|keyreq|radio_off}
MEDIUM_ARGS=${MEDIUM_ARGS---medium=central}
T0=$SECONDS

# The bindir with the wrapper
mkdir "$OUT/bin"
for f in "$BIN"/*; do
    [ "$(basename "$f")" = wssimserver ] || ln -s "$f" "$OUT/bin/"
done
cat > "$OUT/bin/wssimserver" <<EOF
#!/bin/bash
# wssimserver with the central medium (medium_lfn_suite.sh)
args=($MEDIUM_ARGS)
[ "${MEDIUM_LOG:-0}" = 1 ] && [ -f simserver.log ] && args+=(--medium-log="\$PWD/medium.log")
exec "$BIN/wssimserver" "\${args[@]}" "\$@"
EOF
chmod +x "$OUT/bin/wssimserver"
echo "medium_lfn_suite: $MODE, wssimserver $MEDIUM_ARGS, tests matching ONLY=$ONLY"

"$HERE/lfn_suite.sh" "$MODE" "$OUT/bin" "$WORK" "$OUT/central"
RC=$?
if [ "${LEGACY:-0}" = 1 ]; then
    echo "medium_lfn_suite: the same suite in legacy mode"
    "$HERE/lfn_suite.sh" "$MODE" "$BIN" "$WORK" "$OUT/legacy"
    # runs whose result differs (the run times aside)
    join -j2 -a1 -a2 -e MISSING -o 0,1.1,2.1 \
        <(awk '{print $1, $2}' "$OUT/legacy/results.txt" | sort -k2) \
        <(awk '{print $1, $2}' "$OUT/central/results.txt" | sort -k2) |
        awk '$2 != $3 { print $1 ": legacy " $2 ", central " $3 }' > "$OUT/differences.txt"
    echo "medium_lfn_suite: $(wc -l < "$OUT/differences.txt") runs differ between legacy and central ($OUT/differences.txt)"
    cat "$OUT/differences.txt"
fi
echo "medium_lfn_suite: $((SECONDS - T0)) s wall time"
exit $RC
