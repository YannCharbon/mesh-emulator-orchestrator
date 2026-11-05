#!/bin/bash
# The LFN suite (lfn_suite.sh) with other wssimserver options on every call,
# by default in legacy mode (--medium=legacy). lfn_suite.sh alone runs the
# central medium, the default with --vtime (docs/MEDIUM.md §1); this script
# is the one command for the legacy medium:
#
#   lfn_suite.sh full build-vt $(pwd) runs/lfn            # central
#   medium_lfn_suite.sh full build-vt $(pwd) runs/lfn_l   # legacy
#
# Every test starts wssimserver from <bindir>, through run.sh or directly
# (test_lfn_mac.sh), and some set SIM_OPTS themselves (test_lfn_keyreq.sh):
# so the options reach them through a bindir of our own, <outdir>/bin, with
# links to <bindir>'s programs and a wssimserver wrapper that puts
# MEDIUM_ARGS before the call's own options.
#
# The suite's unit tests are left out: test_conductor.py and test_power.py
# copy and run the server with options of their own, test_medium_radio.sh
# is about the central medium: ONLY defaults to every other test.
#
# usage: medium_lfn_suite.sh [quick|full] <bindir> <workdir(holds examples/)> <outdir>
# Environment: MEDIUM_ARGS ("--medium=legacy"; e.g. "--medium-capture-db=10"
# for the central medium with another capture margin), MEDIUM_LOG (1: with
# the central medium, a run of run.sh also writes <run>/medium.log; keep the
# runs with KEEP=1 to read them with medium_stats.py), COMPARE=1: also run
# the plain suite (the central medium, into <outdir>/default) and print the
# runs whose result differs between the two; JOBS, ONLY, KEEP as
# lfn_suite.sh.
# Output: <outdir>/args/results.txt (and default/results.txt), one PASS or
# FAIL line per run; <outdir>/differences.txt with COMPARE=1.
# Wall time: as lfn_suite.sh (quick about 1.5 min, full about 7 min with 12
# jobs); COMPARE=1 adds one suite.
# Exit status 0 if every run with MEDIUM_ARGS passed.
set -u
MODE=${1:-}; case $MODE in quick|full) shift ;; *) MODE=quick ;; esac
[ $# = 3 ] || { echo "usage: medium_lfn_suite.sh [quick|full] <bindir> <workdir> <outdir>" >&2; exit 2; }
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
[ -z "$(ls -A "$OUT")" ] || { echo "medium_lfn_suite: $OUT is not empty" >&2; exit 1; }
[ -x "$BIN/wssimserver" ] || { echo "medium_lfn_suite: no $BIN/wssimserver" >&2; exit 2; }
export ONLY=${ONLY:-join|mesh|mcast|better|mc_full|revoke|lost|silen|parent_gone|scale|weak|reset|mac_|restart|profiles|keyreq|radio_off}
MEDIUM_ARGS=${MEDIUM_ARGS---medium=legacy}
T0=$SECONDS

# The bindir with the wrapper
mkdir "$OUT/bin"
for f in "$BIN"/*; do
    [ "$(basename "$f")" = wssimserver ] || ln -s "$f" "$OUT/bin/"
done
cat > "$OUT/bin/wssimserver" <<EOF
#!/bin/bash
# wssimserver with MEDIUM_ARGS (medium_lfn_suite.sh)
args=($MEDIUM_ARGS)
[ "${MEDIUM_LOG:-0}" = 1 ] && [ -f simserver.log ] && args+=(--medium-log="\$PWD/medium.log")
exec "$BIN/wssimserver" "\${args[@]}" "\$@"
EOF
chmod +x "$OUT/bin/wssimserver"
echo "medium_lfn_suite: $MODE, wssimserver $MEDIUM_ARGS, tests matching ONLY=$ONLY"

"$HERE/lfn_suite.sh" "$MODE" "$OUT/bin" "$WORK" "$OUT/args"
RC=$?
if [ "${COMPARE:-0}" = 1 ]; then
    echo "medium_lfn_suite: the same suite with the default options (central medium)"
    "$HERE/lfn_suite.sh" "$MODE" "$BIN" "$WORK" "$OUT/default"
    # runs whose result differs (the run times aside)
    join -j2 -a1 -a2 -e MISSING -o 0,1.1,2.1 \
        <(awk '{print $1, $2}' "$OUT/default/results.txt" | sort -k2) \
        <(awk '{print $1, $2}' "$OUT/args/results.txt" | sort -k2) |
        awk -v a="$MEDIUM_ARGS" '$2 != $3 { print $1 ": default " $2 ", " a " " $3 }' > "$OUT/differences.txt"
    echo "medium_lfn_suite: $(wc -l < "$OUT/differences.txt") runs differ ($OUT/differences.txt)"
    cat "$OUT/differences.txt"
fi
echo "medium_lfn_suite: $((SECONDS - T0)) s wall time"
exit $RC
