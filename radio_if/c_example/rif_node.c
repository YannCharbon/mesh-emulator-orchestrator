/* SPDX-License-Identifier: Apache-2.0
 *
 * A second client of the emulator's radio interface, in C, written from
 * docs/RADIO_INTERFACE.md alone: no emulator or Nanostack header; every
 * message layout below comes from the document's §4. It shows that a client
 * in a stack's own language can be written from the document, and it
 * exchanges frames with the Python reference client. It speaks interface
 * version 1 by default, and version 2 with --if-version 2.
 *
 * Protocol (a demonstration only): the node sends a frame every --period-ms
 * at --offset-ms of each period on its own clock, with a CCA before it (a busy
 * channel skips that frame). Between frames its receiver is on (--listen) or
 * its radio is off; then it turns the receiver on --cca-lead-us (1000) before
 * each frame, for the CCA.
 *
 * Preamble sampling (version 2, §6.7): with --sniff-period-ms the radio is off
 * between frames and the node sniffs its channel for --sniff-us every period
 * (with the LOCK flag unless --no-lock), receiving the frames whose preamble
 * it detects; a sniff is skipped while the radio is busy or when a frame is
 * due within it. With --lp-preamble-ms its frames are sent with that wake-up
 * preamble ("xl"), which should be longer than the receivers' sniff period
 * plus their sniff.
 *
 * Output: one line per event, "<virtual us> <node> <event> k=v ...", in the
 * format of radio_if/ref_client.py.
 *
 *     cc -O2 -Wall -o rif_node rif_node.c
 *     rif_node --sock /tmp/sock --id 5 --chan 0 --period-ms 1000 --offset-ms 10 --listen
 *     rif_node --sock /tmp/sock --id 6 --if-version 2 --sniff-period-ms 100 --frames 0
 *     rif_node --sock /tmp/sock --id 7 --if-version 2 --lp-preamble-ms 110 --offset-ms 250
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

#define RIF_NEVER UINT64_MAX
#define HELLO_VTIME 0x0001
#define HELLO_RADIO_IF 0x0002
#define WELCOME_POWER 0x0001
#define WELCOME_SNIFF 0x0004                    /* §4.3, version 2 */
#define FRAME_MEDIUM 0x01
#define FRAME_LONG_PREAMBLE 0x02                /* §4.6, version 2 */
#define EV_CCA 0x01
#define SNIFF_LOCK 0x01                         /* §4.12 */
enum { NOT_LOCKED, LOCKED, RECEIVING };         /* §4.13 */
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
    uint32_t cca_us;                                /* 0 without power recording */
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

struct __attribute__((packed)) msg_sniff {          /* §4.12, version 2 */
    char magic[2];
    uint16_t channel;
    uint32_t duration_us;
    uint32_t sniff_id;
    uint8_t flags;
    uint8_t end_state;
    uint16_t reserved;
};

struct __attribute__((packed)) msg_sniff_result {   /* §4.13, version 2 */
    char magic[2];
    uint8_t detected;
    uint8_t locked;
    uint32_t sniff_id;
    uint32_t frame_id;
    uint16_t src;
    uint16_t reserved;
    uint64_t t_us;
    float rssi_dbm;
    float sinr_db;
};

struct __attribute__((packed)) msg_lp {             /* §4.14, version 2: a complete "xx" follows */
    char magic[2];
    uint16_t reserved;
    uint32_t preamble_us;
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
_Static_assert(sizeof(struct msg_sniff) == 16, "sniff");
_Static_assert(sizeof(struct msg_sniff_result) == 32, "sniff result");
_Static_assert(sizeof(struct msg_lp) == 8, "long-preamble header");
_Static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__, "the interface is little-endian (§4.1)");

/* ---- the node ---- */

static struct {
    const char *sock;
    int id, chan, tx_dbm, shr, phr, fcs, payload_len, version;
    uint32_t rate;
    uint64_t period_us, offset_us, cca_lead_us;
    uint64_t sniff_period_us, sniff_phase_us, lp_preamble_us;
    uint32_t sniff_us;
    long max_frames;
    float cca_dbm;
    bool listen, cca, lock;
} opt = {
    .chan = 0, .tx_dbm = 10, .shr = 8, .phr = 2, .fcs = 2, .payload_len = 16, .rate = 50000, .version = 1,
    .period_us = 1000000, .offset_us = 0, .cca_lead_us = 1000, .max_frames = -1, .cca_dbm = -85, .listen = false, .cca = true,
    .sniff_us = 1000, .lock = true,
};

static int fd;
static uint64_t now, start_us;
static int32_t drift_ppb;
static bool power;
static int state = ST_OFF;
static uint16_t seq;
static long sent;
static uint64_t k_next;                     /* the next frame's period number */
static uint64_t t_tx = RIF_NEVER, t_tx_end = RIF_NEVER, t_rx_on = RIF_NEVER, t_sniff = RIF_NEVER;
static uint64_t sniff_n;                    /* the next sniff's number, from 0 */
static struct {
    bool on;                                /* a sniff is in progress: one "xd" will come */
    uint32_t id;
} sniffing;
static bool lock_next;                      /* the "xd" said that a locked frame's "xx" follows */
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
    if (sniffing.on) {
        log_ev("sniff_ended id=%" PRIu32 " why=tx", sniffing.id);
        sniffing.on = false;                 /* §6.7: a frame ends the sniff, no "xd" */
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
    airtime = (uint64_t)(opt.shr + opt.phr + n + opt.fcs) * 8 * 1000000 / opt.rate;
    if (opt.lp_preamble_us) {
        /* §4.14: the 8-byte header and the complete "xx" in one message;
         * the transmission lasts the long preamble plus the frame (§6.7) */
        static uint8_t m[sizeof(struct msg_lp) + sizeof(f)];
        struct msg_lp lp = { .magic = { 'x', 'l' }, .preamble_us = opt.lp_preamble_us };

        memcpy(m, &lp, sizeof(lp));
        memcpy(m + sizeof(lp), &f, sizeof(f.h) + n);
        tx(m, sizeof(lp) + sizeof(f.h) + n);
        airtime += opt.lp_preamble_us;
    } else {
        tx(&f, sizeof(f.h) + n);
    }
    t_tx_end = now + airtime;
    sent++;
    log_ev("tx seq=%u len=%d airtime_us=%" PRIu64 " preamble_us=%" PRIu64, seq, n, airtime, opt.lp_preamble_us);
}

static void on_frame(const struct msg_frame *f, size_t len)
{
    bool via_sniff = lock_next;

    if (len != sizeof(*f) + f->len || !(f->flags & FRAME_MEDIUM))
        die("malformed frame");
    /* §4.6: a version-1 client gets MEDIUM only */
    if (opt.version < 2 && (f->flags & ~FRAME_MEDIUM))
        die("frame flags unknown to interface version 1");
    lock_next = false;
    if (via_sniff) {
        /* §6.7: locked by the sniff; the server has put the radio in RECEIVE */
        state = ST_RECEIVE;
    } else if (held.on)                      /* §6.5: capture */
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
    log_ev("rx_start src=%u frame=%" PRIu32 " rx_dbm=%.1f via=%s lp=%d", f->src, f->frame_id, f->rx_dbm,
           via_sniff ? "sniff" : "listen", !!(f->flags & FRAME_LONG_PREAMBLE));
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
    /* A frame that started during a sniff still in progress: the receiver
     * goes back to the sniff, which goes on (OFF would end it, §6.7) */
    report(sniffing.on ? ST_LISTEN : rest_state(), false);
}

/* §6.7: one sniff now, the next one a period later on the node's clock */
static void on_sniff_timer(void)
{
    struct msg_sniff m = { .magic = { 'x', 'n' }, .channel = opt.chan, .duration_us = opt.sniff_us,
                           .flags = opt.lock ? SNIFF_LOCK : 0, .end_state = ST_OFF };
    uint64_t k = sniff_n++;

    t_sniff = virtual_at(opt.sniff_phase_us + sniff_n * opt.sniff_period_us);
    if (state != ST_OFF || held.on) {
        log_ev("sniff_skipped id=%" PRIu64 " reason=%s", k, state_names[state]);
        return;
    }
    /* a frame or its CCA due within the window: the radio is needed then */
    if (t_tx < now + opt.sniff_us || t_rx_on < now + opt.sniff_us) {
        log_ev("sniff_skipped id=%" PRIu64 " reason=tx_due", k);
        return;
    }
    m.sniff_id = (uint32_t)k;
    tx(&m, sizeof(m));
    sniffing.on = true;
    sniffing.id = m.sniff_id;
    state = ST_LISTEN;                       /* applied by the server from the "xn" */
    log_ev("sniff id=%" PRIu64 " us=%" PRIu32, k, opt.sniff_us);
}

static void on_sniff_result(const struct msg_sniff_result *d)
{
    if (!sniffing.on || d->sniff_id != sniffing.id) {
        log_ev("anomaly what=unexpected_sniff_result id=%" PRIu32, d->sniff_id);
        return;
    }
    sniffing.on = false;
    if (!d->detected) {
        log_ev("sniff_none id=%" PRIu32 " rssi_dbm=%.2f", d->sniff_id, d->rssi_dbm);
        if (!held.on)
            state = ST_OFF;                  /* the request's end state, applied by the server */
        return;
    }
    log_ev("sniff_detect id=%" PRIu32 " src=%u frame=%" PRIu32 " locked=%u t_us=%" PRIu64 " rssi_dbm=%.1f sinr_db=%.1f",
           d->sniff_id, d->src, d->frame_id, d->locked, d->t_us, d->rssi_dbm, d->sinr_db);
    if (d->locked == LOCKED)
        lock_next = true;                    /* its "xx" is the next message */
    else if (d->locked == NOT_LOCKED && !held.on)
        report(ST_OFF, false);               /* detected, too late to receive: the radio stays in LISTEN until told */
    /* RECEIVING: the frame is held already, its verdict will come */
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
    if (t_sniff < i.next_us)
        i.next_us = t_sniff;
    tx(&i, sizeof(i));
}

static void parse(int argc, char **argv)
{
    static const struct option longopts[] = {
        { "sock", 1, 0, 's' }, { "id", 1, 0, 'i' }, { "chan", 1, 0, 'c' }, { "period-ms", 1, 0, 'p' },
        { "offset-ms", 1, 0, 'o' }, { "frames", 1, 0, 'n' }, { "tx-dbm", 1, 0, 't' }, { "rate", 1, 0, 'r' },
        { "payload-len", 1, 0, 'l' }, { "cca-dbm", 1, 0, 'b' }, { "cca-lead-us", 1, 0, 'e' }, { "listen", 0, 0, 'L' }, { "no-cca", 0, 0, 'N' },
        { "if-version", 1, 0, 'v' }, { "sniff-period-ms", 1, 0, 'P' }, { "sniff-us", 1, 0, 'u' },
        { "sniff-phase-ms", 1, 0, 'h' }, { "no-lock", 0, 0, 'K' }, { "lp-preamble-ms", 1, 0, 'g' },
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
        case 'v': opt.version = atoi(optarg); break;
        case 'P': opt.sniff_period_us = (uint64_t)(atof(optarg) * 1000); break;
        case 'u': opt.sniff_us = strtoul(optarg, NULL, 0); break;
        case 'h': opt.sniff_phase_us = (uint64_t)(atof(optarg) * 1000); break;
        case 'K': opt.lock = false; break;
        case 'g': opt.lp_preamble_us = (uint64_t)(atof(optarg) * 1000); break;
        default: exit(1);
        }
    }
    if (!opt.sock || opt.id < 0 || opt.id > 4095 || !opt.rate || opt.payload_len > MAX_PSDU ||
        opt.version < 1 || opt.version > 2 || ((opt.sniff_period_us || opt.lp_preamble_us) && opt.version < 2) ||
        (opt.sniff_period_us && (opt.listen || !opt.sniff_us)) || opt.lp_preamble_us > UINT32_MAX) {
        fprintf(stderr, "usage: %s --sock PATH --id N [--chan C] [--period-ms P] [--offset-ms O] [--frames N]\n"
                "       [--tx-dbm D] [--rate BPS] [--payload-len L] [--cca-dbm D] [--cca-lead-us US] [--listen] [--no-cca]\n"
                "       [--if-version 1|2] [--sniff-period-ms P [--sniff-us US] [--sniff-phase-ms PH] [--no-lock]]\n"
                "       [--lp-preamble-ms MS]   (sniffing and long preambles need --if-version 2, not --listen)\n",
                argv[0]);
        exit(1);
    }
}

int main(int argc, char **argv)
{
    static uint8_t buf[sizeof(struct msg_frame) + MAX_PSDU];
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    struct msg_hello hello = { .magic = { 'x', 'h' }, .flags = HELLO_VTIME | HELLO_RADIO_IF };
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
    hello.version = opt.version;
    tx(&hello, sizeof(hello));
    /* §9, §13: a server that cannot serve this version closes the connection */
    if (rx(&w, sizeof(w)) != sizeof(w) || memcmp(w.magic, "xw", 2) || w.version != opt.version)
        die("expected a welcome of the interface version asked for");
    now = start_us = w.start_us;
    drift_ppb = w.drift_ppb;
    power = w.flags & WELCOME_POWER;
    /* §13: sniffing needs version 2 and the welcome's SNIFF */
    if ((opt.sniff_period_us || opt.lp_preamble_us) && !(w.flags & WELCOME_SNIFF))
        die("the server does not serve preamble sniffing (no SNIFF in the welcome)");
    log_ev("start seed=%" PRIu64 " drift_ppb=%" PRId32 " start_us=%" PRIu64 " version=%u can_sniff=%d", w.seed,
           drift_ppb, start_us, w.version, !!(w.flags & WELCOME_SNIFF));
    report(rest_state(), false);             /* the radio starts off (§6.1) */
    arm_tx();
    if (opt.sniff_period_us)
        t_sniff = virtual_at(opt.sniff_phase_us);
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
            else if (opt.version >= 2 && len == sizeof(struct msg_sniff_result) && !memcmp(buf, "xd", 2))
                on_sniff_result((const struct msg_sniff_result *)buf);
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
            } else if (t_sniff <= now && t_sniff <= t_tx) {
                wr.timers[4]++;
                on_sniff_timer();
            } else if (t_tx <= now) {
                wr.timers[5]++;
                on_tx();
            } else {
                break;
            }
        }
    }
}
