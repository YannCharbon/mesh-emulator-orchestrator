/* SPDX-License-Identifier: Apache-2.0
 *
 * wslfntest-inproc: two-node check of the Wi-SUN FAN 1.1 LFN unicast
 * mechanics in the stack's MAC and FHSS (GS8 stage 2; the stack's
 * docs/LFN_MAC.md), on the emulated radio, without the upper layers.
 *
 * One process is the parent (an FFN: unicast hopping, optional broadcast
 * schedule, receiver always on, LFN parenting on), the other the LFN (its
 * unicast listen schedule; receiver on only in its windows). This program
 * plays the part of the LLC that stage 3 will bring:
 *
 *  1. The LFN sends a solicit as an asynchronous frame on every channel,
 *     with its LUTT-IE and LUS-IE, until it has heard its parent.
 *  2. The parent learns the LFN's schedule from those IEs (and from every
 *     later LFN frame) and answers with a unicast advert carrying its UTT-IE,
 *     which the MAC holds until the LFN's next window.
 *  3. Then: the parent sends a data frame to the LFN every --parent-tx-period
 *     ms (plus a random part), each held for a window; the LFN sends a data
 *     frame to the parent every --lfn-tx-period ms, at once (TX, ACK, radio
 *     off), with fresh LUTT/LUS.
 *
 * Log lines "lfntest ..." are read by sim_utils/vtime/test_lfn_mac.sh.
 *
 * usage: wslfntest-inproc --eui64=ADDR -u <sim socket> --role=parent|lfn
 *                         --peer=EUI64 [options, see usage()]
 */
#include <getopt.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "mbed-client-libservice/ns_trace.h"
#include "mbed-client-libservice/common_functions.h"
#include "nanostack-event-loop/eventOS_event.h"
#include "nanostack-event-loop/eventOS_event_timer.h"
#include "nanostack-event-loop/eventOS_scheduler.h"
#include "nanostack/mac/mac_api.h"
#include "nanostack/mac/mac_mcps.h"
#include "nanostack/mac/mlme.h"
#include "nanostack/mac/sw_mac.h"
#include "nanostack/mac/fhss_api.h"
#include "nanostack/mac/fhss_config.h"
#include "nanostack/mac/fhss_ws_extension.h"
#include "nanostack/mac/net_fhss.h"
#include "nanostack/mac/channel_list.h"
#include "nanostack/mac/platform/arm_hal_phy.h"
#include "ns_port/critical.h"
#include "ns_port/sched.h"
#include "ns_port/time.h"
#include "ns_port/fhss_timer.h"
#include "ns_rand.h"
#include "common/os_scheduler.h"
#include "common/log.h"
#include "app_wshwsim/sl_rf_inproc.h"

#define PAN_ID          0xabcd
#define WH_IE_ID        0x2a
#define SUB_UTT         1
#define SUB_BT          2
#define SUB_LTO         17
#define SUB_LUTT        10
#define SUB_LUS         13

enum { EV_INIT = 0, EV_TIMER = 1 };
enum { TMR_SOLICIT = 1, TMR_PARENT_TX, TMR_LFN_TX };

static struct {
    bool lfn;
    uint8_t eui64[8];
    uint8_t peer[8];
    const char *sock;
    // PHY and FFN schedule
    uint32_t datarate;
    uint16_t channels;
    uint8_t uc_dwell_ms;
    uint32_t bc_interval_ms;
    uint8_t bc_dwell_ms;
    // LFN mechanics (user settings, FHSS defaults otherwise)
    fhss_ws_lfn_listen_t listen;
    fhss_ws_lfn_parent_t parent;
    // Traffic
    uint32_t parent_tx_period_ms;
    uint32_t parent_tx_jitter_ms;
    uint32_t lfn_tx_period_ms;
    uint32_t solicit_period_ms;
    uint16_t payload;
    uint8_t max_frame_retries;
    // LTO-IE in the parent's answer (0, 0: none)
    uint32_t lto_offset_ms;
    uint32_t lto_interval_ms;
} cfg;

static mac_api_t *mac;
static fhss_api_t *fhss;
static int8_t tasklet;
static fhss_ws_neighbor_timing_info_t peer_info;   // the one neighbour
static bool peer_known;                            // its schedule is known
static uint16_t seq_tx;
static uint8_t msdu_handle;
static uint32_t channel_mask[8];
static int lto_handle = -1;                        // parent: the answer that carries the LTO

/* ------------------------------------------------------------- IEs */

// WH header IE with the Wi-SUN sub-ID: 2-byte header (length, ID 0x2a,
// type 0), sub-ID, content
static uint8_t *wh_write(uint8_t *p, uint8_t sub_id, uint8_t content_len)
{
    uint16_t hdr = ((content_len + 1) & 0x7f) | (WH_IE_ID << 7);
    p = common_write_16_bit_inverse(hdr, p);
    *p++ = sub_id;
    return p;
}

static const uint8_t *wh_find(const uint8_t *p, uint16_t len, uint8_t sub_id, uint8_t *content_len)
{
    while (len >= 2) {
        uint16_t hdr = common_read_16_bit_inverse(p);
        uint8_t l = hdr & 0x7f;
        uint8_t id = (hdr >> 7) & 0xff;
        if (l + 2 > len) {
            return NULL;
        }
        if (id == WH_IE_ID && l >= 1 && p[2] == sub_id) {
            *content_len = l - 1;
            return p + 3;
        }
        p += l + 2;
        len -= l + 2;
    }
    return NULL;
}

/* -------------------------------------------------------- neighbour */

static fhss_ws_neighbor_timing_info_t *get_neighbor(const fhss_api_t *api, uint8_t eui64[8])
{
    (void)api;
    if (peer_known && !memcmp(eui64, cfg.peer, 8)) {
        return &peer_info;
    }
    return NULL;
}

static void peer_channel_plan(void)
{
    peer_info.uc_timing_info.unicast_channel_function = WS_DH1CF;
    peer_info.uc_timing_info.unicast_number_of_channels = cfg.channels;
    peer_info.uc_channel_list.channel_count = cfg.channels;
    memcpy(peer_info.uc_channel_list.channel_mask, channel_mask, sizeof(channel_mask));
}

/* ------------------------------------------------------------- TX */

static void send_frame(char kind, bool unicast, bool lfn_ies, bool lto)
{
    // The MAC keeps pointers to the IEs and the payload until the confirm
    // (an asynchronous frame is rebuilt for every channel): one buffer per
    // MSDU handle
    static struct {
        uint8_t pies[2 + 256];
        uint8_t ies[48];
        ns_ie_iovec_t hdr_vec;
        ns_ie_iovec_t pl_vec;
    } bufs[256];
    uint8_t handle = msdu_handle++;
    uint8_t *ies = bufs[handle].ies, *p = ies;
    ns_ie_iovec_t *hdr_vec = &bufs[handle].hdr_vec;
    mcps_data_req_ie_list_t ie_list = { 0 };
    mcps_data_req_t req = { 0 };
    channel_list_s async = { 0 };
    uint16_t seq = seq_tx++;

    if (lfn_ies) {
        // LUTT-IE: frame type, slot, offset (written by the FHSS at TX time)
        p = wh_write(p, SUB_LUTT, 6);
        *p++ = 0;
        memset(p, 0, 5);
        p += 5;
        // LUS-IE: listen interval (ms), channel plan tag
        p = wh_write(p, SUB_LUS, 4);
        p = common_write_24_bit_inverse(cfg.listen.listen_interval_ms, p);
        *p++ = 0;
    } else {
        // UTT-IE: frame type, UFSI (written by the FHSS at TX time)
        p = wh_write(p, SUB_UTT, 4);
        *p++ = 0;
        memset(p, 0, 3);
        p += 3;
        // BT-IE: the parent's broadcast slot and offset (FHSS, TX time)
        if (cfg.bc_interval_ms && cfg.bc_dwell_ms) {
            p = wh_write(p, SUB_BT, 5);
            memset(p, 0, 5);
            p += 5;
        }
        if (lto) {
            p = wh_write(p, SUB_LTO, 6);
            p = common_write_24_bit_inverse(cfg.lto_offset_ms, p);
            p = common_write_24_bit_inverse(cfg.lto_interval_ms, p);
            lto_handle = handle;
        }
    }
    hdr_vec->ieBase = ies;
    hdr_vec->iovLen = p - ies;
    ie_list.headerIeVectorList = hdr_vec;
    ie_list.headerIovLength = 1;
    // The test payload (kind, sequence number, padding) in a payload IE of
    // the Wi-SUN group (WP-IE, group 4), with no MSDU after it: as the
    // Wi-SUN LLC carries its data in the MPX-IE, which the MAC's receive
    // path expects
    uint8_t *pl = bufs[handle].pies;
    common_write_16_bit_inverse((cfg.payload & 0x7ff) | (4 << 11) | (1 << 15), pl);
    memset(pl + 2, 0, cfg.payload);
    pl[2] = kind;
    common_write_16_bit(seq, pl + 3);
    bufs[handle].pl_vec.ieBase = pl;
    bufs[handle].pl_vec.iovLen = 2 + cfg.payload;
    ie_list.payloadIeVectorList = &bufs[handle].pl_vec;
    ie_list.payloadIovLength = 1;

    req.SrcAddrMode = MAC_ADDR_MODE_64_BIT;
    req.DstPANId = PAN_ID;
    req.msdu = NULL;
    req.msduLength = 0;
    req.msduHandle = handle;
    req.SeqNumSuppressed = false;
    req.PanIdSuppressed = true;
    if (unicast) {
        req.DstAddrMode = MAC_ADDR_MODE_64_BIT;
        memcpy(req.DstAddr, cfg.peer, 8);
        req.TxAckReq = true;
        mac->mcps_data_req_ext(mac, &req, &ie_list, NULL, MAC_DATA_NORMAL_PRIORITY, 0);
    } else {
        // Asynchronous: one frame per channel of the plan
        req.DstAddrMode = MAC_ADDR_MODE_NONE;
        req.TxAckReq = false;
        async.channel_page = CHANNEL_PAGE_10;
        memcpy(async.channel_mask, channel_mask, sizeof(channel_mask));
        mac->mcps_data_req_ext(mac, &req, &ie_list, &async, MAC_DATA_NORMAL_PRIORITY, 0);
    }
    INFO("lfntest tx-req %c seq=%u handle=%u", kind, seq, req.msduHandle);
}

static void timer_ms(uint8_t id, uint32_t ms)
{
    eventOS_event_timer_request(id, EV_TIMER, tasklet, ms ? ms : 1);
}

/* ------------------------------------------------------------- RX */

static void data_ind_ext(const mac_api_t *api, const mcps_data_ind_t *ind, const mcps_data_ie_list_t *ie)
{
    const uint8_t *c;
    uint8_t clen;
    (void)api;

    if (ind->SrcAddrMode != MAC_ADDR_MODE_64_BIT || memcmp(ind->SrcAddr, cfg.peer, 8) ||
            ie->payloadIeListLength < 5 || ((common_read_16_bit_inverse(ie->payloadIeList) >> 11) & 0xf) != 4) {
        return;
    }
    char kind = ie->payloadIeList[2];
    uint16_t seq = common_read_16_bit(ie->payloadIeList + 3);

    if (cfg.lfn) {
        // The parent's UTT-IE: its FFN unicast schedule
        c = wh_find(ie->headerIeList, ie->headerIeListLength, SUB_UTT, &clen);
        if (c && clen >= 4) {
            peer_channel_plan();
            peer_info.uc_timing_info.unicast_dwell_interval = cfg.uc_dwell_ms;
            peer_info.uc_timing_info.ufsi = common_read_24_bit_inverse(c + 1);
            peer_info.uc_timing_info.utt_rx_timestamp = ind->timestamp;
            if (!peer_known) {
                peer_known = true;
                INFO("lfntest lfn: parent schedule learnt");
                timer_ms(TMR_LFN_TX, cfg.lfn_tx_period_ms);
            }
        }
        // The parent's BT-IE: its broadcast schedule, which uplinks avoid
        c = wh_find(ie->headerIeList, ie->headerIeListLength, SUB_BT, &clen);
        if (c && clen >= 5) {
            broadcast_timing_info_t bt = {
                .broadcast_channel_function = WS_DH1CF,
                .broadcast_dwell_interval = cfg.bc_dwell_ms,
                .broadcast_interval = cfg.bc_interval_ms,
                .broadcast_slot = common_read_16_bit_inverse(c),
                .broadcast_interval_offset = common_read_24_bit_inverse(c + 2),
                .bt_rx_timestamp = ind->timestamp,
            };
            ns_fhss_ws_set_parent(fhss, cfg.peer, &bt, true);
            INFO("lfntest lfn: parent bc offset=%"PRIu32" ts=%"PRIu32" interval=%"PRIu32" dwell=%u",
                 (uint32_t)bt.broadcast_interval_offset, bt.bt_rx_timestamp, bt.broadcast_interval, bt.broadcast_dwell_interval);
        }
        // LTO-IE: move the listen schedule
        c = wh_find(ie->headerIeList, ie->headerIeListLength, SUB_LTO, &clen);
        if (c && clen >= 6) {
            uint32_t off = common_read_24_bit_inverse(c), ivl = common_read_24_bit_inverse(c + 3);
            int ret = ns_fhss_ws_lfn_timing_set(fhss, off, ivl, ind->timestamp);
            if (!ret && ivl) {
                cfg.listen.listen_interval_ms = ivl;
            }
            INFO("lfntest lfn: lto offset=%"PRIu32" interval=%"PRIu32" ts=%"PRIu32" ret=%d", off, ivl, ind->timestamp, ret);
        }
    } else {
        // The LFN's LUTT-IE and LUS-IE: its unicast listen schedule
        const uint8_t *lus = wh_find(ie->headerIeList, ie->headerIeListLength, SUB_LUS, &clen);
        uint8_t clen2;
        c = wh_find(ie->headerIeList, ie->headerIeListLength, SUB_LUTT, &clen2);
        if (c && clen2 >= 6 && lus && clen >= 4) {
            bool first = !peer_known;
            peer_channel_plan();
            peer_info.lfn_uc_timing_info.listen_interval_ms = common_read_24_bit_inverse(lus);
            peer_info.lfn_uc_timing_info.slot_number = common_read_16_bit_inverse(c + 1);
            peer_info.lfn_uc_timing_info.interval_offset_ms = common_read_24_bit_inverse(c + 3);
            peer_info.lfn_uc_timing_info.lutt_rx_timestamp = ind->timestamp;
            peer_known = true;
            INFO("lfntest parent: lfn schedule interval=%"PRIu32" slot=%u offset=%"PRIu32" ms",
                 peer_info.lfn_uc_timing_info.listen_interval_ms, peer_info.lfn_uc_timing_info.slot_number,
                 (uint32_t)peer_info.lfn_uc_timing_info.interval_offset_ms);
            if (first) {
                send_frame('A', true, false, cfg.lto_offset_ms || cfg.lto_interval_ms);
                timer_ms(TMR_PARENT_TX, cfg.parent_tx_period_ms);
            }
        }
    }
    INFO("lfntest rx %c seq=%u ts=%"PRIu32" ch-rssi=%d", kind, seq, ind->timestamp, ind->signal_dbm);
}

static void data_conf_ext(const mac_api_t *api, const mcps_data_conf_t *conf, const mcps_data_conf_payload_t *payload)
{
    (void)api;
    (void)payload;
    INFO("lfntest conf handle=%u status=%u tx_retries=%u cca_retries=%u", conf->msduHandle, conf->status,
         conf->tx_retries, conf->cca_retries);
    if (!cfg.lfn && conf->msduHandle == lto_handle) {
        lto_handle = -1;
        if (conf->status == MLME_SUCCESS) {
            // The LFN applied the LTO: its schedule as it now runs
            int ret = ns_fhss_ws_lfn_neighbor_lto(fhss, cfg.peer, cfg.lto_offset_ms, cfg.lto_interval_ms, conf->timestamp);
            INFO("lfntest parent: lto applied ts=%"PRIu32" ret=%d", conf->timestamp, ret);
        }
    }
}

static void ack_req_ext(const mac_api_t *api, mcps_ack_data_payload_t *data, int8_t rssi, uint8_t lqi)
{
    (void)api;
    (void)rssi;
    (void)lqi;
    memset(data, 0, sizeof(*data));
}

static void data_conf(const mac_api_t *api, const mcps_data_conf_t *conf) { (void)api; (void)conf; }
static void data_ind(const mac_api_t *api, const mcps_data_ind_t *ind) { (void)api; (void)ind; }
static void purge_conf(const mac_api_t *api, mcps_purge_conf_t *conf) { (void)api; (void)conf; }
static void mlme_conf(const mac_api_t *api, mlme_primitive id, const void *data) { (void)api; (void)id; (void)data; }
static void mlme_ind(const mac_api_t *api, mlme_primitive id, const void *data) { (void)api; (void)id; (void)data; }

/* -------------------------------------------------------------- setup */

static void mlme_set(mlme_attr_t attr, const void *value, uint8_t size)
{
    mlme_set_t set = { .attr = attr, .value_pointer = value, .value_size = size };
    mac->mlme_req(mac, MLME_SET, &set);
}

static void stats_report(void)
{
    fhss_ws_lfn_stats_t st;

    if (ns_fhss_ws_lfn_stats_get(fhss, &st) == 0) {
        INFO("lfntest stats windows_opened=%"PRIu32" windows_skipped=%"PRIu32" closes_deferred=%"PRIu32
             " tx_released=%"PRIu32" tx_held=%"PRIu32" tx_expired=%"PRIu32,
             st.windows_opened, st.windows_skipped, st.closes_deferred, st.tx_released, st.tx_held, st.tx_expired);
    }
}

static void start(void)
{
    fhss_ws_configuration_t fc = { 0 };
    phy_rf_channel_configuration_s rf = { 0 };
    phy_802_15_4_mode_t mode = IEEE_802_15_4G_2012;
    mlme_start_t start_req = { 0 };
    bool rx_on = false;
    uint16_t ack_wait = 0;

    for (int i = 0; i < cfg.channels; i++) {
        channel_mask[i / 32] |= 1u << (i % 32);
    }
    mac->mac_initialize(mac, data_conf, data_ind, purge_conf, mlme_conf, mlme_ind, 0);
    mac->mac_mcps_extension_enable(mac, data_ind_ext, data_conf_ext, ack_req_ext);
    mac->mac64_set(mac, cfg.eui64);
    mlme_set(mac802_15_4Mode, &mode, sizeof(mode));
    rf.channel_0_center_frequency = 863100000;
    rf.channel_spacing = 200000;
    rf.datarate = cfg.datarate;
    rf.number_of_channels = cfg.channels;
    rf.modulation = M_2FSK;
    rf.modulation_index = MODULATION_INDEX_0_5;
    mlme_set(macRfConfiguration, &rf, sizeof(rf));
    // As the Wi-SUN stack (ws_bootstrap_set_rf_config(): WS_ACK_WAIT_SYMBOLS
    // plus WS_TACK_MAX_MS of symbols)
    ack_wait = 800 + 5 * (cfg.datarate / 1000);
    mlme_set(macAckWaitDuration, &ack_wait, sizeof(ack_wait));
    mlme_set(macMaxFrameRetries, &cfg.max_frame_retries, sizeof(cfg.max_frame_retries));
    // Restarts after failed retries, as the Wi-SUN stack sets them
    // (ws_bootstrap.c: WS_CCA_REQUEST_RESTART_MAX, WS_TX_REQUEST_RESTART_MAX,
    // WS_REQUEST_RESTART_BLACKLIST_MIN/MAX)
    mlme_request_restart_config_t restart = { 9, 4, 20, 300 };
    mlme_set(macRequestRestart, &restart, sizeof(restart));

    fc.ws_uc_channel_function = WS_DH1CF;
    fc.ws_bc_channel_function = WS_DH1CF;
    fc.bsi = 0x5a5a;
    fc.fhss_uc_dwell_interval = cfg.uc_dwell_ms;
    if (!cfg.lfn) {
        fc.fhss_broadcast_interval = cfg.bc_interval_ms;
        fc.fhss_bc_dwell_interval = cfg.bc_dwell_ms;
    }
    fc.channel_mask_size = cfg.channels;
    memcpy(fc.channel_mask, channel_mask, sizeof(channel_mask));
    memcpy(fc.unicast_channel_mask, channel_mask, sizeof(channel_mask));
    fhss = ns_fhss_ws_create(&fc, &ns_port_fhss_timer);
    FATAL_ON(!fhss, 2, "ns_fhss_ws_create");
    FATAL_ON(ns_sw_mac_fhss_register(mac, fhss), 2, "ns_sw_mac_fhss_register");
    ns_fhss_set_neighbor_info_fp(fhss, get_neighbor);
    if (cfg.lfn) {
        int ret = ns_fhss_ws_lfn_listen_set(fhss, &cfg.listen);
        FATAL_ON(ret, 2, "ns_fhss_ws_lfn_listen_set: %d", ret);
    } else {
        int ret = ns_fhss_ws_lfn_parent_set(fhss, &cfg.parent);
        FATAL_ON(ret, 2, "ns_fhss_ws_lfn_parent_set: %d", ret);
    }

    start_req.PANId = PAN_ID;
    start_req.BeaconOrder = 0x0f;
    start_req.SuperframeOrder = 0x0f;
    start_req.PANCoordinator = true;
    mac->mlme_req(mac, MLME_START, &start_req);
    if (cfg.lfn) {
        // The schedule's slot 0 starts first_slot_delay_us after this line
        INFO("lfntest lfn: schedule started interval=%"PRIu32" window=%"PRIu32" guard=%"PRIu32" first_slot_delay=%"PRIu32,
             cfg.listen.listen_interval_ms, cfg.listen.rx_window_us, cfg.listen.rx_guard_us, cfg.listen.first_slot_delay_us);
        mlme_set(macRxOnWhenIdle, &rx_on, sizeof(rx_on));
        timer_ms(TMR_SOLICIT, 100);
    } else {
        INFO("lfntest parent: started tx_offset=%"PRIu32" tx_lead=%"PRIu32" tx_min_prep=%"PRIu32" ttl_intervals=%u",
             cfg.parent.tx_offset_us, cfg.parent.tx_lead_us, cfg.parent.tx_min_prep_us, cfg.parent.tx_ttl_intervals);
    }
}

static void tasklet_cb(arm_event_t *ev)
{
    if (ev->event_type == EV_INIT) {
        start();
        return;
    }
    switch (ev->event_id) {
        case TMR_SOLICIT:
            if (!peer_known) {
                send_frame('S', false, true, false);
                timer_ms(TMR_SOLICIT, cfg.solicit_period_ms);
            }
            break;
        case TMR_PARENT_TX:
            send_frame('D', true, false, false);
            stats_report();
            timer_ms(TMR_PARENT_TX, cfg.parent_tx_period_ms +
                     (cfg.parent_tx_jitter_ms ? rand_get_random_in_range(0, cfg.parent_tx_jitter_ms) : 0));
            break;
        case TMR_LFN_TX:
            send_frame('U', true, true, false);
            stats_report();
            timer_ms(TMR_LFN_TX, cfg.lfn_tx_period_ms);
            break;
    }
}

/* ------------------------------------------------------------ options */

static void usage(FILE *f)
{
    fprintf(f,
            "usage: wslfntest-inproc --eui64=ADDR -u <sim socket> --role=parent|lfn --peer=EUI64 [options]\n"
            "PHY and FFN schedule:\n"
            "  --datarate=<bit/s>            (150000)       --channels=<n>      (35)\n"
            "  --uc-dwell-ms=<ms>            (255)\n"
            "  --bc-interval-ms=<ms>         parent's broadcast schedule (1020; 0: none)\n"
            "  --bc-dwell-ms=<ms>            (255)\n"
            "LFN listen schedule (role lfn; the stack's FHSS_LFN_*_DEFAULT otherwise):\n"
            "  --listen-interval-ms=<ms>  --rx-window-us=<us>  --rx-guard-us=<us>\n"
            "  --close-retry-us=<us>      --first-slot-delay-us=<us>\n"
            "LFN parent (role parent):\n"
            "  --tx-offset-us=<us>  --tx-lead-us=<us>  --tx-min-prep-us=<us>  --tx-ttl-intervals=<n>\n"
            "  --lto-offset-ms=<ms> --lto-interval-ms=<ms>  LTO-IE in the answer to the solicit (none)\n"
            "Traffic:\n"
            "  --parent-tx-period-ms=<ms>    (3000)  --parent-tx-jitter-ms=<ms>  (1000)\n"
            "  --lfn-tx-period-ms=<ms>       (10000) --solicit-period-ms=<ms>    (2000)\n"
            "  --payload=<bytes>             (40)\n"
            "  --max-frame-retries=<n>       (3, as the Wi-SUN stack, which also restarts a\n"
            "                                failed frame 4 times; to an LFN, one attempt per window)\n");
}

static void parse_eui64(const char *s, uint8_t out[8])
{
    int n = sscanf(s, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx:%hhx:%hhx", &out[0], &out[1], &out[2], &out[3],
                   &out[4], &out[5], &out[6], &out[7]);
    FATAL_ON(n != 8, 1, "bad EUI-64 %s", s);
}

static void parse(int argc, char *argv[])
{
    enum {
        O_ROLE = 256, O_PEER, O_DATARATE, O_CHANNELS, O_UCDWELL, O_BCINT, O_BCDWELL, O_LISTEN, O_WINDOW, O_GUARD,
        O_CLOSERETRY, O_FIRSTSLOT, O_TXOFF, O_TXLEAD, O_TXPREP, O_TTL, O_PTX, O_PJIT, O_LTX, O_SOL, O_PAYLOAD, O_RETRIES,
        O_LTOOFF, O_LTOIVL,
    };
    static const struct option opts[] = {
        { "role", required_argument, 0, O_ROLE }, { "peer", required_argument, 0, O_PEER },
        { "datarate", required_argument, 0, O_DATARATE }, { "channels", required_argument, 0, O_CHANNELS },
        { "uc-dwell-ms", required_argument, 0, O_UCDWELL }, { "bc-interval-ms", required_argument, 0, O_BCINT },
        { "bc-dwell-ms", required_argument, 0, O_BCDWELL }, { "listen-interval-ms", required_argument, 0, O_LISTEN },
        { "rx-window-us", required_argument, 0, O_WINDOW }, { "rx-guard-us", required_argument, 0, O_GUARD },
        { "close-retry-us", required_argument, 0, O_CLOSERETRY }, { "first-slot-delay-us", required_argument, 0, O_FIRSTSLOT },
        { "tx-offset-us", required_argument, 0, O_TXOFF }, { "tx-lead-us", required_argument, 0, O_TXLEAD },
        { "tx-min-prep-us", required_argument, 0, O_TXPREP }, { "tx-ttl-intervals", required_argument, 0, O_TTL },
        { "parent-tx-period-ms", required_argument, 0, O_PTX }, { "parent-tx-jitter-ms", required_argument, 0, O_PJIT },
        { "lfn-tx-period-ms", required_argument, 0, O_LTX }, { "solicit-period-ms", required_argument, 0, O_SOL },
        { "payload", required_argument, 0, O_PAYLOAD }, { "max-frame-retries", required_argument, 0, O_RETRIES },
        { "lto-offset-ms", required_argument, 0, O_LTOOFF }, { "lto-interval-ms", required_argument, 0, O_LTOIVL },
        { "help", no_argument, 0, 'h' },
        { 0 }
    };
    bool role = false, peer = false;
    int opt;

    cfg.datarate = 150000;
    cfg.channels = 35;
    cfg.uc_dwell_ms = 255;
    cfg.bc_interval_ms = 1020;
    cfg.bc_dwell_ms = 255;
    ns_fhss_ws_lfn_listen_defaults(&cfg.listen);
    ns_fhss_ws_lfn_parent_defaults(&cfg.parent);
    cfg.parent_tx_period_ms = 3000;
    cfg.parent_tx_jitter_ms = 1000;
    cfg.lfn_tx_period_ms = 10000;
    cfg.solicit_period_ms = 2000;
    cfg.payload = 40;
    cfg.max_frame_retries = 3;
    while ((opt = getopt_long(argc, argv, "u:h", opts, NULL)) != -1) {
        unsigned long v = optarg ? strtoul(optarg, NULL, 0) : 0;
        switch (opt) {
            case 'u': cfg.sock = optarg; break;
            case O_ROLE:
                FATAL_ON(strcmp(optarg, "lfn") && strcmp(optarg, "parent"), 1, "--role=parent|lfn");
                cfg.lfn = !strcmp(optarg, "lfn");
                role = true;
                break;
            case O_PEER: parse_eui64(optarg, cfg.peer); peer = true; break;
            case O_DATARATE: cfg.datarate = v; break;
            case O_CHANNELS: FATAL_ON(!v || v > 256, 1, "--channels"); cfg.channels = v; break;
            case O_UCDWELL: FATAL_ON(!v || v > 255, 1, "--uc-dwell-ms"); cfg.uc_dwell_ms = v; break;
            case O_BCINT: cfg.bc_interval_ms = v; break;
            case O_BCDWELL: FATAL_ON(v > 255, 1, "--bc-dwell-ms"); cfg.bc_dwell_ms = v; break;
            case O_LISTEN: cfg.listen.listen_interval_ms = v; break;
            case O_WINDOW: cfg.listen.rx_window_us = v; break;
            case O_GUARD: cfg.listen.rx_guard_us = v; break;
            case O_CLOSERETRY: cfg.listen.close_retry_us = v; break;
            case O_FIRSTSLOT: cfg.listen.first_slot_delay_us = v; break;
            case O_TXOFF: cfg.parent.tx_offset_us = v; break;
            case O_TXLEAD: cfg.parent.tx_lead_us = v; break;
            case O_TXPREP: cfg.parent.tx_min_prep_us = v; break;
            case O_TTL: cfg.parent.tx_ttl_intervals = v; break;
            case O_PTX: cfg.parent_tx_period_ms = v; break;
            case O_PJIT: cfg.parent_tx_jitter_ms = v; break;
            case O_LTX: cfg.lfn_tx_period_ms = v; break;
            case O_SOL: cfg.solicit_period_ms = v; break;
            case O_PAYLOAD: FATAL_ON(v < 3 || v > 200, 1, "--payload 3..200"); cfg.payload = v; break;
            case O_RETRIES: FATAL_ON(v > 7, 1, "--max-frame-retries 0..7"); cfg.max_frame_retries = v; break;
            case O_LTOOFF: cfg.lto_offset_ms = v; break;
            case O_LTOIVL: cfg.lto_interval_ms = v; break;
            case 'h': usage(stdout); exit(0);
            default: usage(stderr); exit(1);
        }
    }
    if (!role || !peer || !cfg.sock) {
        usage(stderr);
        exit(1);
    }
}

static void trace_print(const char *str)
{
    puts(str);
}

int main(int argc, char *argv[])
{
    mbed_trace_print_function_set(&trace_print);
    mbed_trace_init();
    mbed_trace_config_set(TRACE_ACTIVE_LEVEL_ALL | TRACE_MODE_PLAIN);
    platform_critical_init();
    ns_port_init();
    eventOS_scheduler_init();
    rf_inproc_take_eui64(&argc, argv, cfg.eui64);
    parse(argc, argv);
    mac = rf_inproc_init(cfg.sock, cfg.eui64);
    tasklet = eventOS_event_handler_create(tasklet_cb, EV_INIT);
    FATAL_ON(tasklet < 0, 2, "eventOS_event_handler_create");
    rf_inproc_add_to_run_loop();
    ns_port_posix_add_scheduler(true);
    ns_port_run();
    return 0;
}
