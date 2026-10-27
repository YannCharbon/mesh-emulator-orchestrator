/* SPDX-License-Identifier: Apache-2.0
 *
 * In-process emulator nodes: see sl_rf_inproc.h.
 */
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#include "nanostack/mac/mac_api.h"
#include "nanostack/mac/sw_mac.h"
#include "nanostack/mac/fhss_ws_extension.h"
#include "nanostack/mac/platform/arm_hal_phy.h"
#include "nanostack/net_interface.h"
#include "common/os_scheduler.h"
#include "common/utils.h"
#include "common/log.h"
#include "sl_rf_driver.h"
#include "sl_rf_inproc.h"
#ifdef NS_PORT_SIM
#include <time.h>
#include "ns_port/time.h"
#endif

// The same table and sizes as wshwsim (sl_wsrcp.h, g_storage_sizes).
static struct neighbor_timings neighbors[255];
static mac_description_storage_size_t storage_sizes = {
    .device_description_table_size = ARRAY_SIZE(neighbors),
    .key_description_table_size = 4,
    .key_lookup_size = 1,
    .key_usage_size = 3,
};

void rf_inproc_take_eui64(int *argc, char *argv[], uint8_t eui64[8])
{
    const char *str = NULL;
    int i, j, n = 1;
    int ret;

    for (i = 1; i < *argc; i++) {
        if (!strncmp(argv[i], "--eui64=", 8)) {
            str = argv[i] + 8;
        } else if (!strcmp(argv[i], "--eui64") && i + 1 < *argc) {
            str = argv[++i];
        } else {
            argv[n++] = argv[i];
            continue;
        }
    }
    for (j = n; j < *argc; j++)
        argv[j] = NULL;
    *argc = n;
    FATAL_ON(!str, 1, "missing --eui64=ADDR");
    ret = sscanf(str, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx:%hhx:%hhx%*c",
                 &eui64[0], &eui64[1], &eui64[2], &eui64[3],
                 &eui64[4], &eui64[5], &eui64[6], &eui64[7]);
    FATAL_ON(ret != 8, 1, "malformated EUI64");
}

struct mac_api_s *rf_inproc_init(const char *sim_socket, const uint8_t eui64[8])
{
    struct mac_api_s *mac;
    int8_t rf_id;

    g_rf_host.rf_fd = rf_sim_connect(sim_socket, eui64);
    g_rf_host.neighbors = neighbors;
    g_rf_host.neighbors_count = ARRAY_SIZE(neighbors);
    rf_id = virtual_rf_device_register(PHY_LINK_15_4_SUBGHZ_TYPE, 2043);
    FATAL_ON(rf_id < 0, 2, "virtual_rf_device_register: %d", rf_id);
    arm_net_phy_mac64_set((uint8_t *)eui64, rf_id);
    mac = ns_sw_mac_create(rf_id, &storage_sizes);
    FATAL_ON(!mac, 2, "ns_sw_mac_create");
    return mac;
}

static void rf_inproc_rx(void)
{
    phy_rf_rx_now();
}

void rf_inproc_add_to_run_loop(void)
{
    ns_port_posix_add_fd(g_rf_host.rf_fd, false, rf_inproc_rx, NULL);
}

// The CCA of sl_rf_driver.c looks at the radio state of the neighbours the
// stack reports with ns_fhss_ws_update_neighbor(). In split mode these calls
// reach wshwsim over Spinel and fill its table (sl_wsrcp_mac.c). In-process,
// the stack calls rcp/'s version directly, which does nothing because the
// FHSS reads the neighbours from the stack itself. So the program is linked
// with -Wl,--wrap for these two functions, and the table is kept here the way
// wshwsim keeps it.
void __real_ns_fhss_ws_update_neighbor(const uint8_t eui64[8], fhss_ws_neighbor_timing_info_t *fhss_data);
void __real_ns_fhss_ws_drop_neighbor(const uint8_t eui64[8]);

void __wrap_ns_fhss_ws_update_neighbor(const uint8_t eui64[8], fhss_ws_neighbor_timing_info_t *fhss_data)
{
    struct fhss_ws_neighbor_timing_info *val = NULL;
    int i;

    for (i = 0; i < ARRAY_SIZE(neighbors); i++)
        if (!memcmp(neighbors[i].eui64, eui64, 8))
            val = &neighbors[i].val;
    if (!val) {
        for (i = 0; i < ARRAY_SIZE(neighbors); i++) {
            if (!memcmp(neighbors[i].eui64, "\x00\x00\x00\x00\x00\x00\x00\x00", 8)) {
                val = &neighbors[i].val;
                memcpy(neighbors[i].eui64, eui64, 8);
                break;
            }
        }
        BUG_ON(i == ARRAY_SIZE(neighbors), "full");
    }
    *val = *fhss_data;
    __real_ns_fhss_ws_update_neighbor(eui64, fhss_data);
}

void __wrap_ns_fhss_ws_drop_neighbor(const uint8_t eui64[8])
{
    int i;

    for (i = 0; i < ARRAY_SIZE(neighbors); i++)
        if (!memcmp(neighbors[i].eui64, eui64, 8))
            break;
    BUG_ON(i == ARRAY_SIZE(neighbors), "not found");
    memset(neighbors[i].eui64, 0, 8);
    __real_ns_fhss_ws_drop_neighbor(eui64);
}

#ifdef NS_PORT_SIM
// Linked with -Wl,--wrap=time in virtual-time builds: time() is virtual wall
// time, so that mbedTLS (the TLS hello randoms, certificate dates) and any
// other caller stay reproducible.
time_t __wrap_time(time_t *t)
{
    time_t now = ns_port_time_wall_s();

    if (t)
        *t = now;
    return now;
}
#endif
