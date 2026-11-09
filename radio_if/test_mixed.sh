#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# Mixed run: Nanostack nodes and clients of the stack-neutral radio interface
# (docs/RADIO_INTERFACE.md) in the same virtual-time run, sharing the central
# medium.
#
# usage: radio_if/test_mixed.sh <bindir> <workdir(holds examples/)> <outdir>
#
# The network is dag_simple (border router 0, routers 1-14, wsbrd-inproc and
# wsnode-inproc) plus three interface clients near routers 4, 7, 8 and 9:
# node 20 and node 22 run the C example client (radio_if/c_example, receiver
# always on, a 100-byte frame every 100 ms and 50 ms), node 21 the Python
# reference client (a 450 ms window every 500 ms, two 60-byte frames in each).
# All three use channel 8, one of the 35 channels the Wi-SUN nodes hop on,
# at the nodes' bit rate (150 kbit/s). The same seed is also run without
# the clients, as the baseline of the network's formation.
#
# Checks (one PASS/FAIL line each, exit status 1 on any failure):
#   run        both runs end normally, every program exits with status 0
#   formation  every router joins the border router's tree, as in the baseline
#   if_if      the clients receive each other's frames; each client's rx_ok
#              and rx_lost lines match the medium log pair by pair
#   ns_to_if   the clients receive Nanostack frames (foreign bytes to them)
#   if_to_ns   the Wi-SUN nodes lock onto the clients' frames (foreign bytes
#              to their MAC, which drops them) without failing
#   collisions the medium applies collisions across the two kinds: a client's
#              frame arriving at a Wi-SUN node busy with a Wi-SUN frame
#              (busy/captured), a Wi-SUN frame arriving at a busy client, and
#              frames lost to interference at both kinds of receiver
#   power      power.json covers all 18 nodes; every node's state times add up
#              to the run time; each client's TX time is exactly its frames'
#              airtimes; its receive time is not less than its frames received
#   server     no interface rule broken, no unexpected message
#
# With SNIFF=1, the clients use preamble sniffing (interface version 2,
# docs/RADIO_INTERFACE.md §6.7) instead, near the same routers and on the same
# channel. Nodes 20 and 22 run the C example client with version 2: a 1 ms
# sniff every 100 ms with the lock flag (at 0 and 63 ms in the period), the
# radio off in between, and a 100-byte frame with a 110 ms wake-up preamble
# every 2 s (at 250 and 1250 ms), with a CCA before it. Node 21 runs the
# Python reference client: a 10 ms sniff every 50 ms (at 37 ms), no frames.
# Node 23 runs the C client with version 1: receiver always on, a 100-byte
# frame every 50 ms from 100.4 ms, every other one inside node 20's sniffs
# (it gives the collisions with the Wi-SUN nodes). The checks are then:
#   run, formation, collisions, server   as above (server: also the sniff counts)
#   detection  each sniffer detects and receives the other version-2 clients'
#              long-preamble frames through its sniffs (locked = 1); its
#              rx_ok and rx_lost lines match the medium log pair by pair;
#              every client sniff has its line in the server's sniff log,
#              with the same outcome as the client's log
#   lock_ns    sniffs detect Wi-SUN frames and the clients receive them
#   v1         the version-1 client receives the long-preamble frames (from
#              their start, without the LONG_PREAMBLE flag, which it refuses)
#              and Wi-SUN frames; node 20's sniffs find its frames already
#              being received (locked = 2) and node 20 receives them
#   if_to_ns   as above
#   power      as above (TX time includes the wake-up preambles); a sniffer's
#              listening time is at least its sniffs that detected nothing,
#              and at most all its sniffs plus 1 ms of CCA listening per
#              frame sent
#
# Environment: WSSIM_SEED (default 1), DUR (virtual seconds, default 600),
# POWER (default examples/power/efr32fg25-soc.ini), SNIFF (0). About 10 s of
# wall time.
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3
DUR=${DUR:-600}
SEED=${WSSIM_SEED:-1}
POWER=${POWER:-$WORK/examples/power/efr32fg25-soc.ini}
HERE=$(dirname "$(realpath "$0")")
mkdir -p "$OUT" && OUT=$(realpath "$OUT") || exit 1
[ -z "$(ls -A "$OUT")" ] || { echo "test_mixed.sh: $OUT is not empty" >&2; exit 1; }

mkdir -p "$OUT/bin"
cc -std=gnu11 -O2 -Wall -Wextra -Werror -o "$OUT/bin/rif_node" "$HERE/c_example/rif_node.c" || exit 1

DAG_SIMPLE="0,1:-34.0 0,2:-36.0 0,3:-71.0 2,4:-21.0 2,5:-45.0 4,6:-2.0 0,7:-80.0 1,7:-39.0 4,7:-67.0 0,8:-16.0 3,8:-41.0 5,8:-80.0 7,8:-12.0 3,9:-56.0 4,9:-20.0 7,9:-33.0 8,9:-21.0 7,10:-29.0 8,11:-22.0 9,12:-30.0 9,13:-74.0 10,14:-59.0"
IF_LINKS="20,21:-60 20,22:-62 21,22:-64 20,8:-55 20,9:-58 21,8:-65 21,7:-60 22,9:-50 22,4:-70"
SNIFF=${SNIFF:-0}
[ "$SNIFF" = 1 ] && IF_LINKS="$IF_LINKS 23,20:-63 23,21:-61 23,22:-66 23,8:-60 23,7:-62"
tr ' ' '\n' <<< "$DAG_SIMPLE" > "$OUT/topo_base.txt"
tr ' ' '\n' <<< "$DAG_SIMPLE $IF_LINKS" > "$OUT/topo_mixed.txt"

PHY="--chan 8 --rate 150000"
export TUN=none WSSIM_SEED=$SEED POWER FRAMES=1
TOPO=$OUT/topo_base.txt SIM_OPTS="--medium-log=$OUT/base/medium.log" "$WORK/sim_utils/vtime/run.sh" "$BIN" "$WORK" "$OUT/base" "$DUR" > "$OUT/base.out" 2>&1
BASE_RC=$?
if [ "$SNIFF" = 1 ]; then
	SN="--if-version 2 --sniff-period-ms 100 --sniff-us 1000"
	LP="--lp-preamble-ms 110 --period-ms 2000 --payload-len 100"
	TOPO=$OUT/topo_mixed.txt IF_NODES="20 21 22 23" \
		SIM_OPTS="--medium-log=$OUT/mixed/medium.log --sniff-log=$OUT/mixed/sniff.log" \
		IF_CMD_20="$OUT/bin/rif_node" IF_OPTS_20="$PHY $SN $LP --offset-ms 250" \
		IF_CMD_21="python3 $HERE/ref_client.py" IF_OPTS_21="$PHY --sniff-period-ms 50 --sniff-us 10000 --sniff-phase-ms 37" \
		IF_CMD_22="$OUT/bin/rif_node" IF_OPTS_22="$PHY $SN --sniff-phase-ms 63 $LP --offset-ms 1250" \
		IF_CMD_23="$OUT/bin/rif_node" IF_OPTS_23="$PHY --listen --period-ms 50 --offset-ms 100.4 --payload-len 100" \
		"$WORK/sim_utils/vtime/run.sh" "$BIN" "$WORK" "$OUT/mixed" "$DUR" > "$OUT/mixed.out" 2>&1
	MIXED_RC=$?
	python3 "$HERE/check_mixed.py" "$OUT" "$BASE_RC" "$MIXED_RC" "$DUR" sniff
	exit
fi
TOPO=$OUT/topo_mixed.txt SIM_OPTS="--medium-log=$OUT/mixed/medium.log" IF_NODES="20 21 22" \
	IF_CMD_20="$OUT/bin/rif_node" IF_OPTS_20="$PHY --listen --period-ms 100 --offset-ms 0 --payload-len 100" \
	IF_CMD_21="python3 $HERE/ref_client.py" IF_OPTS_21="$PHY --period-ms 500 --window-ms 450 --tx 130 --tx 330 --payload-len 60" \
	IF_CMD_22="$OUT/bin/rif_node" IF_OPTS_22="$PHY --listen --period-ms 50 --offset-ms 7 --payload-len 100" \
	"$WORK/sim_utils/vtime/run.sh" "$BIN" "$WORK" "$OUT/mixed" "$DUR" > "$OUT/mixed.out" 2>&1
MIXED_RC=$?

python3 "$HERE/check_mixed.py" "$OUT" "$BASE_RC" "$MIXED_RC" "$DUR"
