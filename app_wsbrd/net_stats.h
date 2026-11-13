/* SPDX-License-Identifier: Apache-2.0
 *
 * Per-node network statistics (docs/TRAFFIC_AND_REPORT.md): every
 * stats_period_s of virtual time, and once at the end of the run, one line
 *   NET1 {json}
 * with the node's cumulative MAC, adaptation-layer (6LoWPAN), IP, RPL and
 * application counters, its queue depths (current, peak, time integral) and
 * its RPL parent and rank. Off unless stats = true.
 *
 * Sources: the stack's own statistics APIs (ns_sw_mac_statistics_start(),
 * protocol_stats_start(), ws_statistics_start(), ws_stack_info_get(), RPL's
 * DODAG information), and two link-time wrappers of the in-process programs
 * (-Wl,--wrap=sw_mac_stats_update,--wrap=protocol_stats_update) for the
 * time-weighted queue depths and the histogram of MAC retries per frame. The
 * wrappers only forward when the statistics are off.
 */
#ifndef NET_STATS_H
#define NET_STATS_H

#include <stdbool.h>
#include <stdint.h>

struct mac_api_s;

struct net_stats_cfg {
    bool on;                    // stats = true
    int period_s;               // stats_period_s: a line this often (0: only at the end)
};

// The in-process MAC (before the interface starts; NULL in split programs).
// With on, the MAC's per-frame confirmations are counted (NET1 mac.uc_*)
void net_stats_set_mac(struct mac_api_s *mac, bool on);
void net_stats_start(const struct net_stats_cfg *cfg, int8_t rcp_if_id, bool is_br, const uint8_t eui64[8]);

#endif
