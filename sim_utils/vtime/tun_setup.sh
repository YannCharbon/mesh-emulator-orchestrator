#!/bin/bash
# Create the TUN devices that parallel virtual-time runs need: one per border
# router running at the same time (campaign.sh). Run once per boot, with sudo:
#     sudo sim_utils/vtime/tun_setup.sh [count] [user]
#
# Each device is a persistent TUN owned by the user, like tun0 (tunup.sh), with
# no generated addresses and the same fixed link-local address: wsbrd reads and
# logs that address, so a run gives the same logs whichever device it gets.
# With count 0 the devices are removed.
set -e
COUNT=${1:-12}
OWNER=${2:-${SUDO_USER:-$USER}}
LL=fe80::f47d:2495:c476:e1e0/64     # tun0's link-local when these runs were first made

for dev in $(ip -o link show | grep -o 'tunvt[0-9]*' | sort -u); do
    ip link del "$dev"
done
for (( i = 0; i < COUNT; i++ )); do
    dev=tunvt$i
    ip tuntap add mode tun user "$OWNER" name "$dev"
    ip link set "$dev" addrgenmode none
    ip addr add "$LL" dev "$dev" nodad
    ip link set "$dev" up
done
ip -br link show | grep tunvt || echo "no tunvt devices"
