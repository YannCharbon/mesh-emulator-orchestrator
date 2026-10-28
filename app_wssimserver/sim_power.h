/* SPDX-License-Identifier: Apache-2.0
 *
 * Power ledger of the conductor (wssimserver --vtime --power=PROFILE):
 * docs/POWER_MODEL.md. Per node, it follows the MCU (active, light sleep,
 * deep sleep) from the conductor protocol ("xa", "xc", "xi") and the radio
 * from the "xr" reports, charges every state and transition with the node's
 * power profile, and writes the totals at the end of the run.
 *
 * Stack-neutral: it knows only the conductor protocol, so any program that
 * speaks it (EM7's clients too) gets the same accounting.
 */
#ifndef SIM_POWER_H
#define SIM_POWER_H

#include <stdbool.h>
#include <stdint.h>

#include "sim_proto.h"
#include "ns_port_sim.h"

// Options, before pwr_start()
void pwr_set_profile(const char *path);         // --power: every node's profile
void pwr_add_node_profile(const char *spec);    // --power-node=IDS:PROFILE (IDS: 3, 2-5, 1,4)
void pwr_set_out(const char *path);             // --power-out (default power.json)
void pwr_set_log(const char *path);             // --power-log: every state interval
void pwr_set_cell(const char *spec);            // --power-cell=MAH[:VOLT[:EFF]]
bool pwr_enabled(void);

// The run: nodes are those connected, ascending
void pwr_start(int num_nodes, const int *nodes);
void pwr_wake(int node, uint64_t t, unsigned int frames);  // "xa" (and the welcome, at 0)
void pwr_radio(int node, uint64_t t, const struct sim_radio_report *r);
void pwr_wake_report(int node, const struct nsim_wake *w);
void pwr_idle(int node, uint64_t t, uint64_t next_us);     // "xi"
void pwr_finish(uint64_t t_end_us, uint64_t seed);

#endif
