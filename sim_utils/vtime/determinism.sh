#!/bin/bash
# Determinism check (docs/VIRTUAL_TIME.md §7.2): the same seed twice must give
# byte-identical logs, and another seed must give different logs.
#
# usage: determinism.sh <bindir> <workdir(holds examples/)> <outdir> <seconds> [seed] [other seed]
#   Runs <outdir>/a (seed), <outdir>/b (seed) and <outdir>/c (other seed,
#   default seed + 1) one after the other with run.sh, under setarch -R
#   (SETARCH=0 to turn it off), then compares a with b and a with c
#   (compare_logs.py: first differing line per file).
# Other run.sh variables (TOPO, TUN, WALL_TIMEOUT, ...) pass through.
# Exit status 0 when a = b and a != c.
set -u
HERE=$(dirname "$(realpath "$0")")
BIN=$1; WORK=$2; OUT=$3; DUR=$4
SEED=${5:-1}; OTHER=${6:-$((SEED + 1))}
export SETARCH=${SETARCH:-1}

mkdir -p "$OUT" || exit 1
for r in "a $SEED" "b $SEED" "c $OTHER"; do
	set -- $r
	echo "== run $1, seed $2"
	if ! WSSIM_SEED=$2 bash "$HERE/run.sh" "$BIN" "$WORK" "$OUT/$1" "$DUR"; then
		echo "determinism.sh: run $1 failed (see $OUT/$1)"; exit 2
	fi
done

echo; echo "== a vs b (seed $SEED twice: must be identical)"
python3 "$HERE/compare_logs.py" "$OUT/a" "$OUT/b"; AB=$?
echo; echo "== a vs c (seed $SEED vs $OTHER: must differ)"
python3 "$HERE/compare_logs.py" --quiet "$OUT/a" "$OUT/c"; AC=$?

# compare_logs.py: 0 identical, 1 different, anything else an error
echo
if (( AB > 1 || AC > 1 )); then
	echo "determinism: ERROR, compare_logs.py failed"; exit 2
fi
SAME=$AB; OTHER_SAME=$((1 - AC))
if (( SAME == 0 && OTHER_SAME == 0 )); then
	echo "determinism: PASS"; exit 0
fi
(( SAME != 0 )) && echo "determinism: FAIL, the same seed gave different logs"
(( OTHER_SAME != 0 )) && echo "determinism: FAIL, another seed gave the same logs"
exit 1
