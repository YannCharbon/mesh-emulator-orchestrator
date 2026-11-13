/* SPDX-License-Identifier: Apache-2.0
 *
 * The FHSS timings of the neighbours the stack reports with
 * ns_fhss_ws_update_neighbor() / ns_fhss_ws_drop_neighbor(): wshwsim's copy
 * (sl_wsrcp_mac.c, read by the FHSS through Spinel) and the in-process
 * nodes' mirror (sl_rf_inproc.c). The CCA of the legacy medium reads the
 * slots too (sl_rf_driver.c, g_rf_host.neighbors).
 *
 * The stack does not drop every neighbour it reports: its temporary entries
 * (ws_llc_data_service.c: multicast from a node that is not a neighbour, up
 * to 5 at a time, and EAPOL from a joining node, up to 5 + 100) are reported
 * and later reused or freed without a drop. So the set reported and not
 * dropped grows with every node ever heard, while what the stack holds at a
 * time is bounded: 255 neighbours (device_description_table_size, a uint8_t)
 * plus 110 temporary entries. The table holds RF_NEIGHBORS_MAX slots, above
 * that bound. When it is full, the entries the stack no longer holds are
 * removed (the still_known callback, in-process), or else the entry updated
 * longest ago (wshwsim, which cannot see the stack). It never aborts.
 *
 * Slots are given lowest index first and read in index order, as in the
 * fixed 255-entry arrays this replaces: runs that never fill 255 slots are
 * unchanged. Lookups go through a hash of the EUI-64.
 */
#ifndef SL_RF_NEIGHBORS_H
#define SL_RF_NEIGHBORS_H

#include <stdbool.h>
#include <stdint.h>

#include "sl_rf_driver.h"

#define RF_NEIGHBORS_MAX  512
#define RF_NEIGHBORS_HASH 1024  // buckets, a power of 2

struct rf_neighbors {
    struct neighbor_timings slot[RF_NEIGHBORS_MAX]; // EUI-64 all zero: free
    uint32_t stamp[RF_NEIGHBORS_MAX];   // last update (rf_neighbors.clock)
    uint16_t next[RF_NEIGHBORS_MAX];    // hash chain: slot + 1, 0 ends it
    uint16_t head[RF_NEIGHBORS_HASH];   // first slot + 1 of each bucket, 0: none
    uint32_t clock;
    int count;
    struct fhss_ws_neighbor_timing_info scratch; // for the all-zero EUI-64
    int removed;                        // entries removed because the table was full
    // Optional: whether the stack still holds this neighbour (a neighbour
    // or a temporary entry); used to clean up a full table.
    bool (*still_known)(const uint8_t eui64[8]);
};

// The zero-initialised structure is an empty table.
void rf_neighbors_clear(struct rf_neighbors *t);
struct fhss_ws_neighbor_timing_info *rf_neighbors_get(struct rf_neighbors *t, const uint8_t eui64[8]);
// Finds or adds the entry, making room if the table is full; never NULL.
struct fhss_ws_neighbor_timing_info *rf_neighbors_update(struct rf_neighbors *t, const uint8_t eui64[8]);
// false if there was no such entry
bool rf_neighbors_drop(struct rf_neighbors *t, const uint8_t eui64[8]);

#endif
