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
#include <stdio.h>

#include "sim_proto.h"
#include "ns_port_sim.h"

// Options, before pwr_start()
void pwr_set_profile(const char *path);         // --power: every node's profile
void pwr_add_node_profile(const char *spec);    // --power-node=IDS:PROFILE (IDS: 3, 2-5, 1,4)
void pwr_set_mode(const char *spec);             // --power-mode=[IDS:]MODE: auto, common or a PHY mode
void pwr_set_out(const char *path);             // --power-out (default power.json)
void pwr_set_log(const char *path);             // --power-log: every state interval
void pwr_set_cell(const char *spec);            // --power-cell=MAH[:VOLT[:EFF]]
bool pwr_enabled(void);

// The run: nodes are those connected, ascending
void pwr_start(int num_nodes, const int *nodes);
void pwr_power_up(int node, uint64_t t);   // --vtime-power-up: the node's ledger starts at t, not 0
void pwr_wake(int node, uint64_t t, unsigned int frames);  // "xa" (and the welcome, at 0)
void pwr_radio(int node, uint64_t t, const struct sim_radio_report *r);
void pwr_wake_report(int node, const struct nsim_wake *w);
void pwr_idle(int node, uint64_t t, uint64_t next_us);     // "xi"
void pwr_finish(uint64_t t_end_us, uint64_t seed);
// Scenarios (docs/SCENARIOS.md): a node stopped by the driver (its radio off
// from t; it is then charged as asleep), and a snapshot of every node's
// charge so far, one JSON line appended to f:
// {"tag":..,"t_us":..,"nodes":[[id,charge_uc,mcu_uc,listen_uc,receive_uc,tx_uc,other_uc],...]}
void pwr_node_off(int node, uint64_t t);
void pwr_snapshot(FILE *f, const char *tag, uint64_t t);
// The node's CCA duration in µs (its profile's cca_us, for its --power-mode
// or the common value), rounded; 0 without power recording. Sent in a radio
// interface client's welcome (docs/RADIO_INTERFACE.md §4.3)
uint32_t pwr_cca_us(int node);
double pwr_tx_max_dbm(int node);   // the node's TX table top (PA maximum), NAN without a profile

#endif
