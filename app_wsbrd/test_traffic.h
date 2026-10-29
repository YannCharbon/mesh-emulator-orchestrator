/* SPDX-License-Identifier: Apache-2.0
 *
 * Test traffic for virtual-time runs (GS8 stage 3): UDP requests to
 * configured addresses every test_udp_period_s, and an echo of every request
 * received on port TEST_UDP_PORT. Log lines "test-udp ..." are read by
 * sim_utils/vtime/test_lfn_join.sh. Off unless configured.
 */
#ifndef TEST_TRAFFIC_H
#define TEST_TRAFFIC_H

#include <stdbool.h>
#include <stdint.h>

#define TEST_UDP_PORT 7
#define TEST_UDP_DST_MAX 8

struct test_traffic_cfg {
    bool echo;                                  // answer requests (test_udp_echo)
    int period_s;                               // 0: send nothing (test_udp_period_s)
    int start_s;                                // first request after this (test_udp_start_s)
    int dst_count;
    uint8_t dst[TEST_UDP_DST_MAX][16];          // test_udp_dst (repeatable)
};

void test_traffic_start(const struct test_traffic_cfg *cfg);

#endif
