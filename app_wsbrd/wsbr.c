/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2021, Silicon Labs
 * Main authors:
 *     - Jérôme Pouiller <jerome.pouiller@silabs.com>
 */
#include <stdlib.h>
#include <unistd.h>
#include <signal.h>
#include <sys/select.h>
#include <fcntl.h>
#include<sys/time.h>

#include "mbed-client-libservice/ns_trace.h"
#include "nanostack-event-loop/eventOS_event.h"
#include "nanostack-event-loop/eventOS_scheduler.h"
#include "nanostack-event-loop/eventOS_event_timer.h"
#include "nanostack/mac/fhss_api.h"
#include "nanostack/mac/mac_filter_api.h"
#include "nanostack/ns_file_system.h"
#include "nanostack/ws_bbr_api.h"
#include "nanostack/net_ws_test.h"
#include "nanostack/ws_management_api.h"
#include "nanostack/multicast_api.h"
#include "nanostack/source/6lowpan/mac/mac_helper.h"
#include "nanostack/source/6lowpan/ws/ws_common_defines.h"
#include "nanostack/source/6lowpan/ws/ws_regulation.h"
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
#include "mbedtls_config_check.h"
#include "commandline.h"
#include "version.h"
#include "wsbr_mac.h"
#include "wsbr.h"
#include "dbus.h"
#include "tun.h"
#ifdef MAC_INPROCESS
#include "common/fhss_timer.h"
#include "app_wshwsim/sl_rf_inproc.h"
#include "app_wshwsim/version.h"
#endif
#ifdef NS_PORT_SIM
#include "ns_port_sim.h"
#include "node_table.h"
#endif

// See warning in wsbr.h
#ifdef MAC_INPROCESS
// wsbrd-inproc: the MAC runs in this process (sl_rf_inproc.h).
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

    // FIXME: retrieve from RCP. Normally, MAC layer set this value when it
    // receive the mac802_15_4Mode request.
    // .mac_api.phyMTU = MAC_IEEE_802_15_4G_MAX_PHY_PACKET_SIZE,
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

static int8_t ws_enable_mac_filtering(struct wsbr_ctxt *ctxt)
{
    int ret;
    int i;

    if (ctxt->ws_allowed_mac_address_count > 0 || ctxt->ws_denied_mac_address_count > 0) {
        if (fw_api_older_than(ctxt, 0, 3, 0))
            FATAL(1, "RCP API is too old to enable MAC address filtering");
    }

    if (ctxt->ws_allowed_mac_address_count > 0)
        ret = mac_helper_mac_mlme_filter_start(ctxt->rcp_if_id, MAC_FILTER_BLOCKED);
    else if (ctxt->ws_denied_mac_address_count > 0)
        ret = mac_helper_mac_mlme_filter_start(ctxt->rcp_if_id, MAC_FILTER_ALLOWED);
    else
        return 0;
    if (ret)
        return -1;

    ret = mac_helper_mac_mlme_filter_clear(ctxt->rcp_if_id);
    if (ret)
        return -2;

    for (i = 0; i < ctxt->ws_allowed_mac_address_count; i++) {
        ret = mac_helper_mac_mlme_filter_add_long(ctxt->rcp_if_id, ctxt->ws_allowed_mac_addresses[i], MAC_FILTER_ALLOWED);
        if (ret)
            return -3;
    }

    for (i = 0; i < ctxt->ws_denied_mac_address_count; i++) {
        ret = mac_helper_mac_mlme_filter_add_long(ctxt->rcp_if_id, ctxt->ws_denied_mac_addresses[i], MAC_FILTER_BLOCKED);
        if (ret)
            return -4;
    }

    return 0;
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
    if (ctxt->ws_phy_mode_id && ctxt->ws_chan_plan_id) {
        ret = ws_management_domain_configuration_set(ctxt->rcp_if_id, ctxt->ws_domain,
                                                     ctxt->ws_phy_mode_id, ctxt->ws_chan_plan_id);
        WARN_ON(ret);
    }

    // Note that calling ws_management_fhss_timing_configure() is redundant
    // with the two function calls bellow.
    ret = ws_management_fhss_unicast_channel_function_configure(ctxt->rcp_if_id, channel_function, fixed_channel,
                                                                ctxt->uc_dwell_interval);
    WARN_ON(ret);
    ret = ws_management_fhss_broadcast_channel_function_configure(ctxt->rcp_if_id, channel_function, fixed_channel,
                                                                  ctxt->bc_dwell_interval, ctxt->bc_interval);
    WARN_ON(ret);
    if (fixed_channel == 0xFFFF) {
        ret = ws_management_channel_mask_set(ctxt->rcp_if_id, ctxt->ws_allowed_channels);
        WARN_ON(ret);
    }

    if (ctxt->ws_pan_id >= 0)
        ws_bbr_pan_configuration_set(ctxt->rcp_if_id, ctxt->ws_pan_id);

    // Note that calls to ws_management_timing_parameters_set() and
    // ws_bbr_rpl_parameters_set() are done by the function below.
    ret = ws_management_network_size_set(ctxt->rcp_if_id, ctxt->ws_size);
    WARN_ON(ret);

    ret = arm_nwk_set_tx_output_power(ctxt->rcp_if_id, ctxt->tx_power);
    WARN_ON(ret);

    // FAN 1.1 at run time (GS8, LFN study U1); FAN 1.0 stays the default
    if (ctxt->ws_fan_version != 1) {
        ret = ws_management_fan_version_set(ctxt->rcp_if_id, ctxt->ws_fan_version);
        FATAL_ON(ret, 1, "fan_version: not supported by this build (%d)", ret);
        INFO("Wi-SUN FAN version 1.%d", ctxt->ws_fan_version - 1);
    }
    FATAL_ON(!ctxt->rx_on_when_idle || ctxt->rx_off_after_s >= 0, 1,
             "rx_on_when_idle, rx_off_when_idle_after: router only (a border router must listen)");
    FATAL_ON(ctxt->lfn, 1, "device_type = lfn: router program only");
    // FAN 1.1 LFN parenting (GS8 stage 3)
    if (ctxt->enable_lfn) {
        ret = ws_management_lfn_settings_set(ctxt->rcp_if_id, &ctxt->lfn_cfg);
        FATAL_ON(ret, 1, "lfn_*: invalid LFN settings (%d)", ret);
        ret = ws_management_lfn_parent_set(ctxt->rcp_if_id, true);
        FATAL_ON(ret, 1, "enable_lfn: needs fan_version = 1.1 (%d)", ret);
        INFO("LFN parenting on");
    }


    ret = ws_device_min_sens_set(ctxt->rcp_if_id, 174 - 93);
    WARN_ON(ret);

    ret = ws_test_key_lifetime_set(ctxt->rcp_if_id, ctxt->ws_gtk_expire_offset, ctxt->ws_pmk_lifetime, ctxt->ws_ptk_lifetime);
    WARN_ON(ret);

    ret = ws_test_gtk_time_settings_set(ctxt->rcp_if_id, ctxt->ws_revocation_lifetime_reduction, ctxt->ws_gtk_new_activation_time, ctxt->ws_gtk_new_install_required, ctxt->ws_gtk_max_mismatch);
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

    ret = ws_enable_mac_filtering(ctxt);
    WARN_ON(ret);

    if (ctxt->ws_regional_regulation) {
        FATAL_ON(fw_api_older_than(ctxt, 0, 6, 0), 2,
                 "this device does not support regional regulation");
        ret = ws_regulation_set(ctxt->rcp_if_id, ctxt->ws_regional_regulation);
        WARN_ON(ret);
    }
}

// Test hooks (GS8 stage 3): in every build
#define WSBR_LFN_SILENT_EVENT (APPLICATION_EVENT + 0x41)
#define WSBR_LFN_SILENT_END_EVENT (APPLICATION_EVENT + 0x42)
#define WSBR_LGTK_ROTATE_EVENT (APPLICATION_EVENT + 0x43)

#ifdef NS_PORT_SIM
// Batch mode (virtual time): nobody reads D-Bus, so the border router logs
// the table of D-Bus "Nodes" every 30 s of virtual time, one line per node.
// sim_utils/migration/analyze.py reads these lines.
#define WSBR_NODE_TABLE_EVENT (APPLICATION_EVENT + 0x40)
#define WSBR_NODE_TABLE_PERIOD_MS 30000

static void wsbr_node_table_log(struct wsbr_ctxt *ctxt)
{
    static bbr_route_info_t table[4096];
    uint64_t now_us = ns_port_sim_now_us();
    int len, i;

    len = wsbr_node_table_get(ctxt->rcp_if_id, table, ARRAY_SIZE(table));
    // The table holds IIDs; flip the U/L bit back to print the EUI-64s
    // (RFC 4291 appendix A), the addresses given with --eui64.
    for (i = 0; i < len; i++)
        printf("brtree t=%llu.%06llu node=%02x:%02x:%02x:%02x:%02x:%02x:%02x:%02x "
               "parent=%02x:%02x:%02x:%02x:%02x:%02x:%02x:%02x\n",
               (unsigned long long)(now_us / 1000000), (unsigned long long)(now_us % 1000000),
               table[i].target[0] ^ 0x02, table[i].target[1], table[i].target[2], table[i].target[3],
               table[i].target[4], table[i].target[5], table[i].target[6], table[i].target[7],
               table[i].parent[0] ^ 0x02, table[i].parent[1], table[i].parent[2], table[i].parent[3],
               table[i].parent[4], table[i].parent[5], table[i].parent[6], table[i].parent[7]);
    printf("brtree t=%llu.%06llu nodes=%d\n",
           (unsigned long long)(now_us / 1000000), (unsigned long long)(now_us % 1000000), len);
    fflush(stdout);
}

static void wsbr_node_table_log_start(int8_t tasklet_id)
{
    const arm_event_t event = {
        .receiver = tasklet_id,
        .sender = tasklet_id,
        .event_type = WSBR_NODE_TABLE_EVENT,
        .priority = ARM_LIB_LOW_PRIORITY_EVENT,
    };

    if (!eventOS_event_timer_request_every(&event, eventOS_event_timer_ms_to_ticks(WSBR_NODE_TABLE_PERIOD_MS)))
        BUG("eventOS_event_timer_request_every");
}
#endif

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
    uint8_t ipv6[16];
    int ret;

    switch (event->event_type) {
        case ARM_LIB_TASKLET_INIT_EVENT:
            // The tasklet that call arm_nwk_interface_configure_*_bootstrap_set()
            // will be used to receive ARM_LIB_NWK_INTERFACE_EVENT.
            ret = arm_nwk_interface_configure_6lowpan_bootstrap_set(ctxt->rcp_if_id,
                                                                  NET_6LOWPAN_BORDER_ROUTER,
                                                                  NET_6LOWPAN_WS);
            WARN_ON(ret, "arm_nwk_interface_configure_6lowpan_bootstrap_set: %d", ret);
            ret = arm_nwk_interface_configure_ipv6_bootstrap_set(ctxt->tun_if_id,
                                                               NET_IPV6_BOOTSTRAP_STATIC,
                                                               ctxt->ipv6_prefix);
            WARN_ON(ret, "arm_nwk_interface_configure_ipv6_bootstrap_set: %d", ret);
            memset(ipv6, 0, sizeof(ipv6));
            WARN_ON(get_link_local_addr(ctxt->tun_dev, ipv6), "%s has no link-local address", ctxt->tun_dev);
            arm_net_route_add(NULL, 0, ipv6, 0xFFFFFFFF, 0, ctxt->tun_if_id);
            multicast_fwd_full_for_scope(ctxt->tun_if_id, 3);
            multicast_fwd_full_for_scope(ctxt->rcp_if_id, 3);
            // multicast ZigBee params with timer expiration = 0
            // Imin=10*50, Imax=Imin, k=1, t exp=0, window=75
            multicast_set_parameters(10,0,1,1,75);
            test_traffic_start(&ctxt->test_traffic);

            INFO("Subscribing to multicast address");
            multicast_add_address(multi_cast_addr, 1);

            wsbr_configure_ws(ctxt);
            if (arm_nwk_interface_up(ctxt->tun_if_id))
                 WARN("arm_nwk_interface_up TUN");
            if (arm_nwk_interface_up(ctxt->rcp_if_id))
                 WARN("arm_nwk_interface_up RCP");
            if (ws_bbr_start(ctxt->rcp_if_id, ctxt->tun_if_id))
                 WARN("ws_bbr_start");
            // Test: the LFN parent goes silent for a while (GS8 stage 3)
            if (ctxt->test_lfn_silent_at_s > 0)
                eventOS_event_timer_request(0, WSBR_LFN_SILENT_EVENT, event->receiver,
                                            (uint32_t)ctxt->test_lfn_silent_at_s * 1000);
            // An LGTK rotation (GS8 stage 3, LGTK)
            if (ctxt->lgtk_rotate_at_s > 0)
                eventOS_event_timer_request(0, WSBR_LGTK_ROTATE_EVENT, event->receiver,
                                            (uint32_t)ctxt->lgtk_rotate_at_s * 1000);
            break;
        case WSBR_LGTK_ROTATE_EVENT:
            INFO("LGTK rotation (lgtk_rotate_at_s)");
            WARN_ON(ws_management_lfn_lgtk_rotate(ctxt->rcp_if_id));
            break;
        case WSBR_LFN_SILENT_EVENT:
            INFO("LFN parent silent for %d s (test_lfn_silent_*)", ctxt->test_lfn_silent_for_s);
            WARN_ON(ws_test_lfn_parent_silence(ctxt->rcp_if_id, true));
            eventOS_event_timer_request(0, WSBR_LFN_SILENT_END_EVENT, event->receiver,
                                        (uint32_t)ctxt->test_lfn_silent_for_s * 1000);
            break;
        case WSBR_LFN_SILENT_END_EVENT:
            INFO("LFN parent answering again");
            WARN_ON(ws_test_lfn_parent_silence(ctxt->rcp_if_id, false));
            break;

        case ARM_LIB_NWK_INTERFACE_EVENT:
            if (event->event_id == ctxt->tun_if_id) {
                DEBUG("get event for tun interface: %s", nwk_events[event->event_data]);
            } else if (event->event_id == ctxt->rcp_if_id) {
                DEBUG("get event for ws interface: %s", nwk_events[event->event_data]);
            } else {
                WARN("received unknown network event: %d", event->event_id);
            }
            break;
#ifdef NS_PORT_SIM
        case WSBR_NODE_TABLE_EVENT:
            wsbr_node_table_log(ctxt);
            break;
#endif
        default:
            WARN("received unknown event: %d", event->event_type);
            break;
    }
}


#ifndef MAC_INPROCESS
static int wsbr_uart_tx(struct uart_ctxt *uart_ctxt, const void *buf, unsigned int buf_len)
{
    struct wsbr_ctxt *ctxt = &g_ctxt;
    int ret;

    ret = uart_tx(uart_ctxt, buf, buf_len);
    // Old firmware may merge close Rx events
    if (fw_api_older_than(ctxt, 0, 4, 0))
        usleep(20000);
    return ret;
}

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

static int logfile_fd = -1;
void mbed_trace_print_and_log(const char *str)
{
    puts(str);
    if (logfile_fd >= 0) {
        char tm[12];
        snprintf(tm, sizeof(tm), "%09d ", (int)ns_port_time_wall_s());
        write(logfile_fd, &tm, sizeof(tm)-1);
        write(logfile_fd, str+5, strlen(str)-9);
        write(logfile_fd, "\n", 1);
    }
}


static void wsbr_dbus_ready(void)
{
    dbus_process(&g_ctxt);
}

static void wsbr_tun_ready(void)
{
    wsbr_tun_read(&g_ctxt);
}

#ifndef MAC_INPROCESS
static void wsbr_uart_ready(void)
{
    rcp_rx(&g_ctxt);
}

static bool wsbr_uart_pending(void)
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
#else
    static const int timeout_values[] = { 1, 60, 600, 3600 }; // seconds
    struct timespec ts = { };
    fd_set rfds;
    int ret;
#endif

    // WSBRD_LOG_FILE gives each run its own copy of the trace (sim_utils/vtime).
    logfile_fd = open(getenv("WSBRD_LOG_FILE") ? getenv("WSBRD_LOG_FILE") : "/tmp/log_wsbr",
                      O_WRONLY | O_CREAT, 0644);
    mbed_trace_print_function_set(&mbed_trace_print_and_log);
    INFO("Silicon Labs Wi-SUN border router %s", version_daemon);
    signal(SIGINT, kill_handler);
    signal(SIGHUP, kill_handler);
    ctxt->ping_socket_fd = -1;
#ifndef MAC_INPROCESS
    ctxt->rcp_tx = wsbr_uart_tx;
    ctxt->rcp_rx = uart_rx;
#endif
    wsbr_check_mbedtls_features();
    mbed_trace_init();
    mbed_trace_config_set(TRACE_ACTIVE_LEVEL_ALL | TRACE_MODE_COLOR);
    platform_critical_init();
    ns_port_init();
    eventOS_scheduler_init();
#ifdef MAC_INPROCESS
    rf_inproc_take_eui64(&argc, argv, eui64);
#endif
    parse_commandline(ctxt, argc, argv, print_help_br);
    if (!memcmp(ctxt->ipv6_prefix, ADDR_UNSPECIFIED, 16))
        FATAL(1, "You must specify a ipv6_prefix");
#ifdef MAC_INPROCESS
    // -u names the wssimserver socket instead of the RCP's UART. The API
    // version is the one wshwsim would report (version.c). The radio comes
    // first: in virtual time, nothing may draw random numbers before it has
    // the conductor's seed, and the TUN's interface does.
    mac = rf_inproc_init(ctxt->uart_dev, eui64);
    wsbr_tun_init(ctxt);
    ctxt->rcp_version_api = version_api;
    ctxt->rcp_version_fw = version_fw;
    memcpy(ctxt->hw_mac, eui64, sizeof(ctxt->hw_mac));
    memcpy(ctxt->dynamic_mac, ctxt->hw_mac, sizeof(ctxt->dynamic_mac));

    if (net_init_core())
        BUG("net_init_core");

    ctxt->rcp_if_id = arm_nwk_interface_lowpan_init(mac, "ws0");
#else
    ctxt->uart.data_fd = uart_open(ctxt->uart_dev, ctxt->uart_baudrate, ctxt->uart_rtscts);
    ctxt->uart.trig_fd = ctxt->uart.data_fd;
    wsbr_tun_init(ctxt);

    wsbr_rcp_reset(ctxt);

    for (int i = 0; i < ARRAY_SIZE(timeout_values); i++) {
        FD_ZERO(&rfds);
        FD_SET(ctxt->uart.data_fd, &rfds);
        ts.tv_sec = timeout_values[i];
        ret = pselect(ctxt->uart.data_fd + 1, &rfds, NULL, NULL, &ts, NULL);
        if (ret == 1)
            break;
        if (ret < 0)
            FATAL(2, "pselect: %m");
        else
            WARN("Still waiting for RCP");
    }

    while (!ctxt->hw_addr_done)
        rcp_rx(ctxt);
    memcpy(ctxt->dynamic_mac, ctxt->hw_mac, sizeof(ctxt->dynamic_mac));

    if (net_init_core())
        BUG("net_init_core");

    ctxt->rcp_if_id = arm_nwk_interface_lowpan_init(&ctxt->mac_api, "ws0");
#endif
    if (ctxt->rcp_if_id < 0)
        BUG("arm_nwk_interface_lowpan_init: %d", ctxt->rcp_if_id);

#ifdef NS_PORT_SIM
    {
        int8_t tasklet_id = eventOS_event_handler_create(&wsbr_tasklet, ARM_LIB_TASKLET_INIT_EVENT);

        if (tasklet_id < 0)
            BUG("eventOS_event_handler_create");
        wsbr_node_table_log_start(tasklet_id);
    }
#else
    if (eventOS_event_handler_create(&wsbr_tasklet, ARM_LIB_TASKLET_INIT_EVENT) < 0)
        BUG("eventOS_event_handler_create");
#endif

    /* Example Multicast application */
    /* eventOS_event_timer_request_every((arm_event_t *)&mul_tasklet,1);
    int mul_timer_id = eventOS_callback_timer_register(mul_tasklet);
    eventOS_callback_timer_start_sec(mul_timer_id, 1); */

    dbus_register(ctxt);

    // Same inputs, in the same order, as the loop that was here.
#ifndef MAC_INPROCESS
    // That loop polled with the timeout left over from the wait for the RCP.
    ns_port_posix_set_poll_timeout(&ts);
#endif
    if (dbus_get_fd(ctxt) >= 0)
        ns_port_posix_add_fd(dbus_get_fd(ctxt), false, wsbr_dbus_ready, NULL);
    ns_port_posix_add_fd(ctxt->tun_fd, false, wsbr_tun_ready, NULL);
#ifdef MAC_INPROCESS
    // Radio input, then one event per iteration, as wshwsim's loop did.
    rf_inproc_add_to_run_loop();
    ns_port_posix_add_scheduler(true);
#else
    ns_port_posix_add_scheduler(false);
    ns_port_posix_add_fd(ctxt->uart.trig_fd, ctxt->uart.trig_fd != ctxt->uart.data_fd,
                         wsbr_uart_ready, wsbr_uart_pending);
#endif
    ns_port_run();

    close(logfile_fd);
    return 0;
}

