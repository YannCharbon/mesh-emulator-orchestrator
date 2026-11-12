/* SPDX-License-Identifier: Apache-2.0
 *
 * External interferers of the central medium (wssimserver --vtime, central
 * medium): other users of the band that are not emulated nodes, such as
 * another network, duty-cycled devices (LoRa and the like) or a constant
 * noise rise. docs/MEDIUM.md §13.
 *
 * Each interferer has channels, a received level per node, and a time
 * pattern (constant, periodic, Gilbert-Elliott on/off, or bursts of fixed
 * length with random gaps). Its on-intervals are generated lazily in virtual
 * time from its own seeded stream, so that a run is reproducible whatever
 * the order of the medium's queries. The medium adds its power to the
 * interference over frames, the lock threshold, CCA energy and sniffing.
 *
 * This file also dispatches the command-line options of the interference
 * and spectrum work (intf_option()), including the medium's options that
 * belong to it (adjacent-channel table, smooth capture, sniffing extras).
 */
#ifndef SIM_INTERF_H
#define SIM_INTERF_H

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

// The long options handled by intf_option(), for the getopt table: each
// takes an argument
extern const char *const intf_option_names[];
// One of intf_option_names with its argument
void intf_option(const char *name, const char *arg);
// The usage lines of these options
void intf_usage(FILE *stream);
// Any option of intf_option() given (they need the central medium)
bool intf_options_given(void);

/* Position-based levels. A propagation model (node positions, path loss)
 * registers this hook before intf_start(); an interferer with pos= then
 * gets, at each node with a position, the level the hook returns for a
 * transmitter at pos with eirp_dbm. Returns false if the node has no
 * position (the node then gets the interferer's level= value, if any). */
typedef bool (*intf_pos_fn)(int node, const double pos[3], float eirp_dbm, float *dbm);
void intf_set_position_model(intf_pos_fn fn);

void intf_start(uint64_t seed);
void intf_finish(void);
// At least one interferer is defined
bool intf_any(void);
// Time goes forward: on-intervals that ended long before now can be freed
void intf_advance(uint64_t now);

// Power of the external interferers at node on chan at time t, in mW:
// *fsk the part that interferes as a frame of the band's own modulation
// (kind=fsk), *noise the noise-like part (kind=noise). Both 0 if none.
void intf_power(int node, uint16_t chan, uint64_t t, double *fsk, double *noise);

// Each on-interval of an external interferer at node on chan that overlaps
// [from, to), clipped to it, with the power that falls on chan (mW), whether
// it is noise-like, and for an FSK interferer the end of the preamble it
// sends at the start of each burst (0: none, or the burst started before
// from) and its data rate.
struct intf_piece {
    uint64_t start, end;
    double mw;
    bool noise;
    uint64_t pre_end;
    uint32_t rate_bps;
    int src;                    // the interferer's index
};
typedef void (*intf_cb)(void *ctx, const struct intf_piece *p);
void intf_each(int node, uint16_t chan, uint64_t from, uint64_t to, intf_cb cb, void *ctx);

// Highest external power (mW) at node on chan over [from, to), 0 if none
double intf_max_mw(int node, uint16_t chan, uint64_t from, uint64_t to);

#endif
