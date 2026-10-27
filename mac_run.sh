#!/bin/bash

# Number of nodes (Number 0 is reserved for BR)
let NODES=2
DIR=$(pwd)
DIR_G="/home/wsbr_mounted"


ihex=$(printf '%02X:%02X' $(($1 >> 8)) $(($1 & 0xFF))) # node ID = the last two bytes of the MAC
./wshwsim -m 01:02:03:04:05:06:${ihex} "/tmp/uart$1" /tmp/sim_socket
#for (( i=0; i<=$NODES; i++ ))
#do
#	ihex=$(printf '%02X:%02X' $((i >> 8)) $((i & 0xFF)))
#	./wshwsim -m 01:02:03:04:05:06:${ihex} "/tmp/uart$i" /tmp/sim_socket
#	sleep 0.05
#done