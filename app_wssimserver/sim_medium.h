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

#include "sim_proto.h"

struct med_frame;
struct med_lock;

// Options, before med_start()
void med_set_mode(const char *arg);             // --medium=legacy|central
void med_set_tx_ref(const char *arg);           // --medium-tx-ref=DBM
void med_set_lock_snr(const char *arg);         // --medium-lock-snr=DB
void med_set_capture_db(const char *arg);       // --medium-capture-db=DB
void med_set_log(const char *path);             // --medium-log=FILE
bool med_options_given(void);                   // any of the above
bool med_tuning_given(void);                    // any of them but --medium
void med_default_central(void);                 // --vtime: central unless --medium=legacy
bool med_central(void);

// The run. cca_shm: the "/wssimcca" table (SIM_SHM_CCA_STRIDE bytes per
// node); links: the "/wssimnodesinfos" table.
void med_start(uint64_t seed, const uint8_t *cca_shm, struct node_infos *links);
void med_finish(void);

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

// A verdict event was popped at now: true if *v must be sent to the node.
// The lock is freed either way.
bool med_verdict(struct med_lock *l, uint64_t now, struct sim_verdict *v);
// A verdict event that is dropped (its node is stopped): free the lock
void med_lock_free(struct med_lock *l);

// "xs": the energy on chan at node now, in dBm
float med_sense(int node, uint16_t chan, uint64_t now);

#endif
