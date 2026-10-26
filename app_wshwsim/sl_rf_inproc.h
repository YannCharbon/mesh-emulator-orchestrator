/* SPDX-License-Identifier: Apache-2.0
 *
 * In-process emulator nodes (wsnode-inproc, wsbrd-inproc): the MAC runs in
 * the same process as the stack, on the simulated radio of sl_rf_driver.c
 * (nanostack-generic-standalone GENERICITY_PLAN.md §3.3 and §4.2, step S5).
 */
#ifndef SL_RF_INPROC_H
#define SL_RF_INPROC_H

#include <stdint.h>

struct mac_api_s;

// Remove "--eui64=ADDR" (or "--eui64 ADDR") from the command line and parse
// it. Exits if it is missing.
void rf_inproc_take_eui64(int *argc, char *argv[], uint8_t eui64[8]);

// Connect to wssimserver, register the simulated radio with this EUI-64 and
// create the 802.15.4 MAC on it, as wshwsim does. Returns the MAC.
struct mac_api_s *rf_inproc_init(const char *sim_socket, const uint8_t eui64[8]);

// Add the radio's socket to ns_port_run().
void rf_inproc_add_to_run_loop(void);

#endif
