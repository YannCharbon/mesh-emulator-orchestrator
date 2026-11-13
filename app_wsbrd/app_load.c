/* SPDX-License-Identifier: Apache-2.0
 *
 * Application load generator (app_load.h, docs/TRAFFIC_AND_REPORT.md).
 *
 * Streams: one per (flow, target) this program originates, each with its next
 * send time, kept in a binary heap; one event-loop timer is armed for the
 * earliest. The border router has one DOWN stream per router it has a route
 * to (added as routes appear, checked every APP_LOAD_REFRESH_S), one MCAST
 * stream; a router one UP and one P2P stream. Random draws (start offsets,
 * exponential intervals, P2P peers, fractions of nodes) come from a generator
 * of this module seeded with the run's seed and the node's ID: the stack's
 * own random stream is never drawn from, and with no flow configured nothing
 * at all runs.
 *
 * Packet (little-endian, APP_LOAD_HDR bytes, then zero padding to load_payload):
 *   0 'L' 'D'  2 version (1)  3 type (0 request, 1 echo, 2 one-way)
 *   4 flow  5 load step  6 originator ID (u16)  8 target ID (u16, 0xffff: group)
 *   10 hop limit at send  11 reserved  12 sequence (u32)
 *   16 originator's send time (virtual µs, u64)
 *   24 echo: the target's receive time (virtual µs, u64); else 0
 *
 * Lines (after the port's time prefix), one per packet with load_log = packets:
 *   LOAD1 tx <flow> <seq> <src> <dst> <len> <step> <t_tx_us> <rc>
 *   LOAD1 skip <flow> <seq> <src> <dst> <len> <step> <t_us> <reason>
 *   LOAD1 rx <flow> <seq> <src> <dst> <len> <step> <t_tx_us> <t_rx_us> <hl_tx> <hl_rx>
 *   LOAD1 echo <flow> <seq> <src> <dst> <len> <step> <t_tx_us> <t_peer_rx_us> <t_rx_us> <hl_tx> <hl_rx>
 * src is the originator and dst the target in all four (an echo line is
 * printed by the originator, src = itself).
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "common/log.h"
#include "nanostack/socket_api.h"
#include "nanostack/net_interface.h"
#include "nanostack/net_rpl.h"
#include "nanostack/ws_bbr_api.h"
#include "nanostack-event-loop/eventOS_event.h"
#include "nanostack-event-loop/eventOS_event_timer.h"
#include "mbed-client-libservice/mbed-client-libservice/ip6string.h"
#ifdef NS_PORT_SIM
#include "ns_port_sim.h"
#endif
#include "app_load.h"

// The stack's RPL internals (nanostack/source/rpl/rpl_control.h)
struct rpl_domain;
struct rpl_instance;
extern struct rpl_domain *protocol_6lowpan_rpl_domain;
struct rpl_instance *rpl_control_enumerate_instances(struct rpl_domain *domain, struct rpl_instance *instance);
bool rpl_control_read_dodag_info(const struct rpl_instance *instance, rpl_dodag_info_t *dodag_info);

#define APP_LOAD_REFRESH_S 5            // border router: new routes checked this often
#define APP_LOAD_MAX_STREAMS 4100
#define APP_LOAD_FLOWS 5                // index 1..4
#define EV_INIT 0
#define EV_TIMER 1
#define REFRESH_FLOW 0                  // the border router's route check, in the heap as flow 0

static void join_group(void);
static void print_cfg(void);
static bool excluded(int id);
// Only the border router's library has it
#pragma weak ws_bbr_routing_table_get
void rf_ctl_message(const char *text, int len);

struct stream {
    uint64_t next_us;
    uint32_t seq;
    uint16_t peer;                      // target ID (0xffff: group)
    uint8_t flow;
    bool first_done;
};

struct flow_counters {
    uint32_t tx, tx_err, skip, rx, echo_tx, echo_rx, rx_bytes;
};

static struct {
    struct app_load_cfg cfg;
    bool on, is_br, ready;
    int8_t if_id, tasklet, sock;
    uint16_t id;
    uint8_t prefix[8];
    bool have_prefix;
    uint64_t rng;
    uint64_t ready_us;
    struct stream *streams;             // the heap: streams[0] is the earliest
    int n;
    arm_event_storage_t *armed;
    uint64_t armed_us;
    bool *down_known;                   // border router: routers with a DOWN stream
    struct flow_counters c[APP_LOAD_FLOWS];
    uint32_t flow_seq[APP_LOAD_FLOWS];
    uint64_t excluded[64];              // routers stopped by a scenario (load exclude)
    uint32_t sock_tx_done, sock_tx_fail, sock_no_route;
    uint8_t buf[APP_LOAD_MAX_PAYLOAD];
} ld = { .sock = -1, .tasklet = -1 };

uint64_t app_load_now_us(void)
{
#ifdef NS_PORT_SIM
    return ns_port_sim_now_us();
#else
    static uint64_t t0;
    struct timespec ts;
    uint64_t t;

    clock_gettime(CLOCK_MONOTONIC, &ts);
    t = (uint64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
    if (!t0)
        t0 = t;
    return t - t0;
#endif
}

static uint64_t run_seed(void)
{
#ifdef NS_PORT_SIM
    return ns_port_sim_seed();
#else
    const char *s = getenv("WSSIM_SEED");
    return s ? strtoull(s, NULL, 0) : 1;
#endif
}

static uint64_t mix64(uint64_t z)
{
    z += 0x9e3779b97f4a7c15ULL;
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

static double urand(void)   // [0, 1)
{
    ld.rng = mix64(ld.rng);
    return (ld.rng >> 11) * (1.0 / 9007199254740992.0);
}

/* ------------------------------------------------------------ configuration */

void app_load_defaults(struct app_load_cfg *cfg)
{
    memset(cfg, 0, sizeof(*cfg));
    cfg->nodes_kind = 0;
    cfg->payload = 64;
    cfg->interval_s = 30;
    cfg->jitter_s = -1;
    cfg->after_ready_s = 0;
    cfg->ramp_factor = 2;
    cfg->ramp_steps = 6;
    cfg->echo = true;
    cfg->hop_limit = 64;
    cfg->log_packets = true;
    cfg->control = false;
    // ff05::10ad: site-local, forwarded across the mesh by MPL
    cfg->group[0] = 0xff;
    cfg->group[1] = 0x05;
    cfg->group[14] = 0x10;
    cfg->group[15] = 0xad;
}

static const char *parse_double(const char *v, double *out, double min)
{
    char *end;
    double d = strtod(v, &end);

    if (end == v || *end || !isfinite(d) || d < min)
        return "invalid number";
    *out = d;
    return NULL;
}

static const char *parse_bool(const char *v, bool *out)
{
    if (!strcmp(v, "true") || !strcmp(v, "1") || !strcmp(v, "on") || !strcmp(v, "yes"))
        *out = true;
    else if (!strcmp(v, "false") || !strcmp(v, "0") || !strcmp(v, "off") || !strcmp(v, "no"))
        *out = false;
    else
        return "expected true or false";
    return NULL;
}

const char *app_load_config(struct app_load_cfg *cfg, const char *key, const char *value)
{
    double d;
    const char *err;

    if (!strcmp(key, "flows")) {
        char tmp[128], *tok, *save;
        if (strlen(value) >= sizeof(tmp))
            return "too long";
        strcpy(tmp, value);
        cfg->flows = 0;
        for (tok = strtok_r(tmp, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
            if (!strcmp(tok, "up"))
                cfg->flows |= 1u << APP_LOAD_UP;
            else if (!strcmp(tok, "down"))
                cfg->flows |= 1u << APP_LOAD_DOWN;
            else if (!strcmp(tok, "mcast"))
                cfg->flows |= 1u << APP_LOAD_MCAST;
            else if (!strcmp(tok, "p2p"))
                cfg->flows |= 1u << APP_LOAD_P2P;
            else if (!strcmp(tok, "none"))
                ;
            else
                return "flows: up, down, mcast, p2p or none, separated by commas";
        }
        return NULL;
    }
    if (!strcmp(key, "nodes")) {
        const char *v = value;
        char *end;

        if (!strcmp(value, "all")) {
            cfg->nodes_kind = 0;
            return NULL;
        }
        if (strchr(value, '.')) {
            double f = strtod(value, &end);
            if (*end || !(f >= 0 && f <= 1))
                return "nodes: a fraction from 0 to 1";
            cfg->nodes_kind = 2;
            cfg->nodes_fraction = f;
            return NULL;
        }
        memset(cfg->nodes_list, 0, sizeof(cfg->nodes_list));
        while (*v) {
            long lo = strtol(v, &end, 10), hi = lo;
            if (end == v)
                return "nodes: all, IDs and ranges (1-5,9), or a fraction";
            v = end;
            if (*v == '-') {
                hi = strtol(v + 1, &end, 10);
                if (end == v + 1)
                    return "nodes: a range needs its end";
                v = end;
            }
            if (lo < 0 || hi > 4095 || hi < lo)
                return "nodes: IDs from 0 to 4095, ranges ascending";
            for (long i = lo; i <= hi; i++)
                cfg->nodes_list[i / 64] |= 1ULL << (i % 64);
            if (*v == ',')
                v++;
            else if (*v)
                return "nodes: all, IDs and ranges (1-5,9), or a fraction";
        }
        cfg->nodes_kind = 1;
        return NULL;
    }
    if (!strcmp(key, "max_id")) {
        if ((err = parse_double(value, &d, 0)) || d > 4095)
            return err ? err : "at most 4095";
        cfg->max_id = (int)d;
        return NULL;
    }
    if (!strcmp(key, "payload")) {
        if ((err = parse_double(value, &d, APP_LOAD_HDR)) || d > APP_LOAD_MAX_PAYLOAD)
            return "payload: 32 to 1232 bytes";
        cfg->payload = (int)d;
        return NULL;
    }
    if (!strcmp(key, "interval_s")) {
        if ((err = parse_double(value, &cfg->interval_s, 0.001)))
            return "interval_s: at least 0.001";
        return NULL;
    }
    if (!strcmp(key, "down_interval_s"))
        return parse_double(value, &cfg->down_interval_s, 0);
    if (!strcmp(key, "mcast_interval_s"))
        return parse_double(value, &cfg->mcast_interval_s, 0);
    if (!strcmp(key, "p2p_interval_s"))
        return parse_double(value, &cfg->p2p_interval_s, 0);
    if (!strcmp(key, "dist")) {
        if (!strcmp(value, "fixed"))
            cfg->exponential = false;
        else if (!strcmp(value, "exp"))
            cfg->exponential = true;
        else
            return "dist: fixed or exp";
        return NULL;
    }
    if (!strcmp(key, "jitter_s"))
        return parse_double(value, &cfg->jitter_s, -1);
    if (!strcmp(key, "start_s"))
        return parse_double(value, &cfg->start_s, 0);
    if (!strcmp(key, "after_ready_s"))
        return parse_double(value, &cfg->after_ready_s, -1);
    if (!strcmp(key, "stop_s"))
        return parse_double(value, &cfg->stop_s, 0);
    if (!strcmp(key, "ramp_step_s"))
        return parse_double(value, &cfg->ramp_step_s, 0);
    if (!strcmp(key, "ramp_factor"))
        return parse_double(value, &cfg->ramp_factor, 1);
    if (!strcmp(key, "ramp_steps")) {
        if ((err = parse_double(value, &d, 1)) || d > 50)
            return "ramp_steps: 1 to 50";
        cfg->ramp_steps = (int)d;
        return NULL;
    }
    if (!strcmp(key, "echo"))
        return parse_bool(value, &cfg->echo);
    if (!strcmp(key, "hop_limit")) {
        if ((err = parse_double(value, &d, 2)) || d > 255)
            return "hop_limit: 2 to 255";
        cfg->hop_limit = (int)d;
        return NULL;
    }
    if (!strcmp(key, "group")) {
        if (!stoip6(value, strlen(value), cfg->group) || cfg->group[0] != 0xff)
            return "group: a multicast IPv6 address";
        return NULL;
    }
    if (!strcmp(key, "control"))
        return parse_bool(value, &cfg->control);
    if (!strcmp(key, "step")) {
        if ((err = parse_double(value, &d, 0)) || d > 255)
            return "step: 0 to 255";
        cfg->tag = (int)d;
        return NULL;
    }
    if (!strcmp(key, "log")) {
        if (!strcmp(value, "packets"))
            cfg->log_packets = true;
        else if (!strcmp(value, "none"))
            cfg->log_packets = false;
        else
            return "log: packets or none";
        return NULL;
    }
    return "unknown load_ key (docs/TRAFFIC_AND_REPORT.md)";
}

/* Is router <id> a member of load_nodes? A fraction is drawn per node from the
 * seed and the ID alone, so every program agrees. */
static bool is_member(int id)
{
    uint64_t h;

    if (id <= 0 || id > 4095)
        return false;
    switch (ld.cfg.nodes_kind) {
    case 0:
        return true;
    case 1:
        return ld.cfg.nodes_list[id / 64] >> (id % 64) & 1;
    default:
        h = mix64(run_seed() ^ mix64(0x4c4f4144ULL + (uint64_t)id));
        return (h >> 11) * (1.0 / 9007199254740992.0) < ld.cfg.nodes_fraction;
    }
}

static bool excluded(int id)
{
    return id >= 0 && id < 4096 && (ld.excluded[id / 64] >> (id % 64) & 1);
}

/* --------------------------------------------------------------- the heap */

static bool before(const struct stream *a, const struct stream *b)
{
    if (a->next_us != b->next_us)
        return a->next_us < b->next_us;
    if (a->flow != b->flow)
        return a->flow < b->flow;
    return a->peer < b->peer;
}

static void swap(int i, int j)
{
    struct stream t = ld.streams[i];

    ld.streams[i] = ld.streams[j];
    ld.streams[j] = t;
}

static void sift_down(int i)
{
    for (;;) {
        int l = 2 * i + 1, r = l + 1, m = i;
        if (l < ld.n && before(&ld.streams[l], &ld.streams[m]))
            m = l;
        if (r < ld.n && before(&ld.streams[r], &ld.streams[m]))
            m = r;
        if (m == i)
            return;
        swap(i, m);
        i = m;
    }
}

static void sift_up(int i)
{
    while (i > 0 && before(&ld.streams[i], &ld.streams[(i - 1) / 2])) {
        swap(i, (i - 1) / 2);
        i = (i - 1) / 2;
    }
}

static void arm(void)
{
    uint64_t now = app_load_now_us(), at;
    uint32_t ms;
    arm_event_t ev = { .receiver = ld.tasklet, .sender = ld.tasklet, .event_type = EV_TIMER,
                       .priority = ARM_LIB_LOW_PRIORITY_EVENT };

    if (!ld.n)
        return;
    at = ld.streams[0].next_us;
    if (ld.armed && ld.armed_us <= at)
        return;
    if (ld.armed)
        eventOS_cancel(ld.armed);
    ms = at > now ? (uint32_t)((at - now + 999) / 1000) : 0;
    ld.armed = eventOS_event_timer_request_at(&ev, eventOS_event_timer_ticks() + eventOS_event_timer_ms_to_ticks(ms));
    ld.armed_us = now + (uint64_t)ms * 1000;
    if (!ld.armed)
        WARN("load: eventOS_event_timer_request_at failed");
}

static void add_stream(uint8_t flow, uint16_t peer, uint64_t first_us)
{
    struct stream *s;

    if (ld.n >= APP_LOAD_MAX_STREAMS) {
        WARN("load: more than %d streams", APP_LOAD_MAX_STREAMS);
        return;
    }
    s = &ld.streams[ld.n];
    memset(s, 0, sizeof(*s));
    s->flow = flow;
    s->peer = peer;
    s->next_us = first_us;
    ld.n++;
    sift_up(ld.n - 1);
}

/* ------------------------------------------------------------- scheduling */

static double flow_interval_s(int flow)
{
    double v = 0;

    switch (flow) {
    case APP_LOAD_DOWN: v = ld.cfg.down_interval_s; break;
    case APP_LOAD_MCAST: v = ld.cfg.mcast_interval_s; break;
    case APP_LOAD_P2P: v = ld.cfg.p2p_interval_s; break;
    }
    return v > 0 ? v : ld.cfg.interval_s;
}

static uint64_t s_to_us(double s)
{
    return s <= 0 ? 0 : (uint64_t)(s * 1e6 + 0.5);
}

// The load step at t (0 without a ramp); -1 after the last step
static int step_at(uint64_t t)
{
    uint64_t start = s_to_us(ld.cfg.start_s);
    int k;

    if (ld.cfg.ramp_step_s <= 0)
        return ld.cfg.tag;
    if (t < start)
        return 0;
    k = (int)((t - start) / s_to_us(ld.cfg.ramp_step_s));
    return k < ld.cfg.ramp_steps ? k : -1;
}

static bool stopped(uint64_t t)
{
    if (ld.cfg.stop_s > 0 && t >= s_to_us(ld.cfg.stop_s))
        return true;
    return step_at(t) < 0;
}

static uint64_t next_gap_us(int flow, uint64_t t)
{
    // The ramp's step (not the tag of a load without a ramp)
    int k = ld.cfg.ramp_step_s > 0 ? step_at(t) : 0;
    double mean = flow_interval_s(flow) / pow(ld.cfg.ramp_factor, k < 0 ? 0 : k);
    double gap = ld.cfg.exponential ? -mean * log(1 - urand()) : mean;
    uint64_t us = s_to_us(gap);

    return us ? us : 1000;
}

// First send of a stream that starts at t0 (start_s, or the router's ready time)
static uint64_t first_send_us(int flow, uint64_t t0)
{
    double j = ld.cfg.jitter_s < 0 ? flow_interval_s(flow) : ld.cfg.jitter_s;

    // Poisson sends without a jitter given: the first gap is drawn as the
    // others (memoryless), so that a window's count has no start bias
    if (ld.cfg.exponential && ld.cfg.jitter_s < 0)
        return t0 + next_gap_us(flow, t0);
    return t0 + s_to_us(j * urand());
}

static uint64_t start_point_us(void)
{
    uint64_t t = s_to_us(ld.cfg.start_s);

    if (!ld.is_br && ld.cfg.after_ready_s >= 0) {
        uint64_t r = ld.ready_us + s_to_us(ld.cfg.after_ready_s);
        if (r > t)
            t = r;
    }
    return t;
}

/* ------------------------------------------------------------- addressing */

static bool dodag_id(uint8_t out[16])
{
    struct rpl_instance *inst = rpl_control_enumerate_instances(protocol_6lowpan_rpl_domain, NULL);
    rpl_dodag_info_t info;

    if (!protocol_6lowpan_rpl_domain || !inst || !rpl_control_read_dodag_info(inst, &info))
        return false;
    memcpy(out, info.dodag_id, 16);
    return true;
}

// A node's global address: the /64 and the IID of its EUI-64 (U/L bit
// flipped: the stack's addresses, docs/TRAFFIC_AND_REPORT.md)
static void node_addr(uint16_t id, uint8_t out[16])
{
    static const uint8_t iid[6] = { 0x01 ^ 0x02, 0x02, 0x03, 0x04, 0x05, 0x06 };

    memcpy(out, ld.prefix, 8);
    memcpy(out + 8, iid, 6);
    out[14] = id >> 8;
    out[15] = id & 0xff;
}

/* ---------------------------------------------------------------- packets */

static void put16(uint8_t *p, uint16_t v) { p[0] = v; p[1] = v >> 8; }
static void put32(uint8_t *p, uint32_t v) { put16(p, v); put16(p + 2, v >> 16); }
static void put64(uint8_t *p, uint64_t v) { put32(p, v); put32(p + 4, v >> 32); }
static uint16_t get16(const uint8_t *p) { return p[0] | p[1] << 8; }
static uint32_t get32(const uint8_t *p) { return get16(p) | (uint32_t)get16(p + 2) << 16; }
static uint64_t get64(const uint8_t *p) { return get32(p) | (uint64_t)get32(p + 4) << 32; }

static void send_one(struct stream *s, uint64_t now, int step)
{
    ns_address_t dst = { .type = ADDRESS_IPV6, .identifier = APP_LOAD_PORT };
    uint8_t *b = ld.buf;
    int len = ld.cfg.payload, rc;
    uint16_t peer = s->peer;
    uint8_t type = (s->flow == APP_LOAD_MCAST || !ld.cfg.echo) ? 2 : 0;
    const char *skip = NULL;

    if (s->flow == APP_LOAD_UP) {
        if (!dodag_id(dst.address))
            skip = "no_dodag";
    } else if (s->flow == APP_LOAD_P2P) {
        int span = ld.cfg.max_id, k;
        uint8_t d[16];
        peer = 0;
        // A peer: a member other than this router, drawn uniformly
        for (k = 0; k < 64 && span > 1 && !peer; k++) {
            int c = 1 + (int)(urand() * span);
            if (c != ld.id && is_member(c) && !excluded(c))
                peer = c;
        }
        if (!peer)
            skip = "no_peer";
        else if (!dodag_id(d))
            skip = "no_dodag";
        else {
            memcpy(ld.prefix, d, 8);
            node_addr(peer, dst.address);
        }
    } else if (s->flow == APP_LOAD_DOWN) {
        node_addr(peer, dst.address);
    } else {
        memcpy(dst.address, ld.cfg.group, 16);
    }
    // One sequence per flow and originator, across load changes: (flow,
    // originator, sequence) names a packet for the whole run
    s->seq = ++ld.flow_seq[s->flow];
    if (skip) {
        ld.c[s->flow].skip++;
        if (ld.cfg.log_packets)
            printf("LOAD1 skip %u %u %u %u %d %d %llu %s\n", s->flow, s->seq, ld.id, peer, len, step,
                   (unsigned long long)now, skip);
        return;
    }
    memset(b, 0, len);
    b[0] = 'L';
    b[1] = 'D';
    b[2] = 1;
    b[3] = type;
    b[4] = s->flow;
    b[5] = step;
    put16(b + 6, ld.id);
    put16(b + 8, peer);
    b[10] = ld.cfg.hop_limit;
    put32(b + 12, s->seq);
    put64(b + 16, now);
    rc = socket_sendto(ld.sock, &dst, b, len);
    if (rc < 0)
        ld.c[s->flow].tx_err++;
    else
        ld.c[s->flow].tx++;
    if (ld.cfg.log_packets)
        printf("LOAD1 tx %u %u %u %u %d %d %llu %d %u\n", s->flow, s->seq, ld.id, peer, len, step,
               (unsigned long long)now, rc, type);
}

static void refresh_routes(uint64_t now)
{
    static bbr_route_info_t table[4096];
    int len, i;

    if (!ws_bbr_routing_table_get)
        return;
    len = ws_bbr_routing_table_get(ld.if_id, table, 4096);
    for (i = 0; i < len; i++) {
        // The table holds IIDs: the last two bytes are the node's ID
        uint16_t id = table[i].target[6] << 8 | table[i].target[7];
        if (id < 4096 && !ld.down_known[id] && is_member(id) && !excluded(id)) {
            ld.down_known[id] = true;
            add_stream(APP_LOAD_DOWN, id, first_send_us(APP_LOAD_DOWN, now > s_to_us(ld.cfg.start_s) ? now : s_to_us(ld.cfg.start_s)));
        }
    }
}

static void on_timer(void)
{
    uint64_t now = app_load_now_us();

    ld.armed = NULL;
    while (ld.n && ld.streams[0].next_us <= now) {
        struct stream *s = &ld.streams[0];
        uint64_t gap;

        if (stopped(now)) {
            // no more packets from this stream (nor route checks)
            ld.streams[0] = ld.streams[--ld.n];
            sift_down(0);
            continue;
        }
        if (s->flow == REFRESH_FLOW) {
            s->next_us = now + APP_LOAD_REFRESH_S * 1000000ULL;
            sift_down(0);
            refresh_routes(now);
            continue;
        }
        send_one(s, now, step_at(now));
        // From the scheduled time, not the (up to 1 ms later) wake: the
        // offered rate is the one asked for. A stream far behind (more than
        // a second) starts again from now.
        gap = next_gap_us(s->flow, now);
        s->next_us = s->next_us + gap + 1000000 < now ? now + gap : s->next_us + gap;
        sift_down(0);
    }
    arm();
}

static void on_packet(const uint8_t *b, int len, const ns_address_t *src, int hl_rx)
{
    uint64_t now = app_load_now_us();
    uint8_t type, flow, step, hl_tx;
    uint16_t from, to;
    uint32_t seq;
    uint64_t t_tx;

    if (len < APP_LOAD_HDR || b[0] != 'L' || b[1] != 'D' || b[2] != 1)
        return;
    type = b[3];
    flow = b[4];
    step = b[5];
    from = get16(b + 6);
    to = get16(b + 8);
    hl_tx = b[10];
    seq = get32(b + 12);
    t_tx = get64(b + 16);
    if (flow >= APP_LOAD_FLOWS)
        return;
    if (type == 1) {
        ld.c[flow].echo_rx++;
        if (ld.cfg.log_packets)
            printf("LOAD1 echo %u %u %u %u %d %u %llu %llu %llu %u %d\n", flow, seq, from, to, len, step,
                   (unsigned long long)t_tx, (unsigned long long)get64(b + 24), (unsigned long long)now, hl_tx, hl_rx);
        return;
    }
    ld.c[flow].rx++;
    ld.c[flow].rx_bytes += len;
    if (ld.cfg.log_packets)
        printf("LOAD1 rx %u %u %u %u %d %u %llu %llu %u %d\n", flow, seq, from, ld.id, len, step,
               (unsigned long long)t_tx, (unsigned long long)now, hl_tx, hl_rx);
    if (type == 0) {
        // The echo: same packet, type 1, our receive time, our hop limit
        memcpy(ld.buf, b, len);
        ld.buf[3] = 1;
        put16(ld.buf + 8, ld.id);
        ld.buf[10] = ld.cfg.hop_limit;
        put64(ld.buf + 24, now);
        if (socket_sendto(ld.sock, (ns_address_t *)src, ld.buf, len) >= 0)
            ld.c[flow].echo_tx++;
        (void)to;
    }
}

static void socket_cb(void *arg)
{
    socket_callback_t *cb = arg;
    uint8_t ctrl[64];
    uint8_t rx[APP_LOAD_MAX_PAYLOAD];
    ns_address_t src;
    ns_iovec_t iov = { .iov_base = rx, .iov_len = sizeof(rx) };
    ns_msghdr_t msg = { .msg_name = &src, .msg_namelen = sizeof(src), .msg_iov = &iov, .msg_iovlen = 1,
                        .msg_control = ctrl, .msg_controllen = sizeof(ctrl) };
    ns_cmsghdr_t *cm;
    int len, hl = -1;

    switch (cb->event_type & SOCKET_EVENT_MASK) {
    case SOCKET_DATA:
        break;
    case SOCKET_TX_DONE:
        ld.sock_tx_done++;
        return;
    case SOCKET_TX_FAIL:
        ld.sock_tx_fail++;
        return;
    case SOCKET_NO_ROUTE:
        ld.sock_no_route++;
        return;
    default:
        return;
    }
    len = socket_recvmsg(ld.sock, &msg, 0);
    if (len <= 0)
        return;
    for (cm = NS_CMSG_FIRSTHDR(&msg); cm; cm = NS_CMSG_NXTHDR(&msg, cm))
        if (cm->cmsg_level == SOCKET_IPPROTO_IPV6 && cm->cmsg_type == SOCKET_IPV6_HOPLIMIT) {
            int16_t v;
            memcpy(&v, NS_CMSG_DATA(cm), sizeof(v));
            hl = v;
        }
    on_packet(rx, len, &src, hl);
}

static void tasklet_cb(arm_event_t *ev)
{
    if (ev->event_type == EV_TIMER) {
        on_timer();
        return;
    }
    if (ev->event_type != EV_INIT)
        return;
    ld.sock = socket_open(SOCKET_UDP, APP_LOAD_PORT, socket_cb);
    if (ld.sock < 0) {
        WARN("load: socket_open failed");
        return;
    }
    {
        int16_t hops = ld.cfg.hop_limit;
        bool on = true;
        socket_setsockopt(ld.sock, SOCKET_IPPROTO_IPV6, SOCKET_IPV6_UNICAST_HOPS, &hops, sizeof(hops));
        socket_setsockopt(ld.sock, SOCKET_IPPROTO_IPV6, SOCKET_IPV6_MULTICAST_HOPS, &hops, sizeof(hops));
        socket_setsockopt(ld.sock, SOCKET_IPPROTO_IPV6, SOCKET_IPV6_RECVHOPLIMIT, &on, sizeof(on));
    }
    join_group();
    if (ld.is_br) {
        if (ld.cfg.flows & 1u << APP_LOAD_DOWN) {
            ld.down_known = calloc(4096, sizeof(bool));
            add_stream(REFRESH_FLOW, 0, s_to_us(ld.cfg.start_s));
        }
        if (ld.cfg.flows & 1u << APP_LOAD_MCAST)
            add_stream(APP_LOAD_MCAST, 0xffff, first_send_us(APP_LOAD_MCAST, s_to_us(ld.cfg.start_s)));
        arm();
    }
    INFO("load: node %u, flows 0x%x, member %d, payload %d B, interval %.3f s (%s), start %.1f s, ramp %.1f s x %d / %.2f",
         ld.id, ld.cfg.flows, ld.is_br || is_member(ld.id), ld.cfg.payload, ld.cfg.interval_s,
         ld.cfg.exponential ? "exp" : "fixed", ld.cfg.start_s, ld.cfg.ramp_step_s, ld.cfg.ramp_steps, ld.cfg.ramp_factor);
    print_cfg();
    // A router whose start does not wait for its ready event
    if (!ld.is_br && ld.cfg.after_ready_s < 0)
        app_load_ready();
}

static void add_router_streams(void)
{
    uint64_t t0 = start_point_us();

    if (!is_member(ld.id) || excluded(ld.id))
        return;
    if (ld.cfg.flows & 1u << APP_LOAD_UP)
        add_stream(APP_LOAD_UP, 0, first_send_us(APP_LOAD_UP, t0));
    if (ld.cfg.flows & 1u << APP_LOAD_P2P)
        add_stream(APP_LOAD_P2P, 0, first_send_us(APP_LOAD_P2P, t0));
}

void app_load_ready(void)
{
    if (!ld.on || ld.is_br || ld.ready || ld.tasklet < 0 || ld.sock < 0)
        return;
    ld.ready = true;
    ld.ready_us = app_load_now_us();
    add_router_streams();
    arm();
}

static void join_group(void)
{
    static bool joined;
    ns_ipv6_mreq_t mreq = { .ipv6mr_interface = 0 };

    if (joined || ld.is_br || !(ld.cfg.flows & 1u << APP_LOAD_MCAST) || !is_member(ld.id))
        return;
    joined = true;
    memcpy(mreq.ipv6mr_multiaddr, ld.cfg.group, 16);
    WARN_ON(socket_setsockopt(ld.sock, SOCKET_IPPROTO_IPV6, SOCKET_IPV6_JOIN_GROUP, &mreq, sizeof(mreq)),
            "load: join group");
}

/* Every stream again from now, with the configuration as it is now */
static void restart_streams(void)
{
    uint64_t now = app_load_now_us();

    if (ld.armed)
        eventOS_cancel(ld.armed);
    ld.armed = NULL;
    ld.n = 0;
    ld.cfg.start_s = now / 1e6;
    join_group();
    if (ld.is_br) {
        if (ld.cfg.flows & 1u << APP_LOAD_DOWN) {
            if (!ld.down_known)
                ld.down_known = calloc(4096, sizeof(bool));
            memset(ld.down_known, 0, 4096 * sizeof(bool));
            add_stream(REFRESH_FLOW, 0, now);
        }
        if (ld.cfg.flows & 1u << APP_LOAD_MCAST)
            add_stream(APP_LOAD_MCAST, 0xffff, first_send_us(APP_LOAD_MCAST, now));
    } else if (ld.ready) {
        add_router_streams();
    }
    arm();
}

// The module's state for the report: "LOAD1 cfg <flows> <member> <echo> <step>"
static void print_cfg(void)
{
    if (ld.cfg.log_packets)
        printf("LOAD1 cfg %u %d %d %d\n", ld.cfg.flows, ld.is_br || (is_member(ld.id) && !excluded(ld.id)),
               ld.cfg.echo, ld.cfg.tag);
}

// Remove the streams for which drop() is true, keep the heap
static void drop_streams(bool (*drop)(const struct stream *s))
{
    int i, n = 0;

    for (i = 0; i < ld.n; i++)
        if (!drop(&ld.streams[i]))
            ld.streams[n++] = ld.streams[i];
    ld.n = n;
    for (i = ld.n / 2 - 1; i >= 0; i--)
        sift_down(i);
    if (ld.armed)
        eventOS_cancel(ld.armed);
    ld.armed = NULL;
    arm();
}

static bool drop_excluded(const struct stream *s)
{
    if (s->flow == REFRESH_FLOW || s->flow == APP_LOAD_MCAST)
        return false;
    if (!ld.is_br && excluded(ld.id))
        return true;        // this router no longer sends
    return s->flow == APP_LOAD_DOWN && excluded(s->peer);
}

static const char *parse_ids(const char *v, uint64_t *mask)
{
    struct app_load_cfg tmp;
    const char *err = app_load_config(&tmp, "nodes", v);

    if (err)
        return err;
    if (tmp.nodes_kind != 1)
        return "expected IDs and ranges";
    memcpy(mask, tmp.nodes_list, sizeof(tmp.nodes_list));
    return NULL;
}

const char *app_load_command(const char *text)
{
    char *tmp, *tok, *save, *eq;
    const char *err = NULL;
    struct app_load_cfg cfg;
    uint64_t mask[64];

    if (!ld.on || ld.sock < 0)
        return "load module not running (load_control)";
    tmp = strdup(text);
    if (!tmp)
        return "out of memory";
    tok = strtok_r(tmp, " ", &save);
    if (!tok || strcmp(tok, "load")) {
        err = "not a load command";
        goto out;
    }
    tok = strtok_r(NULL, " ", &save);
    if (tok && !strcmp(tok, "stop")) {
        ld.cfg.flows = 0;
        restart_streams();
        INFO("load: stopped (command)");
        print_cfg();
        goto out;
    }
    if (tok && (!strcmp(tok, "exclude") || !strcmp(tok, "include"))) {
        // Routers stopped or started again by a scenario: no streams to or
        // from them while excluded
        bool ex = !strcmp(tok, "exclude");
        tok = strtok_r(NULL, " ", &save);
        if (!tok || (err = parse_ids(tok, mask)))
            goto out;
        for (int i = 0; i < 64; i++)
            ld.excluded[i] = ex ? ld.excluded[i] | mask[i] : ld.excluded[i] & ~mask[i];
        if (ex) {
            drop_streams(drop_excluded);
        } else if (ld.is_br && ld.down_known) {
            for (int id = 0; id < 4096; id++)
                if (mask[id / 64] >> (id % 64) & 1)
                    ld.down_known[id] = false;     // added again at the next route check
        } else if (!ld.is_br && (mask[ld.id / 64] >> (ld.id % 64) & 1) && ld.ready) {
            add_router_streams();
            arm();
        }
        print_cfg();
        goto out;
    }
    if (!tok || strcmp(tok, "set")) {
        err = "expected load set KEY=VALUE ..., load stop, load exclude IDS or load include IDS";
        goto out;
    }
    // All or nothing: the settings are checked on a copy
    cfg = ld.cfg;
    while ((tok = strtok_r(NULL, " ", &save))) {
        eq = strchr(tok, '=');
        if (!eq) {
            err = "expected KEY=VALUE";
            goto out;
        }
        *eq = '\0';
        if ((err = app_load_config(&cfg, tok, eq + 1)))
            goto out;
    }
    cfg.ramp_step_s = 0;
    ld.cfg = cfg;
    restart_streams();
    INFO("load: set by command: flows 0x%x, step %d, payload %d B, interval %.4f s (%s), member %d",
         ld.cfg.flows, ld.cfg.tag, ld.cfg.payload, ld.cfg.interval_s, ld.cfg.exponential ? "exp" : "fixed",
         ld.is_br || is_member(ld.id));
    print_cfg();
out:
    free(tmp);
    return err;
}

static struct {
    const char *verb;
    void (*fn)(const char *args);
} ctl_verbs[8];

void app_ctl_register(const char *verb, void (*fn)(const char *args))
{
    for (int i = 0; i < 8; i++)
        if (!ctl_verbs[i].verb || !strcmp(ctl_verbs[i].verb, verb)) {
            ctl_verbs[i].verb = verb;
            ctl_verbs[i].fn = fn;
            return;
        }
    BUG("app_ctl_register: full");
}

// The conductor's "xL" message (app_wshwsim/sl_rf_driver.c: a weak symbol
// there): a text command for this program, "[#SEQ ]VERB [ARGS]". With a
// sequence number the program answers "CTL1 ok SEQ" or "CTL1 err SEQ reason".
void rf_ctl_message(const char *text, int len)
{
    char *cmd, *c, *sp;
    const char *err = NULL;
    long seq = -1;
    bool known = false;

    if (len <= 0)
        return;
    cmd = malloc(len + 1);
    if (!cmd)
        return;
    memcpy(cmd, text, len);
    cmd[len] = '\0';
    c = cmd;
    if (*c == '#') {
        seq = strtol(c + 1, &sp, 10);
        c = *sp == ' ' ? sp + 1 : sp;
    }
    if (!strncmp(c, "load ", 5)) {
        err = app_load_command(c);
        known = true;
    } else {
        sp = strchr(c, ' ');
        if (sp)
            *sp++ = '\0';
        for (int i = 0; i < 8 && ctl_verbs[i].verb; i++)
            if (!strcmp(ctl_verbs[i].verb, c)) {
                ctl_verbs[i].fn(sp ? sp : "");
                known = true;
                break;
            }
        if (!known)
            err = "unknown command";
    }
    if (err)
        WARN("control: %.200s: %s", c, err);
    if (seq >= 0) {
        if (err)
            printf("CTL1 err %ld %s\n", seq, err);
        else
            printf("CTL1 ok %ld\n", seq);
    }
    free(cmd);
}

void app_load_start(const struct app_load_cfg *cfg, bool is_br, int8_t rcp_if_id,
                    const uint8_t *prefix, const uint8_t eui64[8])
{
    if (!cfg->flows && !cfg->control)
        return;
    ld.cfg = *cfg;
    ld.on = true;
    ld.is_br = is_br;
    ld.if_id = rcp_if_id;
    ld.id = is_br ? 0 : (eui64[6] << 8 | eui64[7]);
    if (prefix) {
        memcpy(ld.prefix, prefix, 8);
        ld.have_prefix = true;
    }
    ld.rng = mix64(run_seed() ^ mix64(0x6c6f6164ULL + ld.id));
    ld.streams = calloc(APP_LOAD_MAX_STREAMS, sizeof(*ld.streams));
    BUG_ON(!ld.streams);
    ld.tasklet = eventOS_event_handler_create(tasklet_cb, EV_INIT);
}

bool app_load_active(void)
{
    return ld.on;
}

int app_load_counters_json(char *buf, int len)
{
    static const char *const names[APP_LOAD_FLOWS] = { "", "up", "down", "mcast", "p2p" };
    int n = 0, f;
    bool first = true;

    n += snprintf(buf + n, len - n, "\"app\":{");
    for (f = 1; f < APP_LOAD_FLOWS && n < len; f++) {
        struct flow_counters *c = &ld.c[f];
        if (!(ld.cfg.flows & 1u << f) && !c->rx && !c->echo_tx)
            continue;
        n += snprintf(buf + n, len - n, "%s\"%s\":{\"tx\":%u,\"tx_err\":%u,\"skip\":%u,\"rx\":%u,\"rx_bytes\":%u,\"echo_tx\":%u,\"echo_rx\":%u}",
                      first ? "" : ",", names[f], c->tx, c->tx_err, c->skip, c->rx, c->rx_bytes, c->echo_tx, c->echo_rx);
        first = false;
    }
    if (n < len)
        n += snprintf(buf + n, len - n, "%s\"sock\":{\"tx_done\":%u,\"tx_fail\":%u,\"no_route\":%u}}",
                      first ? "" : ",", ld.sock_tx_done, ld.sock_tx_fail, ld.sock_no_route);
    return n;
}

int app_queue_limit;
bool g_log_compact;

#ifdef MAC_INPROCESS
// queue_limit (app_load.h): the stack sizes its queues' random early
// detection from the heap, which is unbounded on a PC; a device drops much
// earlier. Linked with -Wl,--wrap=random_early_detection_create.
void *__real_random_early_detection_create(uint16_t threshold_min, uint16_t threshold_max, uint8_t drop_max_p,
                                           uint16_t weight);
void *__wrap_random_early_detection_create(uint16_t threshold_min, uint16_t threshold_max, uint8_t drop_max_p,
                                           uint16_t weight)
{
    if (app_queue_limit > 0) {
        INFO("queue_limit: random early detection %u..%u frames instead of %u..%u", app_queue_limit / 2,
             app_queue_limit, threshold_min, threshold_max);
        threshold_min = app_queue_limit / 2;
        threshold_max = app_queue_limit;
    }
    return __real_random_early_detection_create(threshold_min, threshold_max, drop_max_p, weight);
}
#endif
