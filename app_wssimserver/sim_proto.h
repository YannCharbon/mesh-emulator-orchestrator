/* SPDX-License-Identifier: Apache-2.0
 *
 * What wssimserver and the simulated radios (app_wshwsim/sl_rf_driver.c)
 * share: the socket messages and the shared-memory tables.
 *
 * Each socket message is one SOCK_SEQPACKET message. A radio sends a hello
 * with its node ID right after it connects, then frames. The server numbers
 * nodes by that ID, not by connection order, and stamps the sender's ID on
 * each frame it relays.
 */
#ifndef SIM_PROTO_H
#define SIM_PROTO_H

#include <stdint.h>

// Node IDs go from 0 to SIM_MAX_NODES - 1. A node's ID is the last two bytes
// of its EUI-64, big-endian (01:02:03:04:05:06:01:2C is node 300).
#define SIM_MAX_NODES 4096

static inline int sim_node_id(const uint8_t eui64[8])
{
    return eui64[6] << 8 | eui64[7];
}

struct sim_hello {
    char magic[2];              // "xh"
    uint16_t node_id;
};

struct sim_frame_hdr {
    char magic[2];              // "xx"
    uint16_t len;               // bytes of frame after this header
    uint16_t chan;
    uint16_t seq;               // per-sender sequence number, for traces
    uint16_t src;               // sender's node ID, set by the server
};

// Shared memory "/wssimnodesinfos": one entry per (receiver, sender) pair,
// written by wssimserver from its -g options.
struct node_infos {
    float rssi;                 // dBm; 0 means not configured (perfect link)
};

#define SIM_SHM_INFOS_SIZE ((long)SIM_MAX_NODES * SIM_MAX_NODES * sizeof(struct node_infos))

static inline struct node_infos *sim_link(struct node_infos *map, int rx, int tx)
{
    return map + (long)rx * SIM_MAX_NODES + tx;
}

// Shared memory "/wssimcca": 3 bytes per node, the radio state then the
// channel, read by the neighbours' CCA.
#define SIM_SHM_CCA_SIZE (SIM_MAX_NODES * 3)

#endif
