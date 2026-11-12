/* SPDX-License-Identifier: Apache-2.0
 *
 * Propagation from node positions (docs/MEDIUM.md §11): the mean received
 * power of each link from a positions file and a path-loss model, static
 * log-normal shadowing seeded from the run's seed, and the hearing graph
 * derived from it. Also the error of the RSSI reported to the receivers
 * (§11.6), separate from the true power the medium uses.
 *
 * With none of its options, nothing here runs and the server behaves as
 * before.
 */
#ifndef SIM_PROPAGATION_H
#define SIM_PROPAGATION_H

#include <getopt.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include "sim_proto.h"

// Options: getopt_long entries (values from PROP_OPT_BASE), to append to the
// server's own table; prop_option() handles one, returns false if not ours.
#define PROP_OPT_BASE 0x1000
extern const struct option prop_long_options[];
bool prop_option(int opt, const char *arg);
void prop_print_help(FILE *stream);
// After parsing: refuse combinations that cannot work (central: the central
// medium is in use)
void prop_check_options(bool central);

bool prop_enabled(void);        // --positions given

// Once, after the -g options: positions, model, shadowing, hearing graph.
// given[rx * SIM_MAX_NODES + tx] != 0 for the directed pairs whose value a -g
// option set; links: the RSSI map (written for the modelled pairs, at the
// nominal TX power); graph: the server's hearing bitmap (pairs added).
void prop_build(uint64_t (*graph)[SIM_MAX_NODES / 64], struct node_infos *links, const uint8_t *given);

// The mean level at node of an external interferer at pos (x, y, z in m)
// with eirp_dbm (docs/MEDIUM.md §13.1); false if node has no position. The
// hook of sim_interf.h (intf_set_position_model()).
bool prop_point_dbm(int node, const double pos[3], float eirp_dbm, float *dbm);

// Whether the power of tx's frames at rx comes from the model (both nodes
// positioned, no -g value, not changed by --vtime-link)
bool prop_modelled(int rx, int tx);
// The mean received power at rx of a frame tx sends at tx_dbm (§11.2)
float prop_link_dbm(int rx, int tx, int8_t tx_dbm);
// Both nodes are positioned (their pair may be below the cutoff)
bool prop_positioned(int a, int b);
// --vtime-link changed the pair a-b: its value is no longer the model's
void prop_override(int a, int b);

// Reported RSSI (§11.6): the value delivered to rx for frame frame_id whose
// true received power is dbm. Returns dbm unchanged without the options.
bool prop_rssi_error(void);
float prop_reported_dbm(int rx, uint32_t frame_id, float dbm);

#endif
