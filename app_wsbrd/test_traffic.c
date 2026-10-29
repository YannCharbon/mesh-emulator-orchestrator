/* SPDX-License-Identifier: Apache-2.0
 *
 * Test traffic for virtual-time runs (test_traffic.h).
 */
#include <stdio.h>
#include <string.h>
#include "common/log.h"
#include "nanostack/socket_api.h"
#include "nanostack/net_interface.h"
#include "nanostack-event-loop/eventOS_event.h"
#include "nanostack-event-loop/eventOS_event_timer.h"
#include "mbed-client-libservice/mbed-client-libservice/ip6string.h"
#include "test_traffic.h"

enum { EV_INIT = 0, EV_SEND = 1 };

static struct test_traffic_cfg tt;
static int8_t tt_sock = -1;
static int8_t tt_tasklet = -1;
static uint32_t tt_seq;

static void tt_socket_cb(void *arg)
{
    socket_callback_t *cb = arg;
    uint8_t buf[64];
    ns_address_t src;
    char a[40];
    int len;

    if ((cb->event_type & SOCKET_EVENT_MASK) != SOCKET_DATA) {
        return;
    }
    len = socket_recvfrom(tt_sock, buf, sizeof(buf) - 1, 0, &src);
    if (len <= 0) {
        return;
    }
    buf[len] = 0;
    ip6tos(src.address, a);
    INFO("test-udp rx from %s: %s", a, (char *)buf);
    if (buf[0] == 'Q' && tt.echo) {
        buf[0] = 'R';
        socket_sendto(tt_sock, &src, buf, len);
        INFO("test-udp tx to %s: %s", a, (char *)buf);
    }
}

static void tt_send(void)
{
    ns_address_t dst = { .type = ADDRESS_IPV6, .identifier = TEST_UDP_PORT };
    char buf[40], a[40];
    int i, len;

    for (i = 0; i < tt.dst_count; i++) {
        memcpy(dst.address, tt.dst[i], 16);
        len = snprintf(buf, sizeof(buf), "Q %u", (unsigned)tt_seq++);
        ip6tos(dst.address, a);
        INFO("test-udp tx to %s: %s (%d)", a, buf, socket_sendto(tt_sock, &dst, buf, len));
    }
}

static void tt_tasklet_cb(arm_event_t *ev)
{
    if (ev->event_type == EV_INIT) {
        tt_sock = socket_open(SOCKET_UDP, TEST_UDP_PORT, tt_socket_cb);
        if (tt_sock < 0) {
            WARN("test-udp: socket_open failed");
            return;
        }
        if (tt.period_s > 0 && tt.dst_count) {
            eventOS_event_timer_request(EV_SEND, EV_SEND, tt_tasklet, (uint32_t)(tt.start_s > 0 ? tt.start_s : tt.period_s) * 1000);
        }
        return;
    }
    if (ev->event_type == EV_SEND) {
        tt_send();
        eventOS_event_timer_request(EV_SEND, EV_SEND, tt_tasklet, (uint32_t)tt.period_s * 1000);
    }
}

void test_traffic_start(const struct test_traffic_cfg *cfg)
{
    if (!cfg->echo && !(cfg->period_s > 0 && cfg->dst_count)) {
        return;
    }
    tt = *cfg;
    tt_tasklet = eventOS_event_handler_create(tt_tasklet_cb, EV_INIT);
}
