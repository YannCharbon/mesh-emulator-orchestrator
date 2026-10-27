#!/bin/bash
# Speed check (docs/VIRTUAL_TIME.md §7.4): wall time per simulated second at
# several network sizes.
#
# usage: speed.sh <bindir> <workdir(holds examples/)> <outdir> <seconds> [sizes...]
#   sizes   node counts, border router included (default: 15 100 500). 15 is
#           dag_simple; any other size is a random geometric graph from
#           gen_topology.py (SHAPE=rgg|grid, DEGREE=<mean degree>, TOPO_SEED=<n>,
#           default rgg, 6, 1).
# Every size is one run.sh run (one after the other, RSS=1), in <outdir>/n<size>.
# The summary goes to <outdir>/speed.txt: wall seconds of the run (process
# start to wssimserver exit), wall seconds per simulated second, the sum and
# the largest of the programs' peak RSS, and wssimserver's own last lines
# (its wall time and wake count, §3).
# Other run.sh variables (WSSIM_SEED, TUN, WALL_TIMEOUT, ...) pass through.
# Note: every node is a process with a socket to wssimserver; more than ~1000
# nodes need a higher `ulimit -n`.
set -u
HERE=$(dirname "$(realpath "$0")")
BIN=$1; WORK=$2; OUT=$3; DUR=$4; shift 4
SIZES=("$@")
(( ${#SIZES[@]} )) || SIZES=(15 100 500)
export RSS=1

mkdir -p "$OUT" || exit 1
SUM="$OUT/speed.txt"
printf '%-6s %8s %10s %12s %14s %12s  %s\n' nodes sim_s wall_s wall/sim_s rss_sum_MB rss_max_MB status > "$SUM"
for n in "${SIZES[@]}"; do
	R="$OUT/n$n"
	if [ "$n" = 15 ]; then
		unset TOPO
	else
		export TOPO="$OUT/topology_n$n.txt"
		python3 "$HERE/gen_topology.py" "$n" --shape "${SHAPE:-rgg}" --degree "${DEGREE:-6}" \
			--seed "${TOPO_SEED:-1}" -o "$TOPO" || exit 1
	fi
	echo "== $n nodes, $DUR simulated seconds: $R"
	bash "$HERE/run.sh" "$BIN" "$WORK" "$R" "$DUR"
	RC=$?
	read -r WALL RSS_SUM RSS_MAX < <(python3 - "$R" <<'EOF'
import sys, os
d = sys.argv[1]
info = dict(kv.split('=', 1) for line in open(os.path.join(d, 'run_info.txt'))
            for kv in line.split() if '=' in kv)
wall = float(info['end_wall']) - float(info['start_wall']) if 'end_wall' in info else float('nan')
rss = []
try:
    rss = [int(l.split()[1]) for l in open(os.path.join(d, 'rss.txt')) if l.strip()]
except OSError:
    pass
print(f'{wall:.1f} {sum(rss) / 1024:.0f} {max(rss, default=0) / 1024:.1f}')
EOF
)
	PER=$(awk -v w="$WALL" -v s="$DUR" 'BEGIN { printf "%.4f", w / s }')
	printf '%-6s %8s %10s %12s %14s %12s  %s\n' "$n" "$DUR" "$WALL" "$PER" "$RSS_SUM" "$RSS_MAX" \
		"$([ $RC = 0 ] && echo ok || echo "FAILED ($RC)")" >> "$SUM"
	{ echo "-- wssimserver, n=$n (last lines):"; tail -n 5 "$R/simserver.log"; } >> "$OUT/simserver_tails.txt"
done
echo
cat "$SUM"
echo
cat "$OUT/simserver_tails.txt"
