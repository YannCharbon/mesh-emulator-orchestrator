#!/bin/bash
# One virtual-time run of in-process nodes (docs/VIRTUAL_TIME.md, EM3 step 1).
#
# usage: run.sh <bindir> <workdir(holds examples/)> <outdir> <seconds>
#   bindir   a virtual-time build (wssimserver, wsbrd-inproc, wsnode-inproc)
#   seconds  virtual seconds to simulate (wssimserver --vtime-end)
#
# Environment (all optional):
#   WSSIM_SEED=<n>     the run's seed (default 1)
#   TOPO=<file>        topology, one edge "a,b:rssi" per line ('#' comments);
#                      node 0 is the border router, the node count is the
#                      highest ID + 1. Default: dag_simple (15 nodes), the same
#                      -g list as sim_utils/migration/headless_run.sh.
#   SOCK=<path>        conductor socket (default: a new /tmp/wssim-vt-*.sock)
#   TUN=<dev>          the border router's TUN device (default tun0). It must
#                      exist, belong to the user and be up (tunup.sh). Two runs
#                      at the same time need two TUN devices.
#   SETARCH=1          run every program under `setarch -R` (no address
#                      randomisation; for the determinism check)
#   KEEP_DBUS=1        let the programs register on the user D-Bus. By default
#                      they cannot reach it, so that they never take the names
#                      of another run (com.silabs.Wisun.BorderRouter,
#                      com.wshwsim.mac*): nothing reads D-Bus in batch mode.
#   WALL_TIMEOUT=<s>   wall-clock limit: SIGTERM to wssimserver (which ends the
#                      run with "xq") after this many seconds (default 0: none)
#   RSS=1              sample every program's peak RSS (VmHWM) every second
#                      into <outdir>/rss.txt ("<log name> <kB>")
#   BR_OPTS, NODE_OPTS extra arguments for wsbrd-inproc / every wsnode-inproc
#
# Output in <outdir>: simserver.log, br.log, node<i>.log (each line starts with
# NSIM_EPOCH_S + virtual time, written by the simulation port), t0
# (= NSIM_EPOCH_S), topology.txt, exit_codes.txt, log_wsbr (the BR's trace
# file), storage/ (the stack's key storage, fresh for every run).
#
# The run ends when wssimserver exits. The script never uses pkill: on error
# it kills only the processes it started.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3; DUR=$4
NSIM_EPOCH_S=1700000000
DAG_SIMPLE="0,1:-34.0 0,2:-36.0 0,3:-71.0 2,4:-21.0 2,5:-45.0 4,6:-2.0 0,7:-80.0 1,7:-39.0 4,7:-67.0 0,8:-16.0 3,8:-41.0 5,8:-80.0 7,8:-12.0 3,9:-56.0 4,9:-20.0 7,9:-33.0 8,9:-21.0 7,10:-29.0 8,11:-22.0 9,12:-30.0 9,13:-74.0 10,14:-59.0"

mkdir -p "$OUT" || exit 1
OUT=$(realpath "$OUT")
if [ -n "$(ls -A "$OUT")" ]; then
	echo "run.sh: $OUT is not empty" >&2; exit 1
fi
if [ -n "${TOPO:-}" ]; then
	grep -v '^\s*\(#\|$\)' "$TOPO" > "$OUT/topology.txt" || { echo "run.sh: empty topology $TOPO" >&2; exit 1; }
else
	tr ' ' '\n' <<< "$DAG_SIMPLE" > "$OUT/topology.txt"
fi
TPG=()
MAXID=0
while read -r e; do
	TPG+=(-g "$e")
	for id in $(cut -d: -f1 <<< "$e" | tr ',-' '  '); do
		(( id > MAXID )) && MAXID=$id
	done
done < "$OUT/topology.txt"
NODES=$MAXID                    # routers 1..NODES; node 0 is the border router
SEED=${WSSIM_SEED:-1}
export WSSIM_SEED=$SEED
SOCK=${SOCK:-$(mktemp -u --suffix=.sock /tmp/wssim-vt-XXXXXXXX)}
TUN=${TUN:-tun0}
if [ -e "$SOCK" ]; then
	echo "run.sh: $SOCK exists (another run?)" >&2; exit 1
fi

PRE=()
[ "${SETARCH:-0}" = 1 ] && PRE=(setarch "$(uname -m)" -R)
# Line-buffered until the port takes over stdout at attach, so that the lines
# of a program that fails early are not lost.
PRE+=(stdbuf -oL -eL)
if [ "${KEEP_DBUS:-0}" != 1 ]; then
	export DBUS_STARTER_BUS_TYPE=user                       # wsbrd: no system bus fallback
	export DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent/wssim-vt-no-dbus
fi
export WSBRD_LOG_FILE=$OUT/log_wsbr
mkdir -p "$OUT/storage"
echo $NSIM_EPOCH_S > "$OUT/t0"
cat > "$OUT/run_info.txt" <<EOF
seed=$SEED nodes=$((NODES + 1)) vtime_end=$DUR sock=$SOCK tun=$TUN setarch=${SETARCH:-0}
bin=$BIN work=$WORK
start_wall=$(date +%s.%N)
EOF

declare -A NAME   # pid -> log name
PIDS=()
WATCHDOG=; SAMPLER=
cleanup() {
	local p
	[ -n "$WATCHDOG" ] && kill "$WATCHDOG" 2>/dev/null
	[ -n "$SAMPLER" ] && kill "$SAMPLER" 2>/dev/null
	for p in "${PIDS[@]}"; do kill -TERM "$p" 2>/dev/null; done
	sleep 1
	for p in "${PIDS[@]}"; do kill -KILL "$p" 2>/dev/null; done
	rm -f "$SOCK"
}
abort() {
	echo "run.sh: $1; stopping this run's processes" >&2
	echo "aborted: $1" >> "$OUT/run_info.txt"
	cleanup
	exit 1
}
trap 'abort "interrupted"' INT TERM HUP

cd "$WORK" || exit 1
mac() { printf '01:02:03:04:05:06:%02X:%02X' $(($1 >> 8)) $(($1 & 0xFF)); }
start() {   # start <log name> <program and args...>
	local log=$1; shift
	"${PRE[@]}" "$@" > "$OUT/$log.log" 2>&1 &
	PIDS+=($!); NAME[$!]=$log
}

start simserver "$BIN/wssimserver" "${TPG[@]}" --vtime=$((NODES + 1)) --vtime-end="$DUR" --dump "$SOCK"
SIM_PID=$!
for (( i = 0; i < 200; i++ )); do
	[ -S "$SOCK" ] && break
	kill -0 $SIM_PID 2>/dev/null || abort "wssimserver exited at start (simserver.log)"
	sleep 0.05
done
[ -S "$SOCK" ] || abort "no socket $SOCK after 10 s"

start br "$BIN/wsbrd-inproc" --eui64="$(mac 0)" -F "$WORK/examples/wsbrd.conf" -u "$SOCK" -t "$TUN" \
	-o storage_prefix="$OUT/storage/br_" ${BR_OPTS:-}
for (( i = 1; i <= NODES; i++ )); do
	start node$i "$BIN/wsnode-inproc" --eui64="$(mac $i)" -F "$WORK/examples/wsnode.conf" -u "$SOCK" \
		-o storage_prefix="$OUT/storage/n${i}_" ${NODE_OPTS:-}
done

if [ "${WALL_TIMEOUT:-0}" -gt 0 ]; then
	( sleep "$WALL_TIMEOUT"; echo "run.sh: wall-clock timeout, SIGTERM to wssimserver" >&2; kill -TERM $SIM_PID ) &
	WATCHDOG=$!
fi
if [ "${RSS:-0}" = 1 ]; then
	(
		declare -A peak
		while kill -0 $SIM_PID 2>/dev/null; do
			for p in "${PIDS[@]}"; do
				v=$(awk '/^VmHWM/ {print $2}' /proc/$p/status 2>/dev/null)
				[ -n "$v" ] && (( v > ${peak[$p]:-0} )) && peak[$p]=$v
			done
			for p in "${!peak[@]}"; do echo "${NAME[$p]} ${peak[$p]}"; done | sort -V > "$OUT/rss.tmp"
			mv "$OUT/rss.tmp" "$OUT/rss.txt"
			sleep 1
		done
	) &
	SAMPLER=$!
fi

# Wait for the processes as they end. wssimserver ends the run; a node that
# fails before that stops the run.
declare -A ACTIVE
for p in "${PIDS[@]}"; do ACTIVE[$p]=1; done
SIM_RC=
while (( ${#ACTIVE[@]} > 0 )); do
	DONE=
	wait -n -p DONE "${!ACTIVE[@]}"
	RC=$?
	[ -z "$DONE" ] && break
	unset "ACTIVE[$DONE]"
	echo "${NAME[$DONE]} $RC" >> "$OUT/exit_codes.txt"
	if [ "$DONE" = "$SIM_PID" ]; then
		SIM_RC=$RC
		break
	fi
	if [ "$RC" != 0 ]; then
		kill -TERM $SIM_PID 2>/dev/null      # "xq" to the others
		sleep 2
		abort "${NAME[$DONE]} exited with status $RC before the end of the run"
	fi
done
[ -n "$WATCHDOG" ] && kill "$WATCHDOG" 2>/dev/null
# wssimserver waits for every node to close before it exits; give stragglers
# a moment, then stop them.
for (( i = 0; i < 50; i++ )); do
	ALIVE=0
	for p in "${PIDS[@]}"; do kill -0 "$p" 2>/dev/null && ALIVE=1; done
	(( ALIVE )) || break
	sleep 0.1
done
for p in "${!ACTIVE[@]}"; do
	[ "$p" = "$SIM_PID" ] && continue
	if kill -0 "$p" 2>/dev/null; then
		echo "run.sh: ${NAME[$p]} still running after wssimserver exited; killed" >&2
		kill -KILL "$p" 2>/dev/null
	fi
	if wait "$p" 2>/dev/null; then RC=0; else RC=$?; fi
	echo "${NAME[$p]} $RC" >> "$OUT/exit_codes.txt"
done
[ -n "$SAMPLER" ] && { kill "$SAMPLER" 2>/dev/null; wait "$SAMPLER" 2>/dev/null; rm -f "$OUT/rss.tmp"; }
rm -f "$SOCK"
echo "end_wall=$(date +%s.%N)" >> "$OUT/run_info.txt"
sort -V -o "$OUT/exit_codes.txt" "$OUT/exit_codes.txt"
echo "run finished: $OUT (wssimserver status ${SIM_RC:-?})"
[ "${SIM_RC:-1}" = 0 ]
