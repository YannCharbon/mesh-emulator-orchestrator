/* SPDX-License-Identifier: Apache-2.0
 *
 * Per-node network statistics: see net_stats.h and docs/TRAFFIC_AND_REPORT.md.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "common/log.h"
#include "nanostack/mac/mac_api.h"
#include "nanostack/mac/sw_mac.h"
#include "nanostack/nwk_stats_api.h"
#include "nanostack/net_rpl.h"
#include "nanostack/ws_management_api.h"
#include "nanostack-event-loop/eventOS_event.h"
#include "nanostack-event-loop/eventOS_event_timer.h"
#include "app_load.h"
#include "net_stats.h"

// The stack's RPL internals (nanostack/source/rpl/rpl_control.h)
struct rpl_domain;
struct rpl_instance;
extern struct rpl_domain *protocol_6lowpan_rpl_domain;
struct rpl_instance *rpl_control_enumerate_instances(struct rpl_domain *domain, struct rpl_instance *instance);
bool rpl_control_read_dodag_info(const struct rpl_instance *instance, rpl_dodag_info_t *dodag_info);
const uint8_t *rpl_control_preferred_parent_addr(const struct rpl_instance *instance, bool global);

// The stack's internal enums of its statistics updates (header-only)
#include "nanostack/source/nwk_interface/protocol_stats.h"
#include "rcp/mac/ieee802154/sw_mac_internal.h"

#define RETRY_BINS 9            // 0..7 retries, then 8 or more

static struct {
    struct net_stats_cfg cfg;
    bool on;
    int8_t if_id, tasklet;
    bool is_br;
    uint16_t id;
    struct mac_api_s *mac;
    mac_statistics_t mac_stats;
    nwk_stats_t nwk;
    ws_statistics_t ws;
    // From the wrappers
    uint32_t mac_q, al_q;
    uint64_t mac_q_since, al_q_since;
    double mac_q_int, al_q_int;         // depth x µs
    uint32_t retry_hist[RETRY_BINS];
    uint32_t routed;
    char line[8192];
} ns = { .tasklet = -1 };

void net_stats_set_mac(struct mac_api_s *mac)
{
    ns.mac = mac;
}

// RPL control messages this node originates: DIS, DIO (trickle), DAO
enum { C_DIS, C_DIO, C_DAO, C_N };
static uint32_t ctl_count[C_N];

#ifdef MAC_INPROCESS
// Link-time wrappers (CMakeLists.txt: -Wl,--wrap of the in-process programs)
struct protocol_interface_rf_mac_setup;
void __real_sw_mac_stats_update(struct protocol_interface_rf_mac_setup *setup, int type, uint32_t update_val);
void __real_protocol_stats_update(int type, uint16_t update_val);

void __wrap_sw_mac_stats_update(struct protocol_interface_rf_mac_setup *setup, int type, uint32_t update_val)
{
    if (ns.on) {
        if (type == STAT_MAC_TX_QUEUE) {
            uint64_t now = app_load_now_us();
            ns.mac_q_int += (double)ns.mac_q * (now - ns.mac_q_since);
            ns.mac_q_since = now;
            ns.mac_q = update_val;
        } else if (type == STAT_MAC_TX_RETRY) {
            ns.retry_hist[update_val < RETRY_BINS - 1 ? update_val : RETRY_BINS - 1]++;
        }
    }
    __real_sw_mac_stats_update(setup, type, update_val);
}

void __wrap_protocol_stats_update(int type, uint16_t update_val)
{
    if (ns.on) {
        if (type == STATS_AL_TX_QUEUE_SIZE) {
            uint64_t now = app_load_now_us();
            ns.al_q_int += (double)ns.al_q * (now - ns.al_q_since);
            ns.al_q_since = now;
            ns.al_q = update_val;
        } else if (type == STATS_IP_ROUTE_UP) {
            ns.routed++;
        }
    }
    __real_protocol_stats_update(type, update_val);
}

// Calls into rpl_control.c from the other objects of the stack: every
// trickle DIO (rpl_upward.c), the DIS of the bootstrap and of RPL's parent
// search, every DAO (rpl_downward.c). DIS answering a DIO of an unknown DODAG
// and unicast DIOs answering a DIS (inside rpl_control.c) are not counted.
struct rpl_domain;
void __real_rpl_control_transmit_dio(void *domain, void *cur, uint8_t instance_id, uint8_t dodag_version, uint16_t rank,
                                     uint8_t g_mop_prf, uint8_t dtsn, void *dodag, const uint8_t dodagid[16],
                                     const void *conf, const uint8_t *dst);
void __wrap_rpl_control_transmit_dio(void *domain, void *cur, uint8_t instance_id, uint8_t dodag_version, uint16_t rank,
                                     uint8_t g_mop_prf, uint8_t dtsn, void *dodag, const uint8_t dodagid[16],
                                     const void *conf, const uint8_t *dst)
{
    ctl_count[C_DIO]++;
    __real_rpl_control_transmit_dio(domain, cur, instance_id, dodag_version, rank, g_mop_prf, dtsn, dodag, dodagid,
                                    conf, dst);
}

void __real_rpl_control_transmit_dis(void *domain, void *cur, uint8_t pred, uint8_t instance_id, const uint8_t *dodagid,
                                     const uint8_t version, const uint8_t *dst);
void __wrap_rpl_control_transmit_dis(void *domain, void *cur, uint8_t pred, uint8_t instance_id, const uint8_t *dodagid,
                                     const uint8_t version, const uint8_t *dst)
{
    ctl_count[C_DIS]++;
    __real_rpl_control_transmit_dis(domain, cur, pred, instance_id, dodagid, version, dst);
}

bool __real_rpl_control_transmit_dao(void *domain, void *cur, void *instance, uint8_t instance_id, uint8_t dao_sequence,
                                     const uint8_t dodagid[16], const uint8_t *opts, uint16_t opts_size,
                                     const uint8_t *dst);
bool __wrap_rpl_control_transmit_dao(void *domain, void *cur, void *instance, uint8_t instance_id, uint8_t dao_sequence,
                                     const uint8_t dodagid[16], const uint8_t *opts, uint16_t opts_size,
                                     const uint8_t *dst)
{
    ctl_count[C_DAO]++;
    return __real_rpl_control_transmit_dao(domain, cur, instance, instance_id, dao_sequence, dodagid, opts, opts_size,
                                           dst);
}
#endif

static int rsl_dbm(uint8_t v)
{
    return (int)v - 174;
}

static void print_line(bool final)
{
    uint64_t now = app_load_now_us();
    struct rpl_instance *inst = protocol_6lowpan_rpl_domain ?
                                rpl_control_enumerate_instances(protocol_6lowpan_rpl_domain, NULL) : NULL;
    rpl_dodag_info_t info;
    ws_stack_info_t wsi;
    const mac_statistics_t *m = &ns.mac_stats;
    const nwk_stats_t *w = &ns.nwk;
    char *b = ns.line;
    int len = sizeof(ns.line), n = 0, i;
    double mac_int = ns.mac_q_int + (double)ns.mac_q * (now - ns.mac_q_since);
    double al_int = ns.al_q_int + (double)ns.al_q * (now - ns.al_q_since);

    n += snprintf(b + n, len - n, "NET1 {\"v\":1,\"node\":%u,\"t_us\":%llu,\"final\":%s,", ns.id,
                  (unsigned long long)now, final ? "true" : "false");
    n += snprintf(b + n, len - n, "\"mac\":{\"tx\":%u,\"tx_bytes\":%u,\"rx\":%u,\"rx_bytes\":%u,\"bc_tx\":%u,\"bc_rx\":%u,"
                  "\"retries\":%u,\"tx_fail\":%u,\"cca_att\":%u,\"cca_fail\":%u,\"rx_drop\":%u,\"q\":%u,\"q_max\":%u,"
                  "\"q_int_us\":%.0f,\"lat_max_ms\":%u,\"retry_hist\":[",
                  m->mac_tx_count, m->mac_tx_bytes, m->mac_rx_count, m->mac_rx_bytes, m->mac_bc_tx_count,
                  m->mac_bc_rx_count, m->mac_retry_count, m->mac_tx_failed_count, m->mac_cca_attempts_count,
                  m->mac_failed_cca_count, m->mac_rx_drop_count, ns.mac_q, m->mac_tx_queue_peak, mac_int,
                  m->mac_tx_latency_max);
    for (i = 0; i < RETRY_BINS; i++)
        n += snprintf(b + n, len - n, "%s%u", i ? "," : "", ns.retry_hist[i]);
    n += snprintf(b + n, len - n, "]},\"al\":{\"q\":%u,\"q_max\":%u,\"q_int_us\":%.0f,\"red_drop\":%u,\"lat_max_s\":%u},",
                  ns.al_q, w->adapt_layer_tx_queue_peak, al_int, w->adapt_layer_tx_congestion_drop,
                  w->adapt_layer_tx_latency_max);
    n += snprintf(b + n, len - n, "\"ip\":{\"tx\":%u,\"rx\":%u,\"tx_bytes\":%u,\"rx_bytes\":%u,\"routed\":%u,"
                  "\"routed_bytes\":%u,\"no_route\":%u,\"rx_drop\":%u,\"frag_tx_err\":%u,\"frag_rx_err\":%u,"
                  "\"buf_fail\":%u},",
                  w->ip_tx_count, w->ip_rx_count, w->ip_tx_bytes, w->ip_rx_bytes, ns.routed, w->ip_routed_up,
                  w->ip_no_route, w->ip_rx_drop, w->frag_tx_errors, w->frag_rx_errors, w->buf_headroom_fail);
    n += snprintf(b + n, len - n, "\"rpl\":{");
    if (inst && rpl_control_read_dodag_info(inst, &info)) {
        const uint8_t *par = rpl_control_preferred_parent_addr(inst, false);
        unsigned inc = info.dag_min_hop_rank_inc ? info.dag_min_hop_rank_inc : 128;
        if (par)
            n += snprintf(b + n, len - n, "\"parent\":%u,", par[14] << 8 | par[15]);
        else
            n += snprintf(b + n, len - n, "\"parent\":null,");
        n += snprintf(b + n, len - n, "\"rank\":%u,\"dag_rank\":%u,\"min_hop_inc\":%u,",
                      info.curent_rank, info.curent_rank / inc, inc);
    } else {
        n += snprintf(b + n, len - n, "\"parent\":null,\"rank\":null,\"dag_rank\":null,");
    }
    n += snprintf(b + n, len - n, "\"parent_changes\":%u,\"parent_tx_fail\":%u,\"local_repair\":%u,"
                  "\"global_repair\":%u,\"etx_parent\":%u},",
                  w->rpl_route_routecost_better_change, w->rpl_parent_tx_fail, w->rpl_local_repair,
                  w->rpl_global_repair, w->etx_1st_parent);
    n += snprintf(b + n, len - n, "\"ws\":{");
    if (!ns.is_br && !ws_stack_info_get(ns.if_id, &wsi))
        n += snprintf(b + n, len - n, "\"routing_cost\":%u,\"rsl_in_dbm\":%d,\"rsl_out_dbm\":%d,\"join_state\":%u,",
                      wsi.routing_cost, rsl_dbm(wsi.rsl_in), rsl_dbm(wsi.rsl_out), wsi.join_state);
    n += snprintf(b + n, len - n, "\"pas_tx\":%u,\"pa_tx\":%u,\"pcs_tx\":%u,\"pc_tx\":%u,\"nbr_add\":%u,\"nbr_remove\":%u},",
                  ns.ws.sent_PAS, ns.ws.sent_PA, ns.ws.sent_PCS, ns.ws.sent_PC, ns.ws.Neighbour_add,
                  ns.ws.Neighbour_remove);
    n += snprintf(b + n, len - n, "\"ctl\":{\"dis\":%u,\"dio\":%u,\"dao\":%u},",
                  ctl_count[C_DIS], ctl_count[C_DIO], ctl_count[C_DAO]);
    if (n < len)
        n += app_load_counters_json(b + n, len - n);
    if (n < len - 2)
        snprintf(b + n, len - n, "}");
    printf("%s\n", b);
}

#define EV_INIT 0
#define EV_SAMPLE 1

static void ctl_stats(const char *args)
{
    (void)args;
    if (ns.on)
        print_line(false);
}

static void tasklet_cb(arm_event_t *ev)
{
    if (ev->event_type == EV_SAMPLE) {
        print_line(false);
        return;
    }
    if (ev->event_type != EV_INIT)
        return;
    if (ns.cfg.period_s > 0) {
        const arm_event_t e = { .receiver = ns.tasklet, .sender = ns.tasklet, .event_type = EV_SAMPLE,
                                .priority = ARM_LIB_LOW_PRIORITY_EVENT };
        if (!eventOS_event_timer_request_every(&e, eventOS_event_timer_ms_to_ticks(ns.cfg.period_s * 1000)))
            WARN("stats: eventOS_event_timer_request_every");
    }
}

static void at_exit(void)
{
    if (ns.on)
        print_line(true);
}

void net_stats_start(const struct net_stats_cfg *cfg, int8_t rcp_if_id, bool is_br, const uint8_t eui64[8])
{
    if (!cfg->on)
        return;
    ns.cfg = *cfg;
    ns.on = true;
    ns.if_id = rcp_if_id;
    ns.is_br = is_br;
    ns.id = is_br ? 0 : (eui64[6] << 8 | eui64[7]);
    ns.mac_q_since = ns.al_q_since = app_load_now_us();
#ifdef MAC_INPROCESS
    // Split programs: the MAC is in wshwsim, its counters stay 0 here
    if (ns.mac && ns_sw_mac_statistics_start(ns.mac, &ns.mac_stats))
        WARN("stats: ns_sw_mac_statistics_start");
#endif
    protocol_stats_start(&ns.nwk);
    if (ws_statistics_start(rcp_if_id, &ns.ws))
        WARN("stats: ws_statistics_start");
    ns.tasklet = eventOS_event_handler_create(tasklet_cb, EV_INIT);
    app_ctl_register("stats", ctl_stats);
    atexit(at_exit);
    INFO("stats: NET1 lines every %d s (0: at the end only)", cfg->period_s);
}
