/* SPDX-License-Identifier: Apache-2.0
 *
 * A second client of the emulator's radio interface, in C, written from
 * docs/RADIO_INTERFACE.md (interface version 1) alone: no emulator or
 * Nanostack header; every message layout below comes from the document's §4.
 * It shows that a client in a stack's own language can be written from the
 * document, and it exchanges frames with the Python reference client.
 *
 * Protocol (a demonstration only): the node sends a frame every --period-ms
 * at --offset-ms of each period on its own clock, with a CCA before it (a busy
 * channel skips that frame). Between frames its receiver is on (--listen) or
 * its radio is off; then it turns the receiver on --cca-lead-us (1000) before
 * each frame, for the CCA. Output: one line per event, "<virtual us> <node> <event>
 * k=v ...", in the format of radio_if/ref_client.py.
 *
 *     cc -O2 -Wall -o rif_node rif_node.c
 *     rif_node --sock /tmp/sock --id 5 --chan 0 --period-ms 1000 --offset-ms 10 --listen
 */
#include <errno.h>
#include <getopt.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

/* ---- §3, §4: units and messages (packed, little-endian) ---- */

#define RIF_VERSION 1
#define RIF_NEVER UINT64_MAX
#define HELLO_VTIME 0x0001
#define HELLO_RADIO_IF 0x0002
#define WELCOME_POWER 0x0001
#define FRAME_MEDIUM 0x01
#define EV_CCA 0x01
#define MAX_PSDU 4096

enum { ST_OFF, ST_IDLE, ST_LISTEN, ST_RECEIVE, ST_TX };
static const char *const state_names[] = { "off", "idle", "listen", "receive", "tx" };
static const char *const causes[] = { "ok", "noise", "interference" };

struct __attribute__((packed)) msg_hello {          /* §4.2 */
    char magic[2];
    uint16_t node_id;
    uint16_t flags;
    uint16_t version;
};

struct __attribute__((packed)) msg_welcome {        /* §4.3 */
    char magic[2];
    uint16_t version;
    uint32_t flags;
    uint64_t seed;
    int32_t drift_ppb;
    uint32_t reserved;
    uint64_t start_us;
};

struct __attribute__((packed)) msg_wake {           /* §4.4 */
    char magic[2];
    uint16_t count;
    uint32_t reserved;
    uint64_t now_us;
};

struct __attribute__((packed)) msg_idle {           /* §4.5 */
    char magic[2];
    uint8_t reserved[6];
    uint64_t next_us;
};

struct __attribute__((packed)) msg_frame {          /* §4.6 */
    char magic[2];
    uint16_t len;
    uint16_t chan;
    uint16_t seq;
    uint16_t src;
    uint8_t shr_len;
    uint8_t fcs_len;
    uint8_t phr_len;
    int8_t tx_dbm;
    uint8_t flags;
    uint8_t reserved;
    uint32_t rate_bps;
    uint32_t frame_id;
    float rx_dbm;
    uint8_t psdu[];
};

struct __attribute__((packed)) msg_verdict {        /* §4.7 */
    char magic[2];
    uint8_t ok;
    uint8_t cause;
    uint32_t frame_id;
    float rx_dbm;
    float sinr_db;
};

struct __attribute__((packed)) msg_report {         /* §4.8 */
    char magic[2];
    uint8_t state;
    int8_t tx_dbm;
    uint8_t events;
    uint8_t phy_mode;
    uint16_t channel;
};

struct __attribute__((packed)) msg_sense {          /* §4.9 */
    char magic[2];
    uint16_t channel;
    float dbm;
};

struct __attribute__((packed)) msg_wake_report {    /* §4.10 */
    char magic[2];
    uint16_t events;
    uint16_t timers[6];
};

_Static_assert(sizeof(struct msg_hello) == 8, "hello");
_Static_assert(sizeof(struct msg_welcome) == 32, "welcome");
_Static_assert(sizeof(struct msg_wake) == 16, "wake");
_Static_assert(sizeof(struct msg_idle) == 16, "idle");
_Static_assert(sizeof(struct msg_frame) == 28, "frame header");
_Static_assert(sizeof(struct msg_verdict) == 16, "verdict");
_Static_assert(sizeof(struct msg_report) == 8, "report");
_Static_assert(sizeof(struct msg_sense) == 8, "sense");
_Static_assert(sizeof(struct msg_wake_report) == 16, "wake report");
_Static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__, "the interface is little-endian (§4.1)");

/* ---- the node ---- */

static struct {
    const char *sock;
    int id, chan, tx_dbm, shr, phr, fcs, payload_len;
    uint32_t rate;
    uint64_t period_us, offset_us, cca_lead_us;
    long max_frames;
    float cca_dbm;
    bool listen, cca;
} opt = {
    .chan = 0, .tx_dbm = 10, .shr = 8, .phr = 2, .fcs = 2, .payload_len = 16, .rate = 50000,
    .period_us = 1000000, .offset_us = 0, .cca_lead_us = 1000, .max_frames = -1, .cca_dbm = -85, .listen = false, .cca = true,
};

static int fd;
static uint64_t now, start_us;
static int32_t drift_ppb;
static bool power;
static int state = ST_OFF;
static uint16_t seq;
static long sent;
static uint64_t k_next;                     /* the next frame's period number */
static uint64_t t_tx = RIF_NEVER, t_tx_end = RIF_NEVER, t_rx_on = RIF_NEVER;
static struct msg_wake_report wr;
static struct {
    bool on;
    uint32_t frame_id;
    uint16_t src, len;
    uint8_t psdu[MAX_PSDU];
} held;

static void die(const char *what)
{
    fprintf(stderr, "node %d: %s: %s\n", opt.id, what, errno ? strerror(errno) : "protocol error");
    exit(2);
}

static void log_ev(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void log_ev(const char *fmt, ...)
{
    va_list ap;

    printf("%" PRIu64 " %d ", now, opt.id);
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    putchar('\n');
    fflush(stdout);
}

static void tx(const void *msg, size_t len)
{
    if (send(fd, msg, len, MSG_NOSIGNAL) != (ssize_t)len)
        die("send");
}

/* One message; returns its length. "xq" or a closed connection end the node (§4.11). */
static size_t rx(void *buf, size_t size)
{
    ssize_t len = recv(fd, buf, size, 0);

    if (len < 0)
        die("recv");
    if (len == 0 || (len == 2 && !memcmp(buf, "xq", 2))) {
        log_ev("end why=%s", len ? "end_of_the_run" : "connection_closed");
        exit(0);
    }
    return len;
}

static void report(int st, bool cca_event)
{
    struct msg_report r = { .magic = { 'x', 'r' }, .state = st, .tx_dbm = st == ST_TX ? opt.tx_dbm : 0,
                            .events = cca_event ? EV_CCA : 0, .channel = opt.chan };

    state = st;
    tx(&r, sizeof(r));
}

/* §6.3: the node's clock, and the first virtual time at which it reads at least local */
static uint64_t local_at(uint64_t v)
{
    int64_t e = v - start_us;
    int64_t p = e * (int64_t)drift_ppb;

    return e + (p >= 0 ? p / 1000000000 : -((-p + 999999999) / 1000000000)); /* floor */
}

static uint64_t virtual_at(uint64_t local)
{
    uint64_t v = start_us + (uint64_t)((long double)local * 1e9L / (1e9L + drift_ppb));

    while (local_at(v) < local)
        v++;
    while (v > start_us && local_at(v - 1) >= local)
        v--;
    return v;
}

static void arm_tx(void)
{
    if (opt.max_frames >= 0 && sent >= opt.max_frames) {
        t_tx = RIF_NEVER;
        return;
    }
    uint64_t local = k_next * opt.period_us + opt.offset_us;

    t_tx = virtual_at(local);
    /* §6.6: a CCA needs the receiver on, and the power report charges it in
     * the listening before it: without --listen, RX goes on that much earlier */
    if (!opt.listen && opt.cca)
        t_rx_on = virtual_at(local > opt.cca_lead_us ? local - opt.cca_lead_us : 0);
    k_next++;
}

static int rest_state(void)
{
    return opt.listen ? ST_LISTEN : ST_OFF;
}

static void on_tx(void)
{
    struct {
        struct msg_frame h;
        uint8_t psdu[MAX_PSDU];
    } f;
    uint64_t airtime;
    int n;

    arm_tx();
    if (state == ST_TX)
        return;
    if (opt.cca) {
        struct msg_sense s = { .magic = { 'x', 's' }, .channel = opt.chan };

        tx(&s, sizeof(s));
        if (rx(&s, sizeof(s)) != sizeof(s) || memcmp(s.magic, "xs", 2))
            die("expected the CCA answer");
        report(state, true);                 /* §6.6: the CCA, for the power report */
        if (s.dbm >= opt.cca_dbm) {
            log_ev("cca_busy dbm=%.1f", s.dbm);
            if (!held.on)
                report(rest_state(), false);
            return;
        }
    }
    if (held.on) {
        log_ev("rx_aborted src=%u frame=%" PRIu32, held.src, held.frame_id);
        held.on = false;                     /* §6.5: half duplex, no verdict will come */
    }
    seq++;
    memset(&f, 0, sizeof(f));
    n = snprintf((char *)f.psdu, sizeof(f.psdu), "C%dS%u", opt.id, seq);
    memset(f.psdu + n, '.', opt.payload_len > n ? opt.payload_len - n : 0);
    n = opt.payload_len > n ? opt.payload_len : n;
    memcpy(f.h.magic, "xx", 2);
    f.h.len = n;
    f.h.chan = opt.chan;
    f.h.seq = seq;
    f.h.shr_len = opt.shr;
    f.h.phr_len = opt.phr;
    f.h.fcs_len = opt.fcs;
    f.h.tx_dbm = opt.tx_dbm;
    f.h.rate_bps = opt.rate;
    report(ST_TX, false);                    /* §6.2: TX first, then the frame */
    tx(&f, sizeof(f.h) + n);
    airtime = (uint64_t)(opt.shr + opt.phr + n + opt.fcs) * 8 * 1000000 / opt.rate;
    t_tx_end = now + airtime;
    sent++;
    log_ev("tx seq=%u len=%d airtime_us=%" PRIu64, seq, n, airtime);
}

static void on_frame(const struct msg_frame *f, size_t len)
{
    if (len != sizeof(*f) + f->len || !(f->flags & FRAME_MEDIUM))
        die("malformed frame");
    if (held.on)                             /* §6.5: capture */
        log_ev("rx_captured src=%u frame=%" PRIu32, held.src, held.frame_id);
    else if (state != ST_LISTEN)
        log_ev("anomaly what=lock_while_not_listening state=%s", state_names[state]);
    held.on = true;
    held.frame_id = f->frame_id;
    held.src = f->src;
    held.len = f->len;
    memcpy(held.psdu, f->psdu, f->len);
    if (state != ST_RECEIVE)
        report(ST_RECEIVE, false);
    log_ev("rx_start src=%u frame=%" PRIu32 " rx_dbm=%.1f", f->src, f->frame_id, f->rx_dbm);
}

static void on_verdict(const struct msg_verdict *v)
{
    if (!held.on || held.frame_id != v->frame_id) {
        log_ev("verdict_ignored frame=%" PRIu32, v->frame_id);
        return;
    }
    held.on = false;
    if (v->ok) {
        /* as text if printable (this protocol's frames), else in hex (another stack's) */
        static char text[2 * MAX_PSDU + 5];
        int n = held.len, i;
        bool printable;

        while (n && held.psdu[n - 1] == '.')
            n--;
        printable = n > 0;
        for (i = 0; i < n; i++)
            printable &= held.psdu[i] > 0x20 && held.psdu[i] < 0x7f;
        if (printable) {
            memcpy(text, held.psdu, n);
            text[n] = '\0';
        } else {
            strcpy(text, "hex:");
            for (i = 0; i < held.len; i++)
                sprintf(text + 4 + 2 * i, "%02x", held.psdu[i]);
        }
        log_ev("rx_ok src=%u frame=%" PRIu32 " psdu=%s sinr_db=%.1f", held.src, v->frame_id, text, v->sinr_db);
    } else {
        log_ev("rx_lost src=%u frame=%" PRIu32 " cause=%s sinr_db=%.1f", held.src, v->frame_id,
               v->cause < 3 ? causes[v->cause] : "?", v->sinr_db);
    }
    report(rest_state(), false);
}

static void idle(void)
{
    struct msg_idle i = { .magic = { 'x', 'i' } };

    if (power) {                             /* §4.10, optional */
        memcpy(wr.magic, "xc", 2);
        tx(&wr, sizeof(wr));
    }
    memset(&wr, 0, sizeof(wr));
    i.next_us = t_tx < t_tx_end ? t_tx : t_tx_end;
    if (t_rx_on < i.next_us)
        i.next_us = t_rx_on;
    tx(&i, sizeof(i));
}

static void parse(int argc, char **argv)
{
    static const struct option longopts[] = {
        { "sock", 1, 0, 's' }, { "id", 1, 0, 'i' }, { "chan", 1, 0, 'c' }, { "period-ms", 1, 0, 'p' },
        { "offset-ms", 1, 0, 'o' }, { "frames", 1, 0, 'n' }, { "tx-dbm", 1, 0, 't' }, { "rate", 1, 0, 'r' },
        { "payload-len", 1, 0, 'l' }, { "cca-dbm", 1, 0, 'b' }, { "cca-lead-us", 1, 0, 'e' }, { "listen", 0, 0, 'L' }, { "no-cca", 0, 0, 'N' },
        { 0 }
    };
    int c;

    while ((c = getopt_long(argc, argv, "", longopts, NULL)) != -1) {
        switch (c) {
        case 's': opt.sock = optarg; break;
        case 'i': opt.id = atoi(optarg); break;
        case 'c': opt.chan = atoi(optarg); break;
        case 'p': opt.period_us = (uint64_t)(atof(optarg) * 1000); break;
        case 'o': opt.offset_us = (uint64_t)(atof(optarg) * 1000); break;
        case 'n': opt.max_frames = atol(optarg); break;
        case 't': opt.tx_dbm = atoi(optarg); break;
        case 'r': opt.rate = strtoul(optarg, NULL, 0); break;
        case 'l': opt.payload_len = atoi(optarg); break;
        case 'b': opt.cca_dbm = atof(optarg); break;
        case 'e': opt.cca_lead_us = strtoull(optarg, NULL, 0); break;
        case 'L': opt.listen = true; break;
        case 'N': opt.cca = false; break;
        default: exit(1);
        }
    }
    if (!opt.sock || opt.id < 0 || opt.id > 4095 || !opt.rate || opt.payload_len > MAX_PSDU) {
        fprintf(stderr, "usage: %s --sock PATH --id N [--chan C] [--period-ms P] [--offset-ms O] [--frames N]\n"
                "       [--tx-dbm D] [--rate BPS] [--payload-len L] [--cca-dbm D] [--cca-lead-us US] [--listen] [--no-cca]\n", argv[0]);
        exit(1);
    }
}

int main(int argc, char **argv)
{
    static uint8_t buf[sizeof(struct msg_frame) + MAX_PSDU];
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    struct msg_hello hello = { .magic = { 'x', 'h' }, .flags = HELLO_VTIME | HELLO_RADIO_IF, .version = RIF_VERSION };
    struct msg_welcome w;
    struct msg_wake a;
    unsigned int i;

    parse(argc, argv);
    fd = socket(AF_UNIX, SOCK_SEQPACKET, 0);
    if (fd < 0)
        die("socket");
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", opt.sock);
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)))
        die("connect");
    hello.node_id = opt.id;
    tx(&hello, sizeof(hello));
    if (rx(&w, sizeof(w)) != sizeof(w) || memcmp(w.magic, "xw", 2) || w.version != RIF_VERSION)
        die("expected a version-1 welcome");
    now = start_us = w.start_us;
    drift_ppb = w.drift_ppb;
    power = w.flags & WELCOME_POWER;
    log_ev("start seed=%" PRIu64 " drift_ppb=%" PRId32 " start_us=%" PRIu64, w.seed, drift_ppb, start_us);
    report(rest_state(), false);             /* the radio starts off (§6.1) */
    arm_tx();
    for (;;) {
        idle();
        if (rx(&a, sizeof(a)) != sizeof(a) || memcmp(a.magic, "xa", 2) || a.now_us < now)
            die("expected a wake");
        now = a.now_us;
        for (i = 0; i < a.count; i++) {
            size_t len = rx(buf, sizeof(buf));

            wr.events += wr.events < UINT16_MAX;
            if (len >= sizeof(struct msg_frame) && !memcmp(buf, "xx", 2))
                on_frame((const struct msg_frame *)buf, len);
            else if (len == sizeof(struct msg_verdict) && !memcmp(buf, "xv", 2))
                on_verdict((const struct msg_verdict *)buf);
            else
                die("unexpected message in a wake");
        }
        /* then the timers due now, earliest first (§5.2) */
        for (;;) {
            if (t_rx_on <= now && t_rx_on <= t_tx) {
                t_rx_on = RIF_NEVER;
                wr.timers[4]++;
                if (state == ST_OFF)
                    report(ST_LISTEN, false);
            } else if (t_tx_end <= now && t_tx_end <= t_tx) {
                t_tx_end = RIF_NEVER;
                wr.timers[4]++;
                report(rest_state(), false);
            } else if (t_tx <= now) {
                wr.timers[5]++;
                on_tx();
            } else {
                break;
            }
        }
    }
}
