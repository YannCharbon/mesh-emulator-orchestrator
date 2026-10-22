#!/bin/bash
# Headless equivalent of wisun-mbed-simulator/run.sh dag_simple: same processes,
# same args, same order and delays, but every process logs to a file.
# usage: headless_run.sh <bindir> <workdir(cwd, holds examples/)> <outdir> <seconds>
BIN=$1; WORK=$2; OUT=$3; DUR=$4
NODES=14
TPG="-g 0,1:-34.0 -g 0,2:-36.0 -g 0,3:-71.0 -g 2,4:-21.0 -g 2,5:-45.0 -g 4,6:-2.0 -g 0,7:-80.0 -g 1,7:-39.0 -g 4,7:-67.0 -g 0,8:-16.0 -g 3,8:-41.0 -g 5,8:-80.0 -g 7,8:-12.0 -g 3,9:-56.0 -g 4,9:-20.0 -g 7,9:-33.0 -g 8,9:-21.0 -g 7,10:-29.0 -g 8,11:-22.0 -g 9,12:-30.0 -g 9,13:-74.0 -g 10,14:-59.0"

pkill -x wssimserver; pkill -x wshwsim; pkill -x wsnode; pkill -x wsbrd; sleep 0.5
rm -rf /tmp/wsbrd/ /tmp/sim_socket /tmp/*_pae_* /tmp/n*_
mkdir -p /tmp/wsbrd/ "$OUT"
cd "$WORK" || exit 1
TS() { perl -MTime::HiRes=time -ne 'BEGIN{$|=1} printf "%.3f %s", time, $_' > "$1"; }
PIDS=()
date +%s.%N > "$OUT/t0"
stdbuf -oL -eL $BIN/wssimserver $TPG /tmp/sim_socket --dump 2>&1 | TS "$OUT/simserver.log" & PIDS+=($!)
sleep 0.5
for (( i=0; i<=NODES; i++ )); do
	ihex=$(printf '0x%02X' $i)
	stdbuf -oL -eL $BIN/wshwsim -m 01:02:03:04:05:06:00:${ihex: -2} "/tmp/uart$i" /tmp/sim_socket 2>&1 | TS "$OUT/mac$i.log" & PIDS+=($!)
	sleep 0.1   # wssimserver numbers nodes by connection order: keep it = MAC index (as run_orchestrator.py)
done
sleep 0.5   # gnome-terminal spawning latency in run.sh; ptys must exist before readlink
for (( i=1; i<=NODES; i++ )); do
	stdbuf -oL -eL $BIN/wsnode -F $WORK/examples/wsnode.conf -u $(readlink "/tmp/uart$i") -o storage_prefix=/tmp/n${i}_ 2>&1 | TS "$OUT/node$i.log" & PIDS+=($!)
done
stdbuf -oL -eL $BIN/wsbrd -F $WORK/examples/wsbrd.conf -u $(readlink /tmp/uart0) 2>&1 | TS "$OUT/br.log" & PIDS+=($!)
for (( t=30; t<=DUR; t+=30 )); do sleep 30; echo "== t=$t" >> "$OUT/dbus_nodes.txt"; busctl --user get-property com.silabs.Wisun.BorderRouter /com/silabs/Wisun/BorderRouter com.silabs.Wisun.BorderRouter Nodes >> "$OUT/dbus_nodes.txt" 2>&1; done
# snapshot of the BR's view (same D-Bus API the GUI polls)
busctl --user introspect com.silabs.Wisun.BorderRouter /com/silabs/Wisun/BorderRouter > "$OUT/dbus_introspect.txt" 2>&1
pkill -INT -x wsbrd; pkill -INT -x wsnode; sleep 1; pkill -9 -x wsbrd; pkill -9 -x wsnode
pkill -9 -x wssimserver; pkill -9 -x wshwsim
echo "run finished: $OUT"
