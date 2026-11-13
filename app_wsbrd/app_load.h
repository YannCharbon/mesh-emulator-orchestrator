/* SPDX-License-Identifier: Apache-2.0
 *
 * Application load generator (docs/TRAFFIC_AND_REPORT.md): UDP flows between
 * the border router and the routers, with echo replies, so that a run
 * measures delivery, one-way latency and round-trip time per hop count and
 * per load step. Off unless load_flows is set. Every packet carries its flow,
 * sequence number, originator and virtual send time; each program prints one
 * "LOAD1" line per packet it sends, receives or gets an echo for
 * (sim_utils/report.py reads them).
 *
 * Configuration: conf keys load_* (one per setting, app_load_config()), given
 * by sim_utils/vtime/run.sh from its LOAD and LOAD_* variables.
 */
#ifndef APP_LOAD_H
#define APP_LOAD_H

#include <stdbool.h>
#include <stdint.h>

#define APP_LOAD_PORT 7010
#define APP_LOAD_HDR 32             // bytes of the packet header (the smallest payload)
#define APP_LOAD_MAX_PAYLOAD 1232   // at most this many bytes of UDP payload

enum app_load_flow {
    APP_LOAD_UP = 1,                // routers -> border router (echoed by it)
    APP_LOAD_DOWN = 2,              // border router -> each router, unicast (echoed by the router)
    APP_LOAD_MCAST = 3,             // border router -> multicast group (not echoed)
    APP_LOAD_P2P = 4,               // router -> another router (echoed by it)
};

struct app_load_cfg {
    unsigned flows;                 // bit (1 << flow): load_flows = up,down,mcast,p2p
    char nodes[1536];               // load_nodes: all | IDs and ranges "1-5,9" | a fraction "0.25"
    int max_id;                     // load_max_id: highest router ID (run.sh sets it; for p2p and fractions)
    int payload;                    // load_payload: UDP payload bytes (>= APP_LOAD_HDR)
    double interval_s;              // load_interval_s: mean interval per node and flow, step 0
    double down_interval_s;         // load_down_interval_s (0: interval_s), per target router
    double mcast_interval_s;        // load_mcast_interval_s (0: interval_s)
    double p2p_interval_s;          // load_p2p_interval_s (0: interval_s)
    bool exponential;               // load_dist = fixed | exp
    double jitter_s;                // load_jitter_s: first packet in [start, start + jitter) (< 0: one
                                    // interval; with exp, an exponential first gap)
    double start_s;                 // load_start_s: no packet before (virtual s since the run's start)
    double after_ready_s;           // load_after_ready_s: a router starts this long after its
                                    // ARM_NWK_BOOTSTRAP_READY (< 0: start_s only)
    double stop_s;                  // load_stop_s: no packet from then on (0: none)
    double ramp_step_s;             // load_ramp_step_s: steps of this length from start_s (0: no ramp)
    double ramp_factor;             // load_ramp_factor: the interval is divided by this at each step
    int ramp_steps;                 // load_ramp_steps: number of steps; no packet after the last
    bool echo;                      // load_echo: targets answer requests
    int hop_limit;                  // load_hop_limit: IPv6 hop limit of every packet
    uint8_t group[16];              // load_group: the multicast flow's group
    bool log_packets;               // load_log = packets | none: one LOAD1 line per packet
    bool control;                   // load_control: run the module even without flows, for
                                    // runtime commands (scenarios, docs/SCENARIOS.md)
    int tag;                        // load_step: the step byte of packets without a ramp
};

void app_load_defaults(struct app_load_cfg *cfg);
// One conf key without its "load_" prefix; returns NULL or an error message.
const char *app_load_config(struct app_load_cfg *cfg, const char *key, const char *value);
// Starts the generator if any flow is configured. is_br: the border router
// (node 0); rcp_if_id: the Wi-SUN interface; prefix: the border router's /64
// (NULL on routers: they take it from the DODAG ID); eui64: the program's.
void app_load_start(const struct app_load_cfg *cfg, bool is_br, int8_t rcp_if_id,
                    const uint8_t *prefix, const uint8_t eui64[8]);
// The router's interface event ARM_NWK_BOOTSTRAP_READY (each time it comes).
void app_load_ready(void);
// Per-flow counters for the NET1 lines (net_stats.c): appends "app":{...} to buf.
int app_load_counters_json(char *buf, int len);
bool app_load_active(void);
// A runtime command (the conductor's "xL" message, docs/SCENARIOS.md):
// "load set KEY=VALUE ...", "load stop". Returns NULL or an error message.
const char *app_load_command(const char *text);
// Other runtime commands ("VERB ARGS" in an "xL" message): stats (net_stats.c),
// tree (wsbr.c), iface (wsrouter.c)
void app_ctl_register(const char *verb, void (*fn)(const char *args));

// Virtual time in µs (wall-clock µs since start on the POSIX port).
uint64_t app_load_now_us(void);

#endif
