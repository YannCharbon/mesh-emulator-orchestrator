/*
 * License: GPLv2
 * Created: 2021-05-19 10:27:04
 * Copyright 2021, Silicon Labs
 * Main authors:
 *    - Jérôme Pouiller <jerome.pouiller@silabs.com>
 */
#ifndef SL_RF_DRIVER_H
#define SL_RF_DRIVER_H

#include <stdint.h>

#include "nanostack/mac/fhss_ws_extension.h"

typedef enum phy_link_type_e phy_link_type_e;

struct neighbor_timings {
    uint8_t eui64[8];
    struct fhss_ws_neighbor_timing_info val;
};

// What the simulated radio needs from the program it runs in: wshwsim (the
// MAC behind Spinel), or a node that runs the MAC in-process
// (sl_rf_inproc.c). Fill it before virtual_rf_device_register().
struct rf_sim_host {
    int rf_fd;                          // connection to wssimserver
    void *pcap_dumper;                  // pcap_dumper_t *, or NULL
    // The CCA looks at the radio state of these nodes (shared memory)
    struct neighbor_timings *neighbors;
    int neighbors_count;
};
extern struct rf_sim_host g_rf_host;

// Connect to wssimserver; returns the socket.
int rf_sim_connect(const char *path);
int8_t virtual_rf_device_register(phy_link_type_e link_type, uint16_t mtu_size);
// Call when g_rf_host.rf_fd is readable.
void phy_rf_rx_now(void);

#endif
