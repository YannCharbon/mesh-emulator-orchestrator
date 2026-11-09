/* SPDX-License-Identifier: Apache-2.0
 *
 * Load client of the emulator's radio interface (docs/RADIO_INTERFACE.md,
 * version 1), for scale runs (docs/SCALE.md). It is a load generator shaped
 * like the traffic of a low-power alarm mesh, not a protocol:
 *
 *  - every node opens a receive window of --window-ms every --period-ms on its
 *    own clock (all nodes at the same local times), on channel k mod
 *    --channels for window k; its radio is off between windows;
 *  - the gateway (--gateway) sends a beacon every --beacon-s; a node that
 *    receives a beacon (or an alarm) for the first time relays it once, in its
 *    next window, after a random backoff and a CCA (a busy channel moves the
 *    frame to a later point of the window, or to the next window);
 *  - after each new beacon a node sends one status report, in a window drawn
 *    in the next --report-windows windows; reports are not relayed;
 *  - --alarm-at=SECONDS (repeatable) makes the node originate an alarm, which
 *    is flooded like a beacon.
 *
 * Frames sent in a window end before the window closes; a frame being
 * received when the window closes keeps the receiver on until its verdict.
 * Random numbers come from the welcome's seed and the node ID (§6.4), so a run
 * is reproducible.
 *
 * Output (--out FILE, else stdout), written at the end of the run:
 *   F <kind> <origin> <seq> <first rx virtual us> <hops>   first reception of a flood
 *   S id=.. tx=.. relays=.. reports=.. cca_busy=.. deferred=.. rx_ok=.. rx_lost=.. rx_captured=.. rx_aborted=.. wakes=.. dropped=..
 * and with --origins also one "O <kind> <origin> <seq> <tx virtual us>" per
 * flood the node originated.
 *
 *     cc -O2 -Wall -o rif_load rif_load.c
 */
#include <errno.h>
#include <getopt.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

/* ---- §3, §4: messages (packed, little-endian) ---- */

#define RIF_VERSION 1
#define RIF_NEVER UINT64_MAX
#define MAX_PSDU 4096

enum { ST_OFF, ST_IDLE, ST_LISTEN, ST_RECEIVE, ST_TX };

struct __attribute__((packed)) msg_hello { char magic[2]; uint16_t node_id, flags, version; };
struct __attribute__((packed)) msg_welcome {
    char magic[2]; uint16_t version; uint32_t flags; uint64_t seed; int32_t drift_ppb; uint32_t reserved; uint64_t start_us;
};
struct __attribute__((packed)) msg_wake { char magic[2]; uint16_t count; uint32_t reserved; uint64_t now_us; };
struct __attribute__((packed)) msg_idle { char magic[2]; uint8_t reserved[6]; uint64_t next_us; };
struct __attribute__((packed)) msg_frame {
    char magic[2]; uint16_t len, chan, seq, src; uint8_t shr_len, fcs_len, phr_len; int8_t tx_dbm;
    uint8_t flags, reserved; uint32_t rate_bps, frame_id; float rx_dbm; uint8_t psdu[];
};
struct __attribute__((packed)) msg_verdict { char magic[2]; uint8_t ok, cause; uint32_t frame_id; float rx_dbm, sinr_db; };
struct __attribute__((packed)) msg_report { char magic[2]; uint8_t state; int8_t tx_dbm; uint8_t events, phy_mode; uint16_t channel; };
struct __attribute__((packed)) msg_sense { char magic[2]; uint16_t channel; float dbm; };
struct __attribute__((packed)) msg_wake_report { char magic[2]; uint16_t events; uint16_t timers[6]; };

_Static_assert(sizeof(struct msg_welcome) == 32, "welcome");
_Static_assert(sizeof(struct msg_frame) == 28, "frame header");
_Static_assert(sizeof(struct msg_verdict) == 16, "verdict");

/* ---- the load protocol's frame (PSDU) ---- */

enum { K_BEACON = 'B', K_ALARM = 'A', K_REPORT = 'R' };

struct __attribute__((packed)) load_psdu {
    uint8_t magic;              /* 'L' */
    uint8_t kind;
    uint16_t origin;
    uint16_t seq;
    uint8_t hops;
    uint8_t pad[];
};

/* ---- options ---- */

#define MAX_ALARMS 16

static struct {
    const char *sock, *out;
    int id, gateway, channels, tx_dbm, shr, phr, fcs, payload_len, report_windows, max_hops;
    uint32_t rate;
    uint64_t period_us, window_us, beacon_us, cca_lead_us, guard_us;
    float cca_dbm;
    double relay_prob;
    int n_alarms;
    uint64_t alarm_at_us[MAX_ALARMS];
    bool origins;
} opt = {
    .gateway = 0, .channels = 1, .tx_dbm = 14, .shr = 8, .phr = 2, .fcs = 2, .payload_len = 20,
    .report_windows = 30, .max_hops = 32, .rate = 100000, .period_us = 1000000, .window_us = 20000,
    .beacon_us = 60000000, .cca_lead_us = 1000, .guard_us = 200, .cca_dbm = -85, .relay_prob = 1.0,
};

/* ---- state ---- */

static int fd;
static uint64_t now, start_us;
static int32_t drift_ppb;
static bool power;
static int state = ST_OFF;
static uint16_t chan;               /* the current window's channel */
static uint64_t rng;

static uint64_t win_k;              /* index of the next window to open */
static uint64_t cur_win = RIF_NEVER; /* index of the open window, or NEVER */
static uint64_t t_open = RIF_NEVER, t_close = RIF_NEVER, t_tx = RIF_NEVER, t_tx_end = RIF_NEVER;
static bool closing;                /* the window is over: off once the frame held or sent ends */

static struct msg_wake_report wr;
static struct { bool on; uint32_t frame_id; uint16_t src, len; uint8_t psdu[MAX_PSDU]; } held;

#define QMAX 64
static struct pending { uint8_t kind, hops; uint16_t origin, seq; uint64_t not_before; } q[QMAX];
static int qn;

#define SEEN_MAX 1024
static struct seen { uint8_t kind; uint16_t origin, seq; uint64_t t; uint8_t hops; } seen[SEEN_MAX];
static int seen_n;

static struct { uint64_t origin_t[MAX_ALARMS + 1024]; uint8_t origin_kind[MAX_ALARMS + 1024]; uint16_t origin_seq[MAX_ALARMS + 1024]; int n; } orig;
static uint16_t beacon_seq, alarm_seq, frame_seq;
static int alarms_done;
static struct { uint64_t tx, relays, reports, cca_busy, deferred, rx_ok, rx_lost, rx_captured, rx_aborted, wakes, dropped; } st;

static void die(const char *what)
{
    fprintf(stderr, "node %d: %s: %s\n", opt.id, what, errno ? strerror(errno) : "protocol error");
    exit(2);
}

static uint64_t mix64(uint64_t z)
{
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

static uint64_t rnd(void)
{
    return mix64(rng += 0x9e3779b97f4a7c15ULL);
}

static double rnd_unit(void)
{
    return (rnd() >> 11) * 0x1.0p-53;
}

static void tx(const void *msg, size_t len)
{
    if (send(fd, msg, len, MSG_NOSIGNAL) != (ssize_t)len)
        die("send");
}

static void summary(void);

static size_t rx(void *buf, size_t size)
{
    ssize_t len = recv(fd, buf, size, 0);

    if (len < 0)
        die("recv");
    if (len == 0 || (len == 2 && !memcmp(buf, "xq", 2))) {
        summary();
        exit(0);
    }
    return len;
}

static void report(int s, bool cca_event)
{
    struct msg_report r = { .magic = { 'x', 'r' }, .state = s, .tx_dbm = s == ST_TX ? opt.tx_dbm : 0,
                            .events = cca_event ? 1 : 0, .channel = chan };

    state = s;
    tx(&r, sizeof(r));
}

/* §6.3: the node's clock */
static uint64_t local_at(uint64_t v)
{
    int64_t e = v - start_us;
    __int128 p = (__int128)e * drift_ppb;

    return e + (int64_t)(p >= 0 ? p / 1000000000 : -((-p + 999999999) / 1000000000));
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

static uint64_t airtime_us(int len)
{
    return (uint64_t)(opt.shr + opt.phr + len + opt.fcs) * 8 * 1000000 / opt.rate;
}

static void enqueue(uint8_t kind, uint16_t origin, uint16_t seq, uint8_t hops, uint64_t not_before)
{
    if (qn == QMAX) {
        st.dropped++;
        return;
    }
    q[qn++] = (struct pending){ .kind = kind, .hops = hops, .origin = origin, .seq = seq, .not_before = not_before };
}

/* A random TX point in the open window, local offset in [cca_lead, window - airtime - guard] */
static void schedule_tx(uint64_t from_local_off)
{
    uint64_t air = airtime_us(opt.payload_len);
    uint64_t lo = from_local_off > opt.cca_lead_us ? from_local_off : opt.cca_lead_us;
    uint64_t hi = opt.window_us > air + opt.guard_us ? opt.window_us - air - opt.guard_us : 0;

    t_tx = RIF_NEVER;
    if (lo > hi)
        return;                 /* no room left in this window */
    t_tx = virtual_at(cur_win * opt.period_us + lo + rnd() % (hi - lo + 1));
}

static int eligible(void)
{
    int i;

    for (i = 0; i < qn; i++)
        if (q[i].not_before <= cur_win)
            return i;
    return -1;
}

static void open_window(void)
{
    cur_win = win_k++;
    t_open = virtual_at(win_k * opt.period_us);
    t_close = virtual_at(cur_win * opt.period_us + opt.window_us);
    closing = false;
    chan = cur_win % opt.channels;
    /* gateway beacon, alarms */
    if (opt.id == opt.gateway && cur_win % (opt.beacon_us / opt.period_us) == 0) {
        beacon_seq++;
        enqueue(K_BEACON, opt.id, beacon_seq, 0, cur_win);
    }
    while (alarms_done < opt.n_alarms && opt.alarm_at_us[alarms_done] <= cur_win * opt.period_us) {
        alarm_seq++;
        alarms_done++;
        enqueue(K_ALARM, opt.id, alarm_seq, 0, cur_win);
    }
    report(ST_LISTEN, false);
    if (eligible() >= 0)
        schedule_tx(0);
}

static void close_window(void)
{
    int i;

    t_close = RIF_NEVER;
    t_tx = RIF_NEVER;
    for (i = 0; i < qn; i++)
        if (q[i].not_before <= cur_win)
            st.deferred++;
    if (held.on || state == ST_TX) {
        closing = true;
        return;
    }
    cur_win = RIF_NEVER;
    report(ST_OFF, false);
}

static void after_frame(void)
{
    if (closing) {
        closing = false;
        cur_win = RIF_NEVER;
        report(ST_OFF, false);
    } else {
        report(ST_LISTEN, false);
        if (t_tx == RIF_NEVER && eligible() >= 0)
            schedule_tx(local_at(now) - cur_win * opt.period_us + 1);
    }
}

static void send_frame(void)
{
    struct { struct msg_frame h; uint8_t psdu[MAX_PSDU]; } f;
    struct msg_sense s = { .magic = { 'x', 's' } };
    struct load_psdu *p = (struct load_psdu *)f.psdu;
    uint64_t off;
    int i = eligible();

    t_tx = RIF_NEVER;
    if (i < 0)
        return;
    off = local_at(now) - cur_win * opt.period_us;
    if (held.on) {              /* receiving: try later in the window */
        st.cca_busy++;
        schedule_tx(off + 1);
        return;
    }
    s.channel = chan;
    tx(&s, sizeof(s));
    if (rx(&s, sizeof(s)) != sizeof(s) || memcmp(s.magic, "xs", 2))
        die("expected the CCA answer");
    report(state, true);
    if (s.dbm >= opt.cca_dbm) {
        st.cca_busy++;
        schedule_tx(off + 1);
        return;
    }
    memset(&f.h, 0, sizeof(f.h));
    memset(f.psdu, 0, opt.payload_len);
    p->magic = 'L';
    p->kind = q[i].kind;
    p->origin = q[i].origin;
    p->seq = q[i].seq;
    p->hops = q[i].hops;
    memcpy(f.h.magic, "xx", 2);
    f.h.len = opt.payload_len;
    f.h.chan = chan;
    f.h.seq = ++frame_seq;
    f.h.shr_len = opt.shr;
    f.h.phr_len = opt.phr;
    f.h.fcs_len = opt.fcs;
    f.h.tx_dbm = opt.tx_dbm;
    f.h.rate_bps = opt.rate;
    report(ST_TX, false);
    tx(&f, sizeof(f.h) + opt.payload_len);
    t_tx_end = now + airtime_us(opt.payload_len);
    st.tx++;
    if (q[i].origin == opt.id && q[i].kind != K_REPORT && orig.n < (int)(sizeof(orig.origin_t) / sizeof(*orig.origin_t))) {
        orig.origin_t[orig.n] = now;
        orig.origin_kind[orig.n] = q[i].kind;
        orig.origin_seq[orig.n++] = q[i].seq;
    }
    if (q[i].kind == K_REPORT)
        st.reports++;
    else if (q[i].origin != opt.id)
        st.relays++;
    memmove(&q[i], &q[i + 1], (qn - i - 1) * sizeof(*q));
    qn--;
}

static void on_payload(const uint8_t *psdu, int len)
{
    const struct load_psdu *p = (const struct load_psdu *)psdu;
    int i;

    if (len < (int)sizeof(*p) || p->magic != 'L' || (p->kind != K_BEACON && p->kind != K_ALARM))
        return;
    if (p->origin == opt.id)
        return;
    for (i = 0; i < seen_n; i++)
        if (seen[i].kind == p->kind && seen[i].origin == p->origin && seen[i].seq == p->seq)
            return;
    if (seen_n < SEEN_MAX)
        seen[seen_n++] = (struct seen){ .kind = p->kind, .origin = p->origin, .seq = p->seq, .t = now, .hops = p->hops + 1 };
    if (p->hops + 1 < opt.max_hops && rnd_unit() < opt.relay_prob)
        enqueue(p->kind, p->origin, p->seq, p->hops + 1, cur_win + 1);
    if (p->kind == K_BEACON && opt.id != opt.gateway)
        enqueue(K_REPORT, opt.id, p->seq, 0, cur_win + 1 + rnd() % opt.report_windows);
}

static void on_frame(const struct msg_frame *f, size_t len)
{
    if (len != sizeof(*f) + f->len)
        die("malformed frame");
    if (held.on)
        st.rx_captured++;
    held.on = true;
    held.frame_id = f->frame_id;
    held.src = f->src;
    held.len = f->len;
    memcpy(held.psdu, f->psdu, f->len);
    if (state != ST_RECEIVE)
        report(ST_RECEIVE, false);
}

static void on_verdict(const struct msg_verdict *v)
{
    if (!held.on || held.frame_id != v->frame_id)
        return;
    held.on = false;
    if (v->ok) {
        st.rx_ok++;
        on_payload(held.psdu, held.len);
    } else {
        st.rx_lost++;
    }
    after_frame();
}

static void summary(void)
{
    FILE *o = opt.out ? fopen(opt.out, "w") : stdout;
    int i;

    if (!o)
        die("open the output");
    for (i = 0; i < seen_n; i++)
        fprintf(o, "F %c %u %u %" PRIu64 " %u\n", seen[i].kind, seen[i].origin, seen[i].seq, seen[i].t, seen[i].hops);
    if (opt.origins)
        for (i = 0; i < orig.n; i++)
            fprintf(o, "O %c %d %u %" PRIu64 "\n", orig.origin_kind[i], opt.id, orig.origin_seq[i], orig.origin_t[i]);
    fprintf(o, "S id=%d tx=%" PRIu64 " relays=%" PRIu64 " reports=%" PRIu64 " cca_busy=%" PRIu64 " deferred=%" PRIu64
            " rx_ok=%" PRIu64 " rx_lost=%" PRIu64 " rx_captured=%" PRIu64 " rx_aborted=%" PRIu64 " wakes=%" PRIu64
            " dropped=%" PRIu64 "\n",
            opt.id, st.tx, st.relays, st.reports, st.cca_busy, st.deferred, st.rx_ok, st.rx_lost, st.rx_captured,
            st.rx_aborted, st.wakes, st.dropped);
    if (o != stdout)
        fclose(o);
}

static void idle(void)
{
    struct msg_idle i = { .magic = { 'x', 'i' } };
    uint64_t n = t_open;

    if (power) {
        memcpy(wr.magic, "xc", 2);
        tx(&wr, sizeof(wr));
    }
    memset(&wr, 0, sizeof(wr));
    if (t_close < n) n = t_close;
    if (t_tx < n) n = t_tx;
    if (t_tx_end < n) n = t_tx_end;
    i.next_us = n;
    tx(&i, sizeof(i));
}

static void parse(int argc, char **argv)
{
    static const struct option longopts[] = {
        { "sock", 1, 0, 's' }, { "id", 1, 0, 'i' }, { "gateway", 1, 0, 'g' }, { "channels", 1, 0, 'c' },
        { "period-ms", 1, 0, 'p' }, { "window-ms", 1, 0, 'w' }, { "beacon-s", 1, 0, 'b' },
        { "report-windows", 1, 0, 'R' }, { "alarm-at", 1, 0, 'a' }, { "tx-dbm", 1, 0, 't' },
        { "rate", 1, 0, 'r' }, { "payload-len", 1, 0, 'l' }, { "cca-dbm", 1, 0, 'C' },
        { "relay-prob", 1, 0, 'P' }, { "max-hops", 1, 0, 'H' }, { "out", 1, 0, 'o' }, { "origins", 0, 0, 'O' },
        { 0 }
    };
    int c;

    while ((c = getopt_long(argc, argv, "", longopts, NULL)) != -1) {
        switch (c) {
        case 's': opt.sock = optarg; break;
        case 'i': opt.id = atoi(optarg); break;
        case 'g': opt.gateway = atoi(optarg); break;
        case 'c': opt.channels = atoi(optarg); break;
        case 'p': opt.period_us = (uint64_t)(atof(optarg) * 1000); break;
        case 'w': opt.window_us = (uint64_t)(atof(optarg) * 1000); break;
        case 'b': opt.beacon_us = (uint64_t)(atof(optarg) * 1e6); break;
        case 'R': opt.report_windows = atoi(optarg); break;
        case 'a':
            if (opt.n_alarms == MAX_ALARMS) {
                fprintf(stderr, "at most %d --alarm-at\n", MAX_ALARMS);
                exit(1);
            }
            opt.alarm_at_us[opt.n_alarms++] = (uint64_t)(atof(optarg) * 1e6);
            break;
        case 't': opt.tx_dbm = atoi(optarg); break;
        case 'r': opt.rate = strtoul(optarg, NULL, 0); break;
        case 'l': opt.payload_len = atoi(optarg); break;
        case 'C': opt.cca_dbm = atof(optarg); break;
        case 'P': opt.relay_prob = atof(optarg); break;
        case 'H': opt.max_hops = atoi(optarg); break;
        case 'o': opt.out = optarg; break;
        case 'O': opt.origins = true; break;
        default: exit(1);
        }
    }
    if (!opt.sock || opt.id < 0 || opt.id > 4095 || !opt.rate || opt.channels < 1 || opt.report_windows < 1 ||
        opt.payload_len < (int)sizeof(struct load_psdu) || opt.payload_len > MAX_PSDU || !opt.period_us ||
        opt.window_us >= opt.period_us || opt.beacon_us < opt.period_us) {
        fprintf(stderr, "usage: %s --sock PATH --id N [--gateway ID] [--channels N] [--period-ms P] [--window-ms W]\n"
                "       [--beacon-s S] [--report-windows N] [--alarm-at SECONDS]... [--tx-dbm D] [--rate BPS]\n"
                "       [--payload-len L] [--cca-dbm D] [--relay-prob P] [--max-hops H] [--out FILE] [--origins]\n", argv[0]);
        exit(1);
    }
    /* alarms in time order */
    for (int i = 1; i < opt.n_alarms; i++)
        for (int j = i; j > 0 && opt.alarm_at_us[j] < opt.alarm_at_us[j - 1]; j--) {
            uint64_t x = opt.alarm_at_us[j];
            opt.alarm_at_us[j] = opt.alarm_at_us[j - 1];
            opt.alarm_at_us[j - 1] = x;
        }
}

int main(int argc, char **argv)
{
    static uint8_t buf[sizeof(struct msg_frame) + MAX_PSDU];
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    struct msg_hello hello = { .magic = { 'x', 'h' }, .flags = 0x0003, .version = RIF_VERSION };
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
    power = w.flags & 0x0001;
    rng = mix64(w.seed ^ 0x6c6f6164ULL) + mix64((uint64_t)opt.id + 1);
    win_k = (local_at(now) + opt.period_us - 1) / opt.period_us;
    t_open = virtual_at(win_k * opt.period_us);
    for (;;) {
        idle();
        if (rx(&a, sizeof(a)) != sizeof(a) || memcmp(a.magic, "xa", 2) || a.now_us < now)
            die("expected a wake");
        now = a.now_us;
        st.wakes++;
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
        /* timers due now, in a fixed order: TX end, window close, TX, window open */
        for (;;) {
            if (t_tx_end <= now) {
                t_tx_end = RIF_NEVER;
                wr.timers[4]++;
                after_frame();
            } else if (t_close <= now) {
                wr.timers[5]++;
                close_window();
            } else if (t_tx <= now) {
                wr.timers[4]++;
                send_frame();
            } else if (t_open <= now) {
                wr.timers[5]++;
                if (cur_win != RIF_NEVER) {     /* the previous window is still held: close it now */
                    if (held.on)
                        st.rx_aborted++;
                    held.on = false;
                    closing = false;
                    cur_win = RIF_NEVER;
                }
                open_window();
            } else {
                break;
            }
        }
    }
}
