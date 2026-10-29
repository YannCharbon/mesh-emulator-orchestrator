/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2021, Silicon Labs
 * Main authors:
 *     - Jérôme Pouiller <jerome.pouiller@silabs.com>
 */
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <getopt.h>
#include <unistd.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/select.h>
#include <sys/time.h>

#include "mbed-client-libservice/ns_trace.h"
#include "nanostack-event-loop/eventOS_event.h"
#include "nanostack-event-loop/eventOS_scheduler.h"
#include "nanostack-event-loop/eventOS_event_timer.h"
#include "nanostack/mac/fhss_api.h"
#include "nanostack/mac/mac_api.h"
#include "nanostack/ns_file_system.h"
#include "nanostack/mac/sw_mac.h"
#include "nanostack/net_ws_test.h"
#include "nanostack/ws_management_api.h"
#include "nanostack/source/6lowpan/ws/ws_common_defines.h"
#include "nanostack/source/core/ns_address_internal.h"

#include "multicast_helper.h"
#include "nanostack/multicast_api.h"

#include "ns_port/critical.h"
#include "ns_port/time.h"
#include "common/bus_uart.h"
#include "common/os_scheduler.h"
#include "common/os_timer.h"
#include "common/slist.h"
#include "common/log.h"
#include "commandline.h"
#include "version.h"
#include "wsbr.h"
#include "wsbr_mac.h"
#ifdef MAC_INPROCESS
#include "common/fhss_timer.h"
#include "app_wshwsim/sl_rf_inproc.h"
#endif

// See warning in wsbr.h
#ifdef MAC_INPROCESS
// wsnode-inproc: the MAC runs in this process (sl_rf_inproc.h).
struct wsbr_ctxt g_ctxt = { };
#else
struct wsbr_ctxt g_ctxt = {
    .mac_api.mac_initialize = wsbr_mac_init,
    .mac_api.mac_mode_switch_resolver_set = wsbr_mac_mode_switch_resolver_set,
    .mac_api.mac_mcps_edfe_enable = wsbr_mac_edfe_ext_init,
    .mac_api.mac_mcps_extension_enable = wsbr_mac_mcps_ext_init,

    .mac_api.mac_storage_sizes_get = wsbr_mac_storage_sizes_get,
    .mac_api.mac64_set = wsbr_mac_addr_set,
    .mac_api.mac64_get = wsbr_mac_addr_get,

    .mac_api.mlme_req = wsbr_mlme,
    .mac_api.mcps_data_req = wsbr_mcps_req,
    .mac_api.mcps_data_req_ext = wsbr_mcps_req_ext,
    .mac_api.mcps_purge_req = wsbr_mcps_purge,

    .mac_api.phyMTU = 2043,
};
#endif


static int get_fixed_channel(uint32_t bitmask[static 8])
{
    int i, j, val = -1;

    for (i = 0; i < 8; i++) {
        for (j = 0; j < 32; j++) {
            if (bitmask[i] & (1 << j)) {
                if (val >= 0)
                    return 0xFFFF;
                val = i * 32 + j;
            }
        }
    }
    return val;
}

static void wsbr_configure_ws(struct wsbr_ctxt *ctxt)
{
    int ret, i;
    int fixed_channel = get_fixed_channel(ctxt->ws_allowed_channels);
    uint8_t channel_function = (fixed_channel == 0xFFFF) ? WS_DH1CF : WS_FIXED_CHANNEL;
    uint8_t *gtks[4] = { };
    bool gtk_force = false;

#ifdef MAC_INPROCESS
    ret = ws_management_node_init(ctxt->rcp_if_id, ctxt->ws_domain,
                                  ctxt->ws_name, (struct fhss_timer *)&ns_port_fhss_timer);
#else
    ret = ws_management_node_init(ctxt->rcp_if_id, ctxt->ws_domain,
                                  ctxt->ws_name, (struct fhss_timer *)-1);
#endif
    WARN_ON(ret);

    WARN_ON(ctxt->ws_domain == 0xFE, "Not supported");
    ret = ws_management_regulatory_domain_set(ctxt->rcp_if_id, ctxt->ws_domain,
                                              ctxt->ws_class, ctxt->ws_mode);
    WARN_ON(ret);

    // Note that calling ws_management_fhss_timing_configure() is redundant
    // with the two function calls bellow.
    ret = ws_management_fhss_unicast_channel_function_configure(ctxt->rcp_if_id, channel_function, fixed_channel,
                                                                WS_FHSS_UC_DWELL_INTERVAL);
    WARN_ON(ret);
    ret = ws_management_fhss_broadcast_channel_function_configure(ctxt->rcp_if_id, channel_function, fixed_channel,
                                                                  WS_FHSS_BC_DWELL_INTERVAL, WS_FHSS_BC_INTERVAL);
    WARN_ON(ret);
    if (fixed_channel == 0xFFFF) {
        ret = ws_management_channel_mask_set(ctxt->rcp_if_id, ctxt->ws_allowed_channels);
        WARN_ON(ret);
    }


    // Note that calls to ws_management_timing_parameters_set() and
    // ws_bbr_rpl_parameters_set() are done by the function below.
    ret = ws_management_network_size_set(ctxt->rcp_if_id, ctxt->ws_size);
    WARN_ON(ret);

    ret = arm_nwk_set_tx_output_power(ctxt->rcp_if_id, ctxt->tx_power);
    WARN_ON(ret);

    // FAN 1.1 LFN (GS8 stage 3): FAN 1.1, its settings; the receiver is off
    // outside its windows
    if (ctxt->lfn) {
        FATAL_ON(ctxt->ws_fan_version != 2, 1, "device_type = lfn: needs fan_version = 1.1");
        ret = ws_management_lfn_settings_set(ctxt->rcp_if_id, &ctxt->lfn_cfg);
        FATAL_ON(ret, 1, "lfn_*: invalid LFN settings (%d)", ret);
        INFO("LFN (FAN 1.1 Limited Function Node)");
    }
    // FAN 1.1 at run time (GS8, LFN study U1); FAN 1.0 stays the default
    if (ctxt->ws_fan_version != 1) {
        ret = ws_management_fan_version_set(ctxt->rcp_if_id, ctxt->ws_fan_version);
        FATAL_ON(ret, 1, "fan_version: not supported by this build (%d)", ret);
        INFO("Wi-SUN FAN version 1.%d", ctxt->ws_fan_version - 1);
    }
    // FAN 1.1 LFN parenting by an FFN router (GS8 stage 5)
    if (ctxt->enable_lfn) {
        FATAL_ON(ctxt->lfn, 1, "enable_lfn: an LFN does not parent LFNs");
        ret = ws_management_lfn_settings_set(ctxt->rcp_if_id, &ctxt->lfn_cfg);
        FATAL_ON(ret, 1, "lfn_*: invalid LFN settings (%d)", ret);
        ret = ws_management_lfn_parent_set(ctxt->rcp_if_id, true);
        FATAL_ON(ret, 1, "enable_lfn: needs fan_version = 1.1 (%d)", ret);
        INFO("LFN parenting on");
    }
    // Radio off when the MAC is idle (LFN study M1): a test of the radio-off
    // path, not an LFN. Applied when the MAC starts.
    if (!ctxt->rx_on_when_idle) {
        ret = ws_management_rx_on_when_idle_set(ctxt->rcp_if_id, false);
        WARN_ON(ret);
        INFO("RX off when idle");
    }

    ret = ws_device_min_sens_set(ctxt->rcp_if_id, 174 - 93);
    WARN_ON(ret);

    ret = arm_network_own_certificate_add(&ctxt->tls_own);
    WARN_ON(ret);

    ret = arm_network_trusted_certificate_add(&ctxt->tls_ca);
    WARN_ON(ret);


    for (i = 0; i < ARRAY_SIZE(ctxt->ws_gtk_force); i++) {
        if (ctxt->ws_gtk_force[i]) {
            gtk_force = true;
            gtks[i] = ctxt->ws_gtk[i];
        }
    }
    if (gtk_force) {
        ret = ws_test_gtk_set(ctxt->rcp_if_id, gtks);
        WARN_ON(ret);
    }
}

// Event of wsbr_tasklet: rx_off_when_idle_after has passed
#define WSROUTER_EVENT_RX_OFF 100

static void wsbr_tasklet(struct arm_event_s *event)
{
    const char *const nwk_events[] = {
        "ARM_NWK_BOOTSTRAP_READY",
        "ARM_NWK_RPL_INSTANCE_FLOODING_READY",
        "ARM_NWK_SET_DOWN_COMPLETE",
        "ARM_NWK_NWK_SCAN_FAIL",
        "ARM_NWK_IP_ADDRESS_ALLOCATION_FAIL",
        "ARM_NWK_DUPLICATE_ADDRESS_DETECTED",
        "ARM_NWK_AUHTENTICATION_START_FAIL",
        "ARM_NWK_AUHTENTICATION_FAIL",
        "ARM_NWK_NWK_CONNECTION_DOWN",
        "ARM_NWK_NWK_PARENT_POLL_FAIL",
        "ARM_NWK_PHY_CONNECTION_DOWN"
    };
    struct wsbr_ctxt *ctxt = &g_ctxt;

    switch (event->event_type) {
        case ARM_LIB_TASKLET_INIT_EVENT:
            // The tasklet that call arm_nwk_interface_configure_*_bootstrap_set()
            // will be used to receive ARM_LIB_NWK_INTERFACE_EVENT.
            if (arm_nwk_interface_configure_6lowpan_bootstrap_set(ctxt->rcp_if_id,
                                                                  ctxt->lfn ? NET_6LOWPAN_HOST : NET_6LOWPAN_ROUTER,
                                                                  NET_6LOWPAN_WS))

                WARN("arm_nwk_interface_configure_6lowpan_bootstrap_set");
            wsbr_configure_ws(ctxt);
            if (arm_nwk_interface_up(ctxt->rcp_if_id))
                 WARN("arm_nwk_interface_up RCP");
            multicast_set_parameters(10,0,1,1,75);
            test_traffic_start(&ctxt->test_traffic);

            if (ctxt->rx_off_after_s >= 0)
                eventOS_event_timer_request(0, WSROUTER_EVENT_RX_OFF, event->receiver,
                                            (uint32_t)ctxt->rx_off_after_s * 1000);
            break;
        case WSROUTER_EVENT_RX_OFF:
            INFO("RX off when idle (rx_off_when_idle_after = %d s)", ctxt->rx_off_after_s);
            WARN_ON(ws_management_rx_on_when_idle_set(ctxt->rcp_if_id, false));
            break;
        case ARM_LIB_NWK_INTERFACE_EVENT:
            if (event->event_id == ctxt->rcp_if_id) {
                DEBUG("get event for ws interface: %s", nwk_events[event->event_data]);
            } else {
                WARN("received unknown network event: %d", event->event_id);
            }
            break;
        default:
            WARN("received unknown event: %d", event->event_type);
            break;
    }
}

#ifndef MAC_INPROCESS
void wsbr_handle_reset(struct wsbr_ctxt *ctxt, const char *version_fw_str)
{
    if (ctxt->reset_done && ctxt->hw_addr_done)
        FATAL(3, "MAC layer has been reset. Operation not supported");
    INFO("Connected to RCP \"%s\" (%d.%d.%d), API %d.%d.%d", version_fw_str,
          FIELD_GET(0xFF000000, ctxt->rcp_version_fw),
          FIELD_GET(0x00FFFF00, ctxt->rcp_version_fw),
          FIELD_GET(0x000000FF, ctxt->rcp_version_fw),
          FIELD_GET(0xFF000000, ctxt->rcp_version_api),
          FIELD_GET(0x00FFFF00, ctxt->rcp_version_api),
          FIELD_GET(0x000000FF, ctxt->rcp_version_api));
    if (fw_api_older_than(ctxt, 0, 2, 0))
        FATAL(3, "RCP API is too old");
    ctxt->reset_done = true;
    wsbr_rcp_get_hw_addr(ctxt);
}
#endif

void kill_handler(int signal)
{
    exit(3);
}

static int logfile = -1;
void mbed_trace_print_and_log(const char *str)
{
    puts(str);
    if (logfile >= 0) {
        char tm[12];
        snprintf(tm, sizeof(tm), "%09d ", (int)ns_port_time_wall_s());
        write(logfile, &tm, sizeof(tm)-1);
        write(logfile, str+5, strlen(str)-9);
        write(logfile, "\n", 1);
    }
}


#ifndef MAC_INPROCESS
static void wsrouter_uart_ready(void)
{
    rcp_rx(&g_ctxt);
}

static bool wsrouter_uart_pending(void)
{
    return g_ctxt.uart.uart_next_frame_ready;
}
#endif

int main(int argc, char *argv[])
{
    struct wsbr_ctxt *ctxt = &g_ctxt;
#ifdef MAC_INPROCESS
    struct mac_api_s *mac;
    uint8_t eui64[8];
#endif
    mbed_trace_print_function_set(&mbed_trace_print_and_log);

    INFO("Silicon Labs Wi-SUN router %s", version_daemon);
    signal(SIGINT, kill_handler);
    signal(SIGHUP, kill_handler);
#ifndef MAC_INPROCESS
    ctxt->rcp_tx = uart_tx;
    ctxt->rcp_rx = uart_rx;
#endif
    mbed_trace_init();
    mbed_trace_config_set(TRACE_ACTIVE_LEVEL_ALL | TRACE_MODE_COLOR);
    platform_critical_init();
    ns_port_init();
    eventOS_scheduler_init();
#ifdef MAC_INPROCESS
    rf_inproc_take_eui64(&argc, argv, eui64);
#endif
    parse_commandline(ctxt, argc, argv, print_help_node);
    if (memcmp(ctxt->ipv6_prefix, ADDR_UNSPECIFIED, 16))
        WARN("ipv6_prefix is ignored");
#ifdef MAC_INPROCESS
    // -u names the wssimserver socket instead of the RCP's UART.
    mac = rf_inproc_init(ctxt->uart_dev, eui64);
    memcpy(ctxt->hw_mac, eui64, sizeof(ctxt->hw_mac));
    memcpy(ctxt->dynamic_mac, ctxt->hw_mac, sizeof(ctxt->dynamic_mac));

    if (net_init_core())
        BUG("net_init_core");

    ctxt->rcp_if_id = arm_nwk_interface_lowpan_init(mac, "ws0");
#else
    ctxt->uart.data_fd = uart_open(ctxt->uart_dev, ctxt->uart_baudrate, ctxt->uart_rtscts);
    ctxt->uart.trig_fd = ctxt->uart.data_fd;

    wsbr_rcp_reset(ctxt);
    while (!ctxt->hw_addr_done)
        rcp_rx(ctxt);
    memcpy(ctxt->dynamic_mac, ctxt->hw_mac, sizeof(ctxt->dynamic_mac));

    if (net_init_core())
        BUG("net_init_core");

    ctxt->rcp_if_id = arm_nwk_interface_lowpan_init(&ctxt->mac_api, "ws0");
#endif
    if (ctxt->rcp_if_id < 0)
        BUG("arm_nwk_interface_lowpan_init: %d", ctxt->rcp_if_id);

    if (eventOS_event_handler_create(&wsbr_tasklet, ARM_LIB_TASKLET_INIT_EVENT) < 0)
        BUG("eventOS_event_handler_create");

    char *file_path = ns_file_system_get_root_path();
    logfile = open(file_path, O_WRONLY | O_CREAT, 0644);
    if (logfile < 0)
        WARN("Err open logfile %s", file_path);

    multicast_start();
    INFO("Subscribing to multicast address");
    multicast_add_address(multi_cast_addr, 1);


#ifdef MAC_INPROCESS
    // As wshwsim's loop: radio input first, then one event per iteration,
    // so that frames are served between the stack's events.
    rf_inproc_add_to_run_loop();
    ns_port_posix_add_scheduler(true);
#else
    // Same inputs, in the same order, as the loop that was here.
    ns_port_posix_add_scheduler(false);
    ns_port_posix_add_fd(ctxt->uart.trig_fd, ctxt->uart.trig_fd != ctxt->uart.data_fd,
                         wsrouter_uart_ready, wsrouter_uart_pending);
#endif
    ns_port_run();
    close(logfile);
    return 0;
}

