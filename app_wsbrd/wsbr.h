/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2021, Silicon Labs
 * Main authors:
 *     - Jérôme Pouiller <jerome.pouiller@silabs.com>
 */
#ifndef WSBR_H
#define WSBR_H

#include <stdbool.h>
#include <stdint.h>
#include <sys/socket.h> // Compatibility with linux headers < 4.12
#include <linux/if.h>
#include <linux/limits.h>
#ifdef HAVE_LIBSYSTEMD
#  include <systemd/sd-bus.h>
#else
typedef struct sd_bus sd_bus;
#endif

#include "common/utils.h"
#include "common/bus_uart.h"
#include "nanostack/mac/mac_api.h"
#include "nanostack/mac/fhss_config.h"
#include "nanostack/net_interface.h"
#include "nanostack/source/mac/rf_driver_storage.h"
#include "nanostack/ws_management_api.h"
#include "test_traffic.h"


struct phy_device_driver_s;
struct eth_mac_api_s;
struct fhss_api;

struct wsbr_ctxt {
    struct uart_ctxt uart;
    sd_bus *dbus;

    char uart_dev[PATH_MAX];
    int  uart_baudrate;
    bool uart_rtscts;

    struct phy_device_driver_s *tun_driver;
    struct eth_mac_api_s *tun_mac_api;
    int  tun_driver_id;
    int  tun_if_id;
    int  tun_fd;
    char tun_dev[IFNAMSIZ];
    bool tun_autoconf;

    bool reset_done;
    bool hw_addr_done;
    uint8_t hw_mac[8];
    uint8_t dynamic_mac[8];
    struct fhss_api *fhss_api;
    struct mac_api_s mac_api;
    struct mac_description_storage_size_s storage_sizes;

    bool fhss_conf_valid;
    struct fhss_ws_configuration fhss_conf;

    int  (*rcp_tx)(struct uart_ctxt *ctxt, const void *buf, unsigned int len);
    int  (*rcp_rx)(struct uart_ctxt *ctxt, void *buf, unsigned int len);
    int  rcp_driver_id;
    int  rcp_if_id;
    int  rcp_time_diff;

    int spinel_tid;
    int spinel_iid;

    uint32_t rcp_version_api;
    uint32_t rcp_version_fw;

    int  ws_domain;
    int  ws_mode;
    int  ws_class;
    int  ws_phy_mode_id;        // FAN 1.1 PHY mode ID with ws_chan_plan_id (0: class and mode only)
    int  ws_chan_plan_id;
    int  ws_size;
    char ws_name[33]; // null-terminated string of 32 chars
    int  ws_pan_id;
    int  tx_power;
    int  ws_fan_version;        // 1: FAN 1.0 (default), 2: FAN 1.1
    bool rx_on_when_idle;       // false: the MAC turns the radio off when idle
    int  rx_off_after_s;        // >= 0: RX off when idle from this many seconds after start
    bool lfn;                   // device_type = lfn: a FAN 1.1 LFN (router program only)
    bool enable_lfn;            // border router: parent LFNs (FAN 1.1)
    ws_lfn_settings_t lfn_cfg;  // lfn_* settings (defaults: ws_management_lfn_settings_defaults())
    struct test_traffic_cfg test_traffic; // test_udp_* (test_traffic.h)
    int test_lfn_silent_at_s;   // > 0: the LFN parent goes silent then (ws_test_lfn_parent_silence())
    int test_lfn_silent_for_s;  // for this long
    int lgtk_rotate_at_s;       // > 0: an LGTK rotation then (ws_management_lfn_lgtk_rotate())


    uint8_t ws_gtk[4][16];
    bool ws_gtk_force[4];
    uint8_t ipv6_prefix[16];
    uint32_t ws_allowed_channels[8];
    int ws_pmk_lifetime;
    int ws_ptk_lifetime;
    int ws_gtk_expire_offset;
    int ws_gtk_new_activation_time;
    int ws_gtk_new_install_required;
    int ws_revocation_lifetime_reduction;
    int ws_gtk_max_mismatch;
    arm_certificate_entry_s tls_own;
    arm_certificate_entry_s tls_ca;
    int uc_dwell_interval;
    int bc_interval;
    int bc_dwell_interval;
    uint8_t ws_allowed_mac_addresses[10][8];
    uint8_t ws_allowed_mac_address_count;
    uint8_t ws_denied_mac_addresses[10][8];
    uint8_t ws_denied_mac_address_count;
    int ws_regional_regulation;

    // For DebugPing dbus interface
    int ping_socket_fd;
};

// This global variable is necessary for various API of nanostack. Beside this
// case, please never use it.
extern struct wsbr_ctxt g_ctxt;

/**
 * Indicates RCP firmware API is older than specified version.
 * (major.minor.patch).
 */
static inline bool fw_api_older_than(const struct wsbr_ctxt *ctxt,
                                     uint8_t major,
                                     uint16_t minor,
                                     uint8_t patch)
{
    uint8_t fw_api_major = FIELD_GET(0xFF000000, ctxt->rcp_version_api);
    uint16_t fw_api_minor = FIELD_GET(0x00FFFF00, ctxt->rcp_version_api);
    uint8_t fw_api_patch = FIELD_GET(0x000000FF, ctxt->rcp_version_api);

    if (fw_api_major < major)
        return true;
    if (fw_api_major == major && fw_api_minor < minor)
        return true;
    if (fw_api_major == major && fw_api_minor == minor && fw_api_patch < patch)
        return true;
    return false;
}

void wsbr_handle_reset(struct wsbr_ctxt *ctxt, const char *version_fw_str);

#endif
