/* SPDX-License-Identifier: Apache-2.0
 *
 * What wssimserver and the simulated radios (app_wshwsim/sl_rf_driver.c)
 * share: the socket messages and the shared-memory tables.
 *
 * Each socket message is one SOCK_SEQPACKET message. A radio sends a hello
 * with its node ID right after it connects, then frames. The server numbers
 * nodes by that ID, not by connection order, and stamps the sender's ID on
 * each frame it relays.
 *
 * Virtual time (docs/VIRTUAL_TIME.md): with --vtime, wssimserver is also the
 * conductor. The nodes are built on the simulation port, set SIM_HELLO_VTIME
 * in their hello, and the conductor messages of ns_port_sim.h (in
 * nanostack-generic-standalone/port/sim/) travel on the same socket.
 *
 * Programs that are not built on Nanostack use the stack-neutral subset of
 * these messages specified in docs/RADIO_INTERFACE.md (SIM_HELLO_RADIO_IF).
 */
#ifndef SIM_PROTO_H
#define SIM_PROTO_H

#include <stdint.h>
#include <stdio.h>
#include <string.h>

// Node IDs go from 0 to SIM_MAX_NODES - 1. A node's ID is the last two bytes
// of its EUI-64, big-endian (01:02:03:04:05:06:01:2C is node 300).
#define SIM_MAX_NODES 4096

static inline int sim_node_id(const uint8_t eui64[8])
{
    return eui64[6] << 8 | eui64[7];
}

#define SIM_HELLO_VTIME 0x0001  // the node runs on virtual time
// The node is a client of the stack-neutral radio interface
// (docs/RADIO_INTERFACE.md): it writes no shared memory, its radio state comes
// from its "xr" reports, and the server checks its frames against that state.
// Needs SIM_HELLO_VTIME and the central medium; version says which interface.
#define SIM_HELLO_RADIO_IF 0x0002
#define SIM_HELLO_KNOWN (SIM_HELLO_VTIME | SIM_HELLO_RADIO_IF)

// Versions of the radio interface this server speaks (hello's version).
// Version 2 adds preamble sniffing ("xn", "xd", "xl"); a version-1 client
// gets the same server behaviour as before, and its "xn" and "xl" are dropped.
#define SIM_RADIO_IF_VERSION_MIN 1
#define SIM_RADIO_IF_VERSION_MAX 2

// Welcome flag for clients of the radio interface, next to the conductor's
// NSIM_WELCOME_* (ns_port_sim.h, which leaves this bit unused): the server
// serves "xn" and "xl" for this client (interface version 2 or later, central
// medium). Never set in a Nanostack node's welcome.
#define SIM_WELCOME_SNIFF 0x0004

struct sim_hello {
    char magic[2];              // "xh"
    uint16_t node_id;
    uint16_t flags;             // SIM_HELLO_*
    uint16_t version;           // with SIM_HELLO_RADIO_IF: the interface version; else 0
};

struct sim_frame_hdr {
    char magic[2];              // "xx"
    uint16_t len;               // bytes of frame after this header
    uint16_t chan;
    uint16_t seq;               // per-sender sequence number, for traces
    uint16_t src;               // sender's node ID, set by the server
    // What the PHY sends around the frame, which the receiver hears too
    // (docs/POWER_MODEL.md §12.4): the sender's SHR, preamble and SFD, in
    // bytes, and its FCS, 2 or 4 bytes (on air, the PHR's FCS type bit).
    uint8_t shr_len;
    uint8_t fcs_len;
    // Central medium (docs/MEDIUM.md). The sender's radio fills these three
    // in both modes; the airtime at the medium is
    // (shr_len + phr_len + len + fcs_len) * 8 * 10^6 / rate_bps, in whole µs.
    uint8_t phr_len;
    int8_t tx_dbm;              // TX power the MAC asked for
    uint8_t flags;              // SIM_FRAME_*, set by the server
    uint8_t reserved;
    uint32_t rate_bps;
    // Set by the server in central mode only (0 in legacy mode)
    uint32_t frame_id;          // run-wide, from 1
    float rx_dbm;               // received power at this receiver
};

#define SIM_FRAME_MEDIUM 0x01   // the medium locked this receiver onto the frame: wait for its "xv"
#define SIM_FRAME_LONG_PREAMBLE 0x02 // the frame was sent with "xl": a long preamble came before it

// Central medium: outcome of a frame at a receiver ("xv" and --medium-log)
enum sim_rx_cause {
    SIM_RX_OK,
    SIM_RX_NOISE,               // lost, no interferer during the frame
    SIM_RX_INTERFERENCE,        // lost, with at least one interferer
    SIM_RX_CAUSES
};

// "xv": the end of a frame the receiver was locked onto, at the frame's end
struct sim_verdict {
    char magic[2];              // "xv"
    uint8_t ok;
    uint8_t cause;              // enum sim_rx_cause
    uint32_t frame_id;
    float rx_dbm;
    float sinr_db;              // lowest over the frame's segments
};

// "xs": energy on a channel (CCA). The radio sends it during its run; the
// conductor answers at once with the same message, dbm filled in.
struct sim_sense {
    char magic[2];              // "xs"
    uint16_t channel;
    float dbm;                  // noise + every frame on air at the radio on that channel
};

// Preamble sniffing (docs/MEDIUM.md §10), central medium only.
//
// "xl": a frame with a long wake-up preamble. The sender's radio sends it
// instead of "xx", during its run: preamble_us of preamble on air, then the
// frame (its own SHR, PHR, PSDU and FCS), as one transmission. A complete "xx"
// message follows the struct. Receivers that lock onto it get that "xx",
// with SIM_FRAME_LONG_PREAMBLE in its flags.
struct sim_lp_frame {
    char magic[2];              // "xl"
    uint16_t reserved;
    uint32_t preamble_us;       // preamble before the frame's own SHR, in µs
};

// "xn": the radio's receiver is on, on channel, from now for duration_us,
// looking for a preamble. Sent during the node's run, not answered at once:
// the conductor sends one "xd" when it detects a frame or at the window's
// end. A new "xn" replaces a sniff in progress (no "xd" for the old one); a
// frame the node sends ends it (no "xd").
#define SIM_SNIFF_LOCK 0x01     // on detection, lock the receiver onto the frame if it can still be received
struct sim_sniff {
    char magic[2];              // "xn"
    uint16_t channel;
    uint32_t duration_us;
    uint32_t sniff_id;          // the node's own tag, echoed in "xd"
    uint8_t flags;              // SIM_SNIFF_*
    uint8_t end_state;          // enum sim_radio_state at a window's end with nothing detected (power ledger)
    uint16_t reserved;
};

// "xd": the outcome of a sniff, at the detection time or at the window's end.
// locked: SIM_SNIFF_LOCKED, the medium locked the receiver onto the frame
// now and its "xx" (SIM_FRAME_MEDIUM) follows in the same wake;
// SIM_SNIFF_RECEIVING, the receiver was already locked onto it (its "xx"
// came at its start); 0, no lock (SIM_SNIFF_LOCK not asked, or the frame can
// no longer be received).
#define SIM_SNIFF_LOCKED 1
#define SIM_SNIFF_RECEIVING 2
struct sim_sniff_result {
    char magic[2];              // "xd"
    uint8_t detected;
    uint8_t locked;             // 0, SIM_SNIFF_LOCKED or SIM_SNIFF_RECEIVING
    uint32_t sniff_id;
    uint32_t frame_id;          // the frame detected, 0 if none
    uint16_t src;               // its sender, 0 if none
    uint16_t reserved;
    uint64_t t_us;              // detection time, or the window's end if none
    float rssi_dbm;             // the frame's received power; if none, the energy on the channel at the end
    float sinr_db;              // lowest SINR over the detecting dwell (0 if none)
};

// Power recording (docs/POWER_MODEL.md), virtual time only: when the welcome
// has NSIM_WELCOME_POWER, the radio reports each change of its state with
// "xr", between the node's wake and its "xi" (at the time of that wake). The
// states are the hardware's, not the driver's: the conductor's power ledger
// gives each one a current from the node's power profile.
enum sim_radio_state {
    SIM_RADIO_OFF,              // powered down
    SIM_RADIO_IDLE,             // ready, neither receiving nor sending
    SIM_RADIO_LISTEN,           // RX on, no frame (also during CSMA backoffs)
    SIM_RADIO_RECEIVE,          // RX of a frame, from its start to its end
    SIM_RADIO_TX,               // sending, at tx_dbm
    SIM_RADIO_STATES
};

#define SIM_RADIO_EV_CCA 0x01   // a CCA ended now (it is not a state of the driver)

// FSK PHY modes, numbered as Wi-SUN FAN 1.1's PhyModeId (1 = mode 1a ...
// 8 = mode 5). The conductor's ledger picks the profile's values for the mode
// (docs/POWER_MODEL.md §6.2). 0: not configured yet, or not one of these.
enum sim_phy_mode {
    SIM_PHY_MODE_NONE,
    SIM_PHY_MODE_1A,            // 2-FSK 50 kbit/s, h = 0.5
    SIM_PHY_MODE_1B,            // 50 kbit/s, h = 1
    SIM_PHY_MODE_2A,            // 100 kbit/s, h = 0.5
    SIM_PHY_MODE_2B,            // 100 kbit/s, h = 1
    SIM_PHY_MODE_3,             // 150 kbit/s, h = 0.5
    SIM_PHY_MODE_4A,            // 200 kbit/s, h = 0.5
    SIM_PHY_MODE_4B,            // 200 kbit/s, h = 1
    SIM_PHY_MODE_5,             // 300 kbit/s, h = 0.5
    SIM_PHY_MODES
};

struct sim_radio_report {
    char magic[2];              // "xr"
    uint8_t state;              // enum sim_radio_state
    int8_t tx_dbm;              // TX power the MAC asked for
    uint8_t events;             // SIM_RADIO_EV_*
    uint8_t phy_mode;           // SIM_PHY_MODE_*: the PHY mode the MAC configured, 0 until then
    uint16_t channel;
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

// Shared memory "/wssimcca": SIM_SHM_CCA_STRIDE bytes per node: the driver's
// state (read by the neighbours' legacy CCA), the channel (2 bytes), then the
// radio's hardware state (enum sim_radio_state, read by the central medium).
#define SIM_SHM_CCA_STRIDE 4
#define SIM_SHM_CCA_SIZE (SIM_MAX_NODES * SIM_SHM_CCA_STRIDE)

// Shared-memory names belong to one server socket, so that several runs can
// share a PC. The default socket keeps the historical names.
#define SIM_DEFAULT_SOCKET "/tmp/sim_socket"

static inline void sim_shm_name(char *buf, size_t size, const char *base, const char *sock_path)
{
    const char *p;
    size_t n;

    if (!strcmp(sock_path, SIM_DEFAULT_SOCKET)) {
        snprintf(buf, size, "/%s", base);
        return;
    }
    n = snprintf(buf, size, "/%s.", base);
    if (n >= size)
        n = size - 1;
    for (p = sock_path; *p && n + 1 < size; p++)
        buf[n++] = (*p == '/') ? '_' : *p;
    buf[n] = '\0';
}

#endif
