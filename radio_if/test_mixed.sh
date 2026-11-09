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
# Environment: WSSIM_SEED (default 1), DUR (virtual seconds, default 600),
# POWER (default examples/power/efr32fg25-soc.ini). About 10 s of wall time.
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
tr ' ' '\n' <<< "$DAG_SIMPLE" > "$OUT/topo_base.txt"
tr ' ' '\n' <<< "$DAG_SIMPLE $IF_LINKS" > "$OUT/topo_mixed.txt"

PHY="--chan 8 --rate 150000"
export TUN=none WSSIM_SEED=$SEED POWER FRAMES=1
TOPO=$OUT/topo_base.txt SIM_OPTS="--medium-log=$OUT/base/medium.log" "$WORK/sim_utils/vtime/run.sh" "$BIN" "$WORK" "$OUT/base" "$DUR" > "$OUT/base.out" 2>&1
BASE_RC=$?
TOPO=$OUT/topo_mixed.txt SIM_OPTS="--medium-log=$OUT/mixed/medium.log" IF_NODES="20 21 22" \
	IF_CMD_20="$OUT/bin/rif_node" IF_OPTS_20="$PHY --listen --period-ms 100 --offset-ms 0 --payload-len 100" \
	IF_CMD_21="python3 $HERE/ref_client.py" IF_OPTS_21="$PHY --period-ms 500 --window-ms 450 --tx 130 --tx 330 --payload-len 60" \
	IF_CMD_22="$OUT/bin/rif_node" IF_OPTS_22="$PHY --listen --period-ms 50 --offset-ms 7 --payload-len 100" \
	"$WORK/sim_utils/vtime/run.sh" "$BIN" "$WORK" "$OUT/mixed" "$DUR" > "$OUT/mixed.out" 2>&1
MIXED_RC=$?

python3 "$HERE/check_mixed.py" "$OUT" "$BASE_RC" "$MIXED_RC" "$DUR"
