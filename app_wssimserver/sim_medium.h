/* SPDX-License-Identifier: Apache-2.0
 *
 * Central radio medium of the conductor (wssimserver --vtime
 * --medium=central): docs/MEDIUM.md. It holds the frames on air, decides at
 * each frame's start which receivers lock onto it (§3), and at the frame's end
 * whether each locked receiver gets it (SINR over the frame's segments, one
 * draw from the receiver's seeded stream, §2). It also answers the radios'
 * CCA energy queries ("xs").
 *
 * The conductor owns the event queue: it calls med_frame_start() and
 * med_decide() when a frame is sent, queues the deliveries and the verdict
 * events, and calls med_verdict() when a verdict event is popped.
 */
#ifndef SIM_MEDIUM_H
#define SIM_MEDIUM_H

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include "sim_proto.h"

struct med_frame;
struct med_lock;

// Options, before med_start()
void med_set_mode(const char *arg);             // --medium=legacy|central
void med_set_tx_ref(const char *arg);           // --medium-tx-ref=DBM
void med_set_tx_max(const char *arg);           // --medium-tx-max=DBM|profile|off
int med_tx_max(float *dbm);                     // 1 a value (*dbm), 0 profile, -1 off
void med_set_lock_snr(const char *arg);         // --medium-lock-snr=DB
void med_set_model(const char *arg);            // --medium-model=fg25|em4
void med_set_capture_db(const char *arg);       // --medium-capture-db=[MODE:]DB
void med_set_int_curve(const char *arg);        // --medium-int-curve=[MODE:]X0,B|noise
void med_set_capture_early(const char *arg);    // --medium-capture-early-bits=[MODE:]N|shr
void med_set_capture_sfd(const char *arg);      // --medium-capture-sfd=[MODE:]on|off
void med_set_break_db(const char *arg);         // --medium-break-db=[MODE:]DB|off
void med_set_adjacent_db(const char *arg);      // --medium-adjacent-db=[MODE:]DB|off
void med_set_log(const char *path);             // --medium-log=FILE
// docs/MEDIUM.md §13, through sim_interf.c's intf_option()
void med_note_option(void);                     // an option that needs the central medium
void med_set_adjacent_table(const char *arg);   // --medium-adjacent-table=[MODE:]DB/DB/...
void med_set_capture_model(const char *arg);    // --medium-capture-model=[MODE:]step|smooth
void med_set_capture_smooth(const char *arg);   // --medium-capture-smooth=[MODE:]KEY=V,...
void med_set_int_curve_presync(const char *arg); // --medium-int-curve-presync=[MODE:]X0,B|same
void med_set_sniff_adjacent(const char *arg);   // --sniff-adjacent=off|on|detect
void med_set_sniff_energy(const char *arg);     // --sniff-energy=DBM|off
void med_set_sniff_energy_us(const char *arg);  // --sniff-energy-us=N
void med_set_sniff_false(const char *arg);      // --sniff-false=P|RATE/s
// The share of the power of a frame at this rate that falls d channels away
// (1 for d = 0), from the profile's adjacent-channel table; valid from med_start()
double med_adjacent_factor(uint32_t rate_bps, int d);
bool med_options_given(void);                   // any of the above
// The preset in force (MEDIUM.md §14) and the options it added, printed at the start
void med_set_preset_note(const char *name, const char *how, const char *opts);
bool med_tuning_given(void);                    // any of them but --medium
void med_default_central(void);                 // --vtime: central unless --medium=legacy
bool med_central(void);

// The run. cca_shm: the "/wssimcca" table (SIM_SHM_CCA_STRIDE bytes per
// node); links: the "/wssimnodesinfos" table.
void med_start(uint64_t seed, const uint8_t *cca_shm, struct node_infos *links);
void med_finish(void);
// The outcome totals so far, " ok=N noise=N ..." (scenario counters)
int med_outcomes_str(char *buf, int len);
// Per receiver " ID:HEARD:COLLISIONS:OK" for every node that heard a frame
// while listening (scenario control "rxcounters")
void med_rx_counters(FILE *f);

// A frame from src starts now. Returns NULL (with a warning) if its header
// cannot give an airtime. The frame stays on air until its end; the caller
// adds its receivers, in ascending node order, then decides each one.
struct med_frame *med_frame_start(int src, const struct sim_frame_hdr *hdr, uint64_t now);
uint32_t med_frame_id(const struct med_frame *f);
uint64_t med_frame_end(const struct med_frame *f);
void med_add_rx(struct med_frame *f, int node);
int med_num_rx(const struct med_frame *f);
int med_rx_node(const struct med_frame *f, int i);
// Receiver i of f (§3): the lock if it locks onto f, else NULL; *rx_dbm is the
// power it gets.
struct med_lock *med_decide(struct med_frame *f, int i, uint64_t now, float *rx_dbm);

// Half duplex: node starts to send now (before its frame's med_frame_start())
void med_tx_start(int node, uint64_t now);
// The node ran after its "xx" of frame_id was delivered: from now on its
// radio state in shared memory tells whether it still receives
void med_delivered(int node, uint32_t frame_id);
// The node was stopped (--vtime-kill): its lock ends ("left")
void med_node_stopped(int node, uint64_t now);

// After med_decide(): the lock whose sync search the frame just broke (§3),
// or NULL. Its receiver must get its failed verdict now: queue a verdict
// event at now for it (a second event; the one at its frame's end frees it).
struct med_lock *med_take_broken(void);

// A verdict event was popped at now: true if *v must be sent to the node.
// The lock is freed either way.
bool med_verdict(struct med_lock *l, uint64_t now, struct sim_verdict *v);
// A verdict event that is dropped (its node is stopped): free the lock
void med_lock_free(struct med_lock *l);

// "xs": the energy on chan at node now, in dBm
float med_sense(int node, uint16_t chan, uint64_t now);

/* ---- Preamble sniffing (docs/MEDIUM.md §10) */

// Options, before med_start()
void med_set_sniff_detect(const char *arg);     // --sniff-detect=preamble|frame
void med_set_sniff_dwell(const char *arg);      // --sniff-dwell-bits=N
void med_set_sniff_threshold(const char *arg);  // --sniff-threshold=DBM|off
void med_set_sniff_curve(const char *arg);      // --sniff-curve=ber:N|SINR:P,...
void med_set_sniff_sfd(const char *arg);        // --sniff-sfd-bits=N
void med_set_sniff_margin(const char *arg);     // --sniff-margin-bits=N
void med_set_sniff_settle(const char *arg);     // --sniff-settle-us=N
void med_set_sniff_log(const char *path);       // --sniff-log=FILE

// "xl": like med_frame_start(), with preamble_us of preamble before the frame
struct med_frame *med_frame_start_lp(int src, const struct sim_frame_hdr *hdr, uint64_t now, uint32_t preamble_us);

#define MED_SNIFF_NONE UINT64_MAX
// "xn" from node now. Returns the serial of the new sniff (for its check
// events); *next is the time of its first check.
uint32_t med_sniff_start(int node, const struct sim_sniff *req, uint64_t now, uint64_t *next);
// Whether the receiver of sniff serial is on by now: true once per sniff,
// with its channel, for the power ledger to record LISTEN now. It is on at
// the request, or when the node's own frame ends (half duplex): ask at once
// after med_sniff_start(), and at each check event.
bool med_sniff_rx_on(int node, uint32_t serial, uint64_t now, uint16_t *chan);
// Receiver i of f (after med_decide()): if it sniffs on f's channel, f joins
// the sniff. Returns the time of a check to queue, or MED_SNIFF_NONE.
uint64_t med_sniff_frame(struct med_frame *f, int i, uint32_t *serial);
// A check event of sniff serial at node, popped at now. Returns true when the
// sniff ends now with *res to send; then, if *lock is set, the receiver was
// locked onto the frame: *msg (*len bytes, an "xx" with its medium fields
// set but rx_dbm) is delivered with *rx_dbm and the verdict is due at
// med_lock_end(*lock). Returns false otherwise, with *next the time of the
// next check (MED_SNIFF_NONE: none).
bool med_sniff_check(int node, uint32_t serial, uint64_t now, struct sim_sniff_result *res,
                     struct med_lock **lock, const void **msg, int *len, float *rx_dbm, uint64_t *next);
uint64_t med_lock_end(const struct med_lock *l);
// The radio state the ledger records when a sniff ends now with nothing
// detected: the request's end state, or SIM_RADIO_STATES (no change) if the
// receiver holds a lock, or its own frame is still on air
uint8_t med_sniff_end_state(int node, uint64_t now);
// The channel of node's sniff in progress, or -1
int med_sniff_channel(int node);
// The node's radio left its sniff (a radio report turned it off or away):
// the sniff ends now, with no "xd" (outcome "left")
void med_sniff_cancel(int node, uint64_t now);
// The node stops receiving the frame it is locked onto, now ("left", no
// verdict), as med_decide() does for a radio that left RECEIVE
void med_rx_leave(int node, uint64_t now);

#endif
