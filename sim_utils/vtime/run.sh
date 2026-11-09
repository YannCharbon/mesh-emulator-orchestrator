#!/bin/bash
# One virtual-time run of in-process nodes (docs/VIRTUAL_TIME.md, EM3 step 1).
#
# usage: run.sh <bindir> <workdir(holds examples/)> <outdir> <seconds>
#   bindir   a virtual-time build (wssimserver, wsbrd-inproc, wsnode-inproc)
#   seconds  virtual seconds to simulate (wssimserver --vtime-end)
#
# Environment (all optional):
#   WSSIM_SEED=<n>     the run's seed (default 1)
#   TOPO=<file>        topology, one wssimserver -g argument per line, without
#                      "-g": "a,b:rssi" (a link, both directions unless the
#                      file also has "b,a:rssi"), "a,b" (0 dBm) or a group
#                      "0-3:rssi"; blank lines and lines starting with '#'
#                      skipped (sim_utils/vtime/README.md, "Topology files").
#                      Node 0 is the border router, the node count is the
#                      highest ID + 1. Default: dag_simple (15 nodes), the same
#                      -g list as sim_utils/migration/headless_run.sh. The
#                      cleaned file (<outdir>/topology.txt) goes to wssimserver
#                      as --group-file, so any size up to 4096 node IDs works
#                      (no command-line limit; a 1000-node building with
#                      19 000 links is read in well under a second)
#   SOCK=<path>        conductor socket (default: a new /tmp/wssim-vt-*.sock)
#   TUN=<dev>          the border router's TUN device (default tun0). It must
#                      exist, belong to the user and be up (tunup.sh). Two runs
#                      at the same time need two TUN devices. TUN=none: no
#                      device (the border router's tun_device = none: its IPv6
#                      output goes nowhere, its link-local address is the one
#                      of the tunvt devices); for runs whose traffic comes
#                      from the programs themselves (test_udp_*), any number
#                      at a time, no sudo
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
#   WSSIM_RADIO_OFF=0  the emulated radio ignores PHY_INTERFACE_DOWN (RX stays
#                      on), as before GS8 stage 1: to compare with older runs
#   CONF=<name>        use examples/wsbrd-<name>.conf and wsnode-<name>.conf
#                      instead of wsbrd.conf and wsnode.conf; CONF=eu1a is the
#                      FG25 firmware's PHY (EU, mode 1a, class 1, +10 dBm)
#   WSSIM_PHY_*        the emulated PHY's data rate and overhead, for every
#                      node (docs/POWER_MODEL.md §6.1): WSSIM_PHY_DATARATE,
#                      _PREAMBLE, _SFD, _PHR, _FCS; WSSIM_PHY_LEGACY=1 for the
#                      PHY before them (150 kbit/s, PSDU-only airtime)
#   BR_ENV, NODE_ENV_<i> environment for the border router / node i only, e.g.
#                      NODE_ENV_3="WSSIM_PHY_PREAMBLE=16"
#   BR_OPTS, NODE_OPTS extra arguments for wsbrd-inproc / every wsnode-inproc
#   NODE_OPTS_<i>      extra arguments for node i only, after NODE_OPTS, e.g.
#                      NODE_OPTS_3="-o tx_power=8" (power settings per node:
#                      docs/POWER_MODEL.md §6.1)
#   POWER=<profile>    record power (docs/POWER_MODEL.md): every node with this
#                      profile (e.g. examples/power/efr32fg25-soc.ini); the
#                      report is <outdir>/power.json. Off by default.
#   POWER_LOG=1        with POWER, also log every state interval
#                      (<outdir>/power_log.txt; large)
#   POWER_CELL=<mAh[:V[:eff]]> with POWER, battery life for this cell
#   POWER_OPTS         more wssimserver options (e.g. --power-node=3:<profile>)
#   SIM_OPTS           more wssimserver options, with or without POWER (e.g.
#                      --drift-random=20, --drift=3:-15: clock drift, EM3 step 5)
#   KILL="<i>:<s> ..."  stop node i outright at virtual second s (wssimserver
#                      --vtime-kill: no more wakes or frames, its connection
#                      closed; the program then exits with "conductor closed
#                      the connection", which is expected)
#   RESTART="<i>:<s> ..." a new instance of node i (same options, same key
#                      storage) starts at virtual second s (--vtime-start);
#                      the running one, if any, is stopped then. Its log is
#                      node<i>.r<k>.log (k = 1, 2, ...); its clock starts
#                      at 0 at s, as a device's at power-up
#   POWER_UP="<i>:<s> ..." node i is powered up (welcomed) at virtual second s
#                      instead of 0: until then it neither runs nor hears
#                      (--vtime-power-up). With POWER, its ledger starts then
#                      and its report covers its own time ("start_us")
#   SPREAD=<s>         with SPREAD_NODES="<i> ...": each of these nodes is
#                      powered up at its own offset in [0, s), drawn from
#                      WSSIM_SEED and its ID (the same for the same seed);
#                      0 or unset: all at 0. Devices do not power up within
#                      the same microsecond: without it, nodes with the same
#                      deterministic timers (an LFN's LPAS sweeps) send in step
#   CUT="<a>:<b>:<s> ..." from virtual second s, frames from node a no longer
#                      reach node b (one direction; --vtime-cut)
#   LINK="<a>:<b>:<s>:<rssi> ..." from virtual second s, the link a-b (both
#                      directions) has this RSSI (dBm; --vtime-link)
#   DROP="<a>:<b>:<seq>:<s1>:<s2> ..." frames from a with MAC sequence number
#                      seq are not delivered to b between s1 and s2 (a lost
#                      frame and its MAC retries; --vtime-drop)
#   DUMP=1|0|auto      print the topology and the RSSI map (row: receiver) at the
#                      start of simserver.log (wssimserver --dump); auto, the
#                      default: only for runs of at most 200 nodes (the maps
#                      have N^2 entries)
#   FRAMES=1           write every frame sent to <outdir>/frames.txt (time us,
#                      sender, channel, PSDU in hex; --vtime-frames)
#   IF_NODES="<i> ..." nodes that are clients of the stack-neutral radio
#                      interface (docs/RADIO_INTERFACE.md) instead of
#                      wsnode-inproc: each runs IF_CMD_<i> (default IF_CMD)
#                      --sock <socket> --id <i> IF_OPTS_<i>, logging to
#                      if<i>.log. Their IDs may appear in TOPO but not among
#                      the routers (they do not count in the router range
#                      1..highest ID). E.g. IF_CMD="python3 radio_if/ref_client.py"
#                      The programs run in <outdir>, so in IF_CMD, IF_CMD_<i>
#                      and IF_OPTS_<i> a word with a '/' that names an existing
#                      file or directory relative to the directory run.sh is
#                      called from is made absolute (radio_if/ref_client.py,
#                      ./my_client, sched/3.txt); a bare name (my_client) is
#                      looked up on PATH, as by the shell. Other relative
#                      paths (files the client creates) are relative to
#                      <outdir>. With node 0 in IF_NODES, the run has no
#                      Nanostack program at all (no border router, no
#                      routers): every node of TOPO must then be in IF_NODES
#
# Output in <outdir>: simserver.log, br.log, node<i>.log (each line starts with
# NSIM_EPOCH_S + virtual time, written by the simulation port), if<i>.log
# (the clients' output, as they write it), t0 (= NSIM_EPOCH_S), topology.txt,
# exit_codes.txt, run_info.txt, and with Nanostack programs only: log_wsbr (the
# BR's trace file), storage/ (the stack's key storage, fresh for every run),
# examples (a link to <workdir>/examples). A run of clients only (node 0 in
# IF_NODES) needs no examples/ in <workdir>.
#
# The run ends when wssimserver exits. The script never uses pkill: on error
# it kills only the processes it started. It fails (exit status 1) if any
# program exits with a non-zero status (an AddressSanitizer or leak report
# included), except the instances that KILL or RESTART stopped.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3; DUR=$4
BR_CONF=$WORK/examples/wsbrd${CONF:+-$CONF}.conf
NODE_CONF=$WORK/examples/wsnode${CONF:+-$CONF}.conf
if [ -n "${POWER:-}" ]; then
	POWER=$(realpath "$POWER") && [ -f "$POWER" ] || { echo "run.sh: no power profile ${POWER:-}" >&2; exit 1; }
fi
NSIM_EPOCH_S=1700000000
DAG_SIMPLE="0,1:-34.0 0,2:-36.0 0,3:-71.0 2,4:-21.0 2,5:-45.0 4,6:-2.0 0,7:-80.0 1,7:-39.0 4,7:-67.0 0,8:-16.0 3,8:-41.0 5,8:-80.0 7,8:-12.0 3,9:-56.0 4,9:-20.0 7,9:-33.0 8,9:-21.0 7,10:-29.0 8,11:-22.0 9,12:-30.0 9,13:-74.0 10,14:-59.0"

mkdir -p "$OUT" || exit 1
OUT=$(realpath "$OUT")
if [ -n "$(ls -A "$OUT")" ]; then
	echo "run.sh: $OUT is not empty" >&2; exit 1
fi
if [ -n "${TOPO:-}" ]; then
	# As wssimserver --group-file: comments, blanks and a leading -g dropped
	sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^--group=//' -e 's/^-g[[:space:]]*//' "$TOPO" |
		grep -v '^$' > "$OUT/topology.txt" || { echo "run.sh: empty topology $TOPO" >&2; exit 1; }
	if grep -n '[[:space:]]' "$OUT/topology.txt" >&2; then
		echo "run.sh: $TOPO: one a,b:rssi per line (above: lines with spaces)" >&2; exit 1
	fi
else
	tr ' ' '\n' <<< "$DAG_SIMPLE" > "$OUT/topology.txt"
fi
declare -A IFN      # clients of the radio interface
for i in ${IF_NODES:-}; do IFN[$i]=1; done
# One pass over the topology (a 1000-node building has about 19 000 lines):
# the highest ID that is not a client, every ID of a group "a-b" included.
# With node 0 a client, every ID must be a client. The server reads the same
# file with --group-file (no -g per link: no command-line length limit).
MAXID=$(awk -v ifn="${IF_NODES:-}" -v allif="${IFN[0]:+1}" '
	BEGIN { n = split(ifn, a, " "); for (i = 1; i <= n; i++) isif[a[i] + 0] = 1; max = 0 }
	{
		sub(/:.*/, "")
		k = split($0, t, ",")
		for (j = 1; j <= k; j++) {
			m = split(t[j], r, "-"); lo = r[1] + 0; hi = (m > 1 ? r[2] : r[1]) + 0
			for (id = lo; id <= hi; id++) {
				if (id in isif) continue
				if (allif) { printf "run.sh: node 0 is in IF_NODES, so node %d of TOPO must be too\n", id > "/dev/stderr"; bad = 1; exit }
				if (id > max) max = id
			}
		}
	}
	END { if (bad) exit 1; print max }' "$OUT/topology.txt") || exit 1
NODES=$MAXID                    # routers 1..NODES; node 0 is the border router
NSBR=1                          # 1: node 0 is the border router (wsbrd-inproc)
if [ -n "${IFN[0]:-}" ]; then   # node 0 is a client: no Nanostack program in the run
	NSBR=0
	NODES=0
fi
if (( NSBR + NODES > 0 )); then  # Nanostack programs: their configurations
	for f in "$BR_CONF" "$NODE_CONF"; do
		[ -f "$f" ] || { echo "run.sh: no $f" >&2; exit 1; }
	done
fi
# The programs run in <outdir>: a word of IF_CMD, IF_CMD_<i> or IF_OPTS_<i>
# that is a relative path (it has a '/') naming a file or directory that
# exists from the caller's directory is made absolute (CALLER). A bare
# program name is looked up on PATH, as by the shell.
CALLER=$PWD
abs_words() {   # abs_words <words>: the words, relative paths made absolute
	local - w out=()
	set -f              # no globbing here: the words are expanded as before at start
	for w in $1; do
		[[ $w == */* && $w != /* && -e $CALLER/$w ]] && w=$CALLER/${w#./}
		out+=("$w")
	done
	echo "${out[*]}"
}
NIF=0
declare -A ICMD IOPT  # client node -> its command, its options
for i in ${IF_NODES:-}; do
	NIF=$((NIF + 1))
	(( (i > NODES || (i == 0 && NSBR == 0)) && i < 4096 )) || { echo "run.sh: IF_NODES: $i is the border router or a router (0..$NODES)" >&2; exit 1; }
	c="IF_CMD_$i"; o="IF_OPTS_$i"
	[ -n "${!c:-${IF_CMD:-}}" ] || { echo "run.sh: IF_NODES: no IF_CMD or IF_CMD_$i" >&2; exit 1; }
	if [ -n "${!c:-}" ]; then ICMD[$i]=$(abs_words "${!c}")
	else
		[ -n "${if_cmd_abs+x}" ] || if_cmd_abs=$(abs_words "$IF_CMD")
		ICMD[$i]=$if_cmd_abs
	fi
	[ -n "${!o:-}" ] && IOPT[$i]=$(abs_words "${!o}")
done
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
if (( NSBR + NODES > 0 )); then  # Nanostack programs: the BR's trace file, the key storage
	export WSBRD_LOG_FILE=$OUT/log_wsbr
	mkdir -p "$OUT/storage"
fi
echo $NSIM_EPOCH_S > "$OUT/t0"
cat > "$OUT/run_info.txt" <<EOF
seed=$SEED nodes=$((NODES + NSBR + NIF)) if_nodes=${IF_NODES:-} vtime_end=$DUR sock=$SOCK tun=$TUN setarch=${SETARCH:-0} conf=${CONF:-default}
phy_env=$(env | grep '^WSSIM_PHY_' | sort | tr '\n' ' ')
bin=$BIN work=$WORK
start_wall=$(date +%s.%N)
EOF

declare -A NAME   # pid -> log name
declare -A ACTIVE # pids started and not yet reaped: the only ones signalled
PIDS=()
WATCHDOG=; SAMPLER=
cleanup() {
	local p
	[ -n "$WATCHDOG" ] && kill "$WATCHDOG" 2>/dev/null
	[ -n "$SAMPLER" ] && kill "$SAMPLER" 2>/dev/null
	for p in "${!ACTIVE[@]}"; do kill -TERM "$p" 2>/dev/null; done
	sleep 1
	for p in "${!ACTIVE[@]}"; do kill -KILL "$p" 2>/dev/null; done
	rm -f "$SOCK"
}
abort() {
	echo "run.sh: $1; stopping this run's processes" >&2
	echo "aborted: $1" >> "$OUT/run_info.txt"
	cleanup
	exit 1
}
trap 'abort "interrupted"' INT TERM HUP

# The programs run in <outdir>, with examples/ linked there (the configs name
# their certificates relative to the working directory), and the key storage
# is given relative to it: the stack prints the storage paths, so they must be
# the same in every run for the determinism check. Not for a run of clients
# only.
(( NSBR + NODES > 0 )) && ln -s "$WORK/examples" "$OUT/examples"
cd "$OUT" || exit 1
mac() { printf '01:02:03:04:05:06:%02X:%02X' $(($1 >> 8)) $(($1 & 0xFF)); }
start() {   # start <log name> <program and args...>
	local log=$1; shift
	"${PRE[@]}" "$@" > "$OUT/$log.log" 2>&1 &
	PIDS+=($!); NAME[$!]=$log; ACTIVE[$!]=1
}

PWR=()
if [ -n "${POWER:-}" ]; then
	PWR=(--power="$POWER" --power-out="$OUT/power.json")
	[ "${POWER_LOG:-0}" = 1 ] && PWR+=(--power-log="$OUT/power_log.txt")
	[ -n "${POWER_CELL:-}" ] && PWR+=(--power-cell="$POWER_CELL")
	PWR+=(${POWER_OPTS:-})
	echo "power=$POWER log=${POWER_LOG:-0} cell=${POWER_CELL:-}" >> "$OUT/run_info.txt"
fi
CTL=()
declare -A STOPPED  # node -> number of its instances that KILL/RESTART stop
for k in ${KILL:-}; do
	CTL+=(--vtime-kill="$k"); STOPPED[${k%%:*}]=$(( ${STOPPED[${k%%:*}]:-0} + 1 ))
done
LATE=()             # "node:log" of each instance started later
declare -A NINST
for k in ${RESTART:-}; do
	i=${k%%:*}
	(( i >= 1 && i <= NODES )) || { echo "run.sh: RESTART: no node $i" >&2; exit 1; }
	CTL+=(--vtime-start="$k"); STOPPED[$i]=$(( ${STOPPED[$i]:-0} + 1 ))
	NINST[$i]=$(( ${NINST[$i]:-0} + 1 )); LATE+=("$i:node$i.r${NINST[$i]}")
done
for k in ${POWER_UP:-}; do CTL+=(--vtime-power-up="$k"); done
if [ -n "${SPREAD_NODES:-}" ] && [ "${SPREAD:-0}" != 0 ]; then
	for k in $(python3 -c '
import hashlib, sys
seed, spread = sys.argv[1], float(sys.argv[2])
for i in sys.argv[3:]:
    h = int.from_bytes(hashlib.sha256(f"power-up {seed} {i}".encode()).digest()[:8], "big")
    print(f"{i}:{h / 2**64 * spread:.6f}")' "$SEED" "$SPREAD" $SPREAD_NODES); do
		CTL+=(--vtime-power-up="$k")
	done
fi
for k in ${CUT:-}; do CTL+=(--vtime-cut="$k"); done
for k in ${LINK:-}; do CTL+=(--vtime-link="$k"); done
for k in ${DROP:-}; do CTL+=(--vtime-drop="$k"); done
[ "${FRAMES:-0}" = 1 ] && CTL+=(--vtime-frames="$OUT/frames.txt")
[ ${#CTL[@]} -gt 0 ] && echo "events=${CTL[*]}" >> "$OUT/run_info.txt"
DUMPOPT=()          # the topology and RSSI matrices in simserver.log: N^2 entries each
case ${DUMP:-auto} in
1) DUMPOPT=(--dump) ;;
auto) (( NODES + NSBR + NIF <= 200 )) && DUMPOPT=(--dump) ;;
esac
start simserver "$BIN/wssimserver" --group-file="$OUT/topology.txt" --vtime=$((NODES + NSBR + NIF)) --vtime-end="$DUR" "${PWR[@]}" ${SIM_OPTS:-} "${CTL[@]}" "${DUMPOPT[@]}" "$SOCK"
SIM_PID=$!
for (( i = 0; i < 200; i++ )); do
	[ -S "$SOCK" ] && break
	kill -0 $SIM_PID 2>/dev/null || abort "wssimserver exited at start (simserver.log)"
	sleep 0.05
done
[ -S "$SOCK" ] || abort "no socket $SOCK after 10 s"

[ $NSBR = 1 ] && start br env ${BR_ENV:-} "$BIN/wsbrd-inproc" --eui64="$(mac 0)" -F "$BR_CONF" -u "$SOCK" -t "$TUN" \
	-o storage_prefix=storage/br_ ${BR_OPTS:-}
start_node() {   # start_node <i> <log name>
	start "$2" env $(v="NODE_ENV_$1"; echo "${!v:-}") "$BIN/wsnode-inproc" --eui64="$(mac $1)" -F "$NODE_CONF" -u "$SOCK" \
		-o storage_prefix="storage/n${1}_" ${NODE_OPTS:-} $(v="NODE_OPTS_$1"; echo "${!v:-}")
}
for (( i = 1; i <= NODES; i++ )); do
	start_node $i node$i
done
for i in ${IF_NODES:-}; do
	start "if$i" ${ICMD[$i]} --sock "$SOCK" --id "$i" ${IOPT[$i]:-}
done
# Later instances connect once the first instance of their node has: the
# conductor takes a node's first hello as its first instance.
for l in "${LATE[@]}"; do
	i=${l%%:*}
	for (( w = 0; w < 600; w++ )); do
		grep -q "node $i connected" "$OUT/simserver.log" 2>/dev/null && break
		kill -0 $SIM_PID 2>/dev/null || abort "wssimserver exited at start (simserver.log)"
		sleep 0.05
	done
	start_node "$i" "${l#*:}"
done

if [ "${WALL_TIMEOUT:-0}" -gt 0 ]; then
	# The run is then incomplete: wall_timeout marks it, and run.sh fails.
	( sleep "$WALL_TIMEOUT"; touch "$OUT/wall_timeout"
	  echo "run.sh: wall-clock timeout, SIGTERM to wssimserver" >&2; kill -TERM $SIM_PID ) &
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

# An instance that KILL or RESTART stopped exits with "conductor closed the
# connection" (status 2): expected, as many times as they stop that node.
declare -A STOPPED_SEEN ACCEPTED
stopped_ok() {   # stopped_ok <log name>
	local n=${1#node}; n=${n%%.*}
	[ -n "${ACCEPTED[$1]:-}" ] && return 0
	[ "$1" != br ] && [ "$1" != simserver ] || return 1
	(( ${STOPPED_SEEN[$n]:-0} < ${STOPPED[$n]:-0} )) || return 1
	grep -q "conductor closed the connection" "$OUT/$1.log" || return 1
	STOPPED_SEEN[$n]=$(( ${STOPPED_SEEN[$n]:-0} + 1 )); ACCEPTED[$1]=1
	echo "$1 stopped by the conductor" >> "$OUT/run_info.txt"
}
# Wait for the processes as they end. wssimserver ends the run; a node that
# fails before that stops the run.
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
	if [ "$RC" != 0 ] && stopped_ok "${NAME[$DONE]}"; then
		continue
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
	for p in "${!ACTIVE[@]}"; do [ "$p" != "$SIM_PID" ] && kill -0 "$p" 2>/dev/null && ALIVE=1; done
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
if [ -e "$OUT/wall_timeout" ]; then
	echo "run stopped by WALL_TIMEOUT=$WALL_TIMEOUT s before its end: $OUT" >&2
	exit 1
fi
# Any program that ended with a non-zero status fails the run: an
# AddressSanitizer or leak report at exit, a crash after the end
BAD=
while read -r n rc; do
	stopped_ok "$n" || BAD+="$n:$rc "
done < <(awk '$1 != "simserver" && $2 != 0' "$OUT/exit_codes.txt")
echo "run finished: $OUT (wssimserver status ${SIM_RC:-?})"
if [ -n "$BAD" ]; then
	echo "run.sh: non-zero exit status: $BAD(exit_codes.txt)" >&2
	echo "failed: non-zero exit status: $BAD" >> "$OUT/run_info.txt"
	exit 1
fi
[ "${SIM_RC:-1}" = 0 ]
