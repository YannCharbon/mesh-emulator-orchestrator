/* SPDX-License-Identifier: Apache-2.0
 *
 * Central radio medium of the conductor: see sim_medium.h and
 * docs/MEDIUM.md.
 *
 * Units: time in µs, powers in dBm at the receiver's antenna, summed in mW.
 *
 * A frame on air (struct med_frame) knows its receivers: the sender's graph
 * neighbours that hear it (not cut, not dropped), each with the power it
 * gets. It stays in the list of frames on air until its end. A lock (struct
 * med_lock) is a receiver receiving a frame: it copies what the verdict needs
 * from the frame, and the start, end and power of every interferer, so that
 * it does not depend on the frame list once the frames have ended. A lock is
 * freed when its verdict event leaves the conductor's queue.
 */
#define _GNU_SOURCE
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "common/log.h"
#include "sim_medium.h"

// kTB at 293 K and 100 kHz, NF 4 dB: the constant of the radio's loss curve
// (app_wshwsim/sl_rf_driver.c)
#define MED_NOISE_DBM (-153.930645 + 30 + 4)
// Separates the verdict streams from the port's random source and the drift
#define MED_RNG_K 0x6d656469756d0001ULL

enum med_outcome {
    MED_OK,
    MED_NOISE,
    MED_INTERFERENCE,
    MED_CAPTURED,
    MED_BELOW_LOCK,
    MED_BUSY,
    MED_NOT_LISTENING,
    MED_TX_ABORT,
    MED_LEFT,
    MED_OUTCOMES
};

static const char *const outcome_names[MED_OUTCOMES] = {
    "ok", "noise", "interference", "captured", "below_lock", "busy", "not_listening", "tx_abort", "left"
};

struct med_rx {
    int node;
    float dbm;
    double mw;
};

struct med_frame {
    uint32_t id;
    int src;
    uint16_t chan;
    uint64_t t_start, t_end;
    uint64_t shr_us;            // airtime of the SHR (and of an "xl" preamble): the capture window
    uint64_t t_bits;            // where the counted bits start: t_start, or the end of an "xl" preamble
    uint64_t pre_end;           // end of the preamble: the SFD follows (§10)
    unsigned int bits;          // counted bits: 8 · (PHR + PSDU + FCS)
    uint32_t rate_bps;
    int8_t tx_dbm;
    void *msg;                  // the "xx" message, for a lock made by a sniff (§10)
    int len;
    int n_rx, cap_rx;
    struct med_rx *rx;          // ascending node IDs
    uint64_t rx_mask[SIM_MAX_NODES / 64]; // the nodes in rx: a quick test before the search
    struct med_frame *next;
};

struct med_intf {
    uint64_t start, end;
    double mw;
};

struct med_lock {
    int node, src;
    uint32_t frame_id;
    uint16_t chan;
    uint64_t t_start, t_end, shr_us;
    uint64_t t_bits;            // the counted bits are spread over [t_bits, t_end)
    unsigned int bits;
    float dbm;
    double mw;
    bool active;                // false once captured, aborted or left
    bool delivered;             // the receiver ran after its "xx"
    int n_intf, cap_intf;
    struct med_intf *intf;
};

static struct {
    bool given, tuning, central, mode_given;
    bool has_tx_ref;
    float tx_ref, lock_snr, capture_db;
    const char *log_path;
    FILE *log;
    uint64_t seed;
    const uint8_t *cca;
    struct node_infos *links;
    double noise_mw;
    uint32_t next_id;
    struct med_frame *air;      // frames on air, newest first
    struct med_lock *locks[SIM_MAX_NODES];
    uint64_t rng[SIM_MAX_NODES];
    bool rng_set[SIM_MAX_NODES];
    uint64_t outcomes[MED_OUTCOMES];
    uint64_t frames, cca_queries, cca_energy;
} med = {
    .lock_snr = 0,
    .capture_db = 6,
};

/* ---- Preamble sniffing: settings and state (§10) */

#define SN_CURVE_MAX 64
// Separates the detection streams from the verdict streams
#define SN_RNG_K 0x736e696666000001ULL

enum sn_outcome { SN_DETECTED, SN_NONE, SN_TX_ABORT, SN_REPLACED, SN_STOPPED, SN_LEFT, SN_OUTCOMES };
static const char *const sn_outcome_names[SN_OUTCOMES] = {
    "detected", "none", "tx_abort", "replaced", "stopped", "left"
};

// A frame on the sniff channel that the sniffing node hears: a candidate
// for detection, and an interferer of the others
struct sn_heard {
    uint32_t id;
    int src;
    uint64_t start, end;        // on air
    uint64_t det_end;           // end of its detectable part
    uint64_t pre_end;           // end of its preamble
    uint64_t dwell;             // µs, at its data rate
    uint64_t next;              // end of its next dwell to evaluate, 0: none
    float dbm;
    double mw;
};

struct sn_sniff {
    bool active;
    uint32_t serial;            // run-wide, from 1: tells stale check events
    uint32_t id;                // the node's tag
    uint16_t chan;
    uint8_t flags, end_state;
    uint64_t t_req, t0, t1;     // request; window [t0, t1), t0 after the node's own TX and the settling time
    uint64_t t_on;              // receiver on: t_req, or the end of the node's own frame (half duplex)
    bool on_pending;            // the ledger's LISTEN is still to record, at t_on
    uint64_t queued;            // earliest check event queued for this sniff (MED_SNIFF_NONE: none)
    int n, cap;
    struct sn_heard *h;
};

static struct {
    bool frame_mode;            // --sniff-detect=frame: the whole frame is detectable
    unsigned int dwell_bits;
    bool has_threshold;
    float threshold;
    int curve_ber_bits;         // > 0: P = (1 - BER(SINR))^bits; 0: the table
    int curve_n;
    float curve_x[SN_CURVE_MAX], curve_p[SN_CURVE_MAX];
    unsigned int sfd_bits;
    uint32_t settle_us;         // --sniff-settle-us: receiver on, not yet detecting
    const char *log_path;
    FILE *log;
    uint32_t next_serial;
    struct sn_sniff *s[SIM_MAX_NODES];
    uint64_t rng[SIM_MAX_NODES];
    bool rng_set[SIM_MAX_NODES];
    uint64_t outcomes[SN_OUTCOMES], locks, draws;
} sn = {
    .dwell_bits = 16,
    .curve_ber_bits = 16,
    .sfd_bits = 16,
};

/* ---- options */

static float med_number(const char *opt, const char *arg, float min, float max)
{
    char *end;
    float v = strtof(arg, &end);

    FATAL_ON(*end || !(v >= min && v <= max), 1, "%s: expected a number from %g to %g: %s", opt, min, max, arg);
    med.given = med.tuning = true;
    return v;
}

void med_set_mode(const char *arg)
{
    if (!strcmp(arg, "legacy"))
        med.central = false;
    else if (!strcmp(arg, "central"))
        med.central = true;
    else
        FATAL(1, "--medium: expected legacy or central: %s", arg);
    med.given = med.mode_given = true;
}

void med_set_tx_ref(const char *arg)
{
    med.tx_ref = med_number("--medium-tx-ref", arg, -100, 100);
    med.has_tx_ref = true;
}

void med_set_lock_snr(const char *arg)
{
    med.lock_snr = med_number("--medium-lock-snr", arg, -100, 100);
}

void med_set_capture_db(const char *arg)
{
    med.capture_db = med_number("--medium-capture-db", arg, -100, 100);
}

void med_set_log(const char *path)
{
    med.log_path = path;
    med.given = med.tuning = true;
}

void med_set_sniff_detect(const char *arg)
{
    if (!strcmp(arg, "preamble"))
        sn.frame_mode = false;
    else if (!strcmp(arg, "frame"))
        sn.frame_mode = true;
    else
        FATAL(1, "--sniff-detect: expected preamble or frame: %s", arg);
    med.given = med.tuning = true;
}

void med_set_sniff_dwell(const char *arg)
{
    sn.dwell_bits = med_number("--sniff-dwell-bits", arg, 1, 100000);
    FATAL_ON(sn.dwell_bits != strtof(arg, NULL), 1, "--sniff-dwell-bits: expected a whole number: %s", arg);
}

void med_set_sniff_threshold(const char *arg)
{
    if (!strcmp(arg, "off")) {
        sn.has_threshold = false;
        med.given = med.tuning = true;
        return;
    }
    sn.threshold = med_number("--sniff-threshold", arg, -200, 100);
    sn.has_threshold = true;
}

// ber:N, or SINR:P,SINR:P,... (SINR in dB, ascending; P from 0 to 1)
void med_set_sniff_curve(const char *arg)
{
    char *copy, *tok, *save, *colon, *end;
    long bits;

    med.given = med.tuning = true;
    if (!strncmp(arg, "ber:", 4)) {
        bits = strtol(arg + 4, &end, 10);
        FATAL_ON(*end || bits < 1 || bits > 100000, 1, "--sniff-curve: ber:N with N from 1 to 100000: %s", arg);
        sn.curve_ber_bits = bits;
        return;
    }
    sn.curve_ber_bits = 0;
    sn.curve_n = 0;
    copy = strdup(arg);
    FATAL_ON(!copy, 1, "strdup: %m");
    for (tok = strtok_r(copy, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
        FATAL_ON(sn.curve_n >= SN_CURVE_MAX, 1, "--sniff-curve: at most %d points", SN_CURVE_MAX);
        colon = strchr(tok, ':');
        FATAL_ON(!colon, 1, "--sniff-curve: expected ber:N or SINR:P,...: %s", arg);
        *colon = '\0';
        sn.curve_x[sn.curve_n] = strtof(tok, &end);
        FATAL_ON(*end || !*tok || !isfinite(sn.curve_x[sn.curve_n]), 1, "--sniff-curve: bad SINR %s", tok);
        sn.curve_p[sn.curve_n] = strtof(colon + 1, &end);
        FATAL_ON(*end || !colon[1] || !(sn.curve_p[sn.curve_n] >= 0 && sn.curve_p[sn.curve_n] <= 1), 1,
                 "--sniff-curve: bad probability %s", colon + 1);
        FATAL_ON(sn.curve_n && !(sn.curve_x[sn.curve_n] > sn.curve_x[sn.curve_n - 1]), 1,
                 "--sniff-curve: the SINR values must ascend: %s", arg);
        sn.curve_n++;
    }
    free(copy);
    FATAL_ON(!sn.curve_n, 1, "--sniff-curve: no point: %s", arg);
}

void med_set_sniff_sfd(const char *arg)
{
    sn.sfd_bits = med_number("--sniff-sfd-bits", arg, 0, 256);
    FATAL_ON(sn.sfd_bits != strtof(arg, NULL), 1, "--sniff-sfd-bits: expected a whole number: %s", arg);
}

void med_set_sniff_settle(const char *arg)
{
    float v = med_number("--sniff-settle-us", arg, 0, 1000000);

    FATAL_ON(v != (uint32_t)v, 1, "--sniff-settle-us: expected a whole number: %s", arg);
    sn.settle_us = v;
}

void med_set_sniff_log(const char *path)
{
    sn.log_path = path;
    med.given = med.tuning = true;
}

bool med_options_given(void)
{
    return med.given;
}

bool med_tuning_given(void)
{
    return med.tuning;
}

// Virtual time: the central medium unless --medium=legacy (docs/MEDIUM.md §1)
void med_default_central(void)
{
    if (!med.mode_given)
        med.central = true;
}

bool med_central(void)
{
    return med.central;
}

/* ---- helpers */

static double dbm_to_mw(double dbm)
{
    return pow(10, dbm / 10);
}

static double mw_to_dbm(double mw)
{
    return 10 * log10(mw);
}

static uint64_t mix64(uint64_t z)
{
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

// The receiver's verdict stream: splitmix64 from
// mix64(seed + mix64(ID + 1) + K), uniform in [0, 1) as the radio's.
static double med_uniform(int node)
{
    if (!med.rng_set[node]) {
        med.rng[node] = mix64(med.seed + mix64((uint64_t)node + 1) + MED_RNG_K);
        med.rng_set[node] = true;
    }
    return (mix64(med.rng[node] += 0x9e3779b97f4a7c15ULL) >> 11) * 0x1.0p-53;
}

// The radio's hardware state and channel, as it wrote them in shared memory
static uint8_t med_state(int node)
{
    return med.cca[node * SIM_SHM_CCA_STRIDE + 3];
}

static uint16_t med_chan(int node)
{
    uint16_t chan;

    memcpy(&chan, med.cca + node * SIM_SHM_CCA_STRIDE + 1, 2);
    return chan;
}

// Received power: the link's value, the power at the sender's TX power, or at
// --medium-tx-ref. An edge without a value (0) stays a perfect link.
static float med_rx_dbm(int rx, int src, int8_t tx_dbm)
{
    float link = sim_link(med.links, rx, src)->rssi;

    if (link == 0 || !med.has_tx_ref)
        return link;
    return link + (tx_dbm - med.tx_ref);
}

static const struct med_rx *med_find_rx(const struct med_frame *f, int node)
{
    int lo = 0, hi = f->n_rx - 1, mid;

    if (!(f->rx_mask[node / 64] & (1ULL << (node % 64))))
        return NULL;
    while (lo <= hi) {
        mid = (lo + hi) / 2;
        if (f->rx[mid].node == node)
            return &f->rx[mid];
        if (f->rx[mid].node < node)
            lo = mid + 1;
        else
            hi = mid - 1;
    }
    return NULL;
}

// Frames that ended by now leave the air
static void med_prune(uint64_t now)
{
    struct med_frame **p = &med.air, *f;

    while ((f = *p)) {
        if (f->t_end <= now) {
            *p = f->next;
            free(f->rx);
            free(f->msg);
            free(f);
        } else {
            p = &f->next;
        }
    }
}

// Sum of the powers at node of the frames on air on chan, but skip
static double med_energy_mw(int node, uint16_t chan, const struct med_frame *skip, int *count)
{
    const struct med_rx *rx;
    const struct med_frame *g;
    double sum = 0;

    for (g = med.air; g; g = g->next) {
        if (g == skip || g->chan != chan || g->src == node)
            continue;
        rx = med_find_rx(g, node);
        if (!rx)
            continue;
        sum += rx->mw;
        if (count)
            (*count)++;
    }
    return sum;
}

static void med_log(uint64_t t_end, int src, int dst, uint32_t id, int outcome, float dbm, double sinr)
{
    med.outcomes[outcome]++;
    if (med.log)
        fprintf(med.log, "%llu %d %d %u %s %.2f %.2f\n", (unsigned long long)t_end, src, dst, id,
                outcome_names[outcome], dbm, sinr);
}

static void med_add_intf(struct med_lock *l, uint64_t start, uint64_t end, double mw)
{
    if (l->n_intf == l->cap_intf) {
        l->cap_intf = l->cap_intf ? l->cap_intf * 2 : 4;
        l->intf = realloc(l->intf, l->cap_intf * sizeof(*l->intf));
        FATAL_ON(!l->intf, 1, "realloc: %m");
    }
    l->intf[l->n_intf].start = start;
    l->intf[l->n_intf].end = end;
    l->intf[l->n_intf].mw = mw;
    l->n_intf++;
}

static int cmp_u64(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;

    return x < y ? -1 : x > y;
}

// The frame of l over [t_bits, upto) (upto at most t_end), cut into segments
// wherever an interferer starts or ends (§2). Returns ln(P_success) of that
// part, and the lowest segment SINR in *sinr_min. An empty range (a frame
// without airtime, or nothing received yet) is the instant t_bits, with all
// the bits. t_bits is the frame's start, except after an "xl" preamble or for
// a lock made by a sniff during the preamble (§10).
static double med_segments(const struct med_lock *l, uint64_t upto, double *sinr_min)
{
    uint64_t end = upto < l->t_end ? upto : l->t_end;
    uint64_t airtime = l->t_end - l->t_bits;
    uint64_t stack_b[16], *b = stack_b;
    double logp = 0, intf, sinr, ber, bits;
    int nb = 0, i, k;

    *sinr_min = INFINITY;
    if (2 * l->n_intf + 2 > (int)(sizeof(stack_b) / sizeof(*stack_b))) {
        b = malloc((2 * l->n_intf + 2) * sizeof(*b));
        FATAL_ON(!b, 1, "malloc: %m");
    }
    b[nb++] = l->t_bits;
    if (end > l->t_bits) {
        b[nb++] = end;
        for (i = 0; i < l->n_intf; i++) {
            if (l->intf[i].start > l->t_bits && l->intf[i].start < end)
                b[nb++] = l->intf[i].start;
            if (l->intf[i].end > l->t_bits && l->intf[i].end < end)
                b[nb++] = l->intf[i].end;
        }
        qsort(b, nb, sizeof(*b), cmp_u64);
    }
    for (k = 0; k < nb; k++) {
        if (k + 1 < nb && b[k + 1] == b[k])
            continue;
        if (k + 1 == nb && end > l->t_bits)
            break;              // the end of the last segment
        intf = 0;
        for (i = 0; i < l->n_intf; i++)
            if (l->intf[i].start <= b[k] && l->intf[i].end > b[k])
                intf += l->intf[i].mw;
        sinr = mw_to_dbm(l->mw / (med.noise_mw + intf));
        if (sinr < *sinr_min)
            *sinr_min = sinr;
        ber = 0.5 * exp(-0.5 * sinr);
        if (ber > 0.5)
            ber = 0.5;
        bits = end > l->t_bits ? (double)l->bits * (b[k + 1] - b[k]) / airtime : l->bits;
        logp += bits * log1p(-ber);
    }
    if (b != stack_b)
        free(b);
    return logp;
}

static void med_end_lock(struct med_lock *l, int outcome, uint64_t now)
{
    double sinr;

    med_segments(l, now, &sinr);
    med_log(l->t_end, l->src, l->node, l->frame_id, outcome, l->dbm, sinr);
    l->active = false;
    if (med.locks[l->node] == l)
        med.locks[l->node] = NULL;
}

/* ---- the run */

void med_start(uint64_t seed, const uint8_t *cca_shm, struct node_infos *links)
{
    med.seed = seed;
    med.cca = cca_shm;
    med.links = links;
    med.noise_mw = dbm_to_mw(MED_NOISE_DBM);
    if (med.log_path) {
        med.log = fopen(med.log_path, "w");
        FATAL_ON(!med.log, 1, "%s: %m", med.log_path);
    }
    if (sn.log_path) {
        sn.log = fopen(sn.log_path, "w");
        FATAL_ON(!sn.log, 1, "%s: %m", sn.log_path);
    }
    INFO("medium: central, lock threshold %.1f dB, capture margin %.1f dB%s", med.lock_snr, med.capture_db,
         med.has_tx_ref ? "" : ", link values are the received power");
    if (med.has_tx_ref)
        INFO("medium: link values are the received power at %.1f dBm TX power", med.tx_ref);
}

struct med_frame *med_frame_start(int src, const struct sim_frame_hdr *hdr, uint64_t now)
{
    return med_frame_start_lp(src, hdr, now, 0);
}

struct med_frame *med_frame_start_lp(int src, const struct sim_frame_hdr *hdr, uint64_t now, uint32_t preamble_us)
{
    struct med_frame *f;
    struct sim_frame_hdr *copy;
    uint64_t bytes = (uint64_t)hdr->shr_len + hdr->phr_len + hdr->len + hdr->fcs_len;
    uint64_t sfd_us;

    if (!hdr->rate_bps) {
        WARN("node %d: frame without a data rate: not on the medium", src);
        return NULL;
    }
    med_prune(now);
    f = calloc(1, sizeof(*f));
    FATAL_ON(!f, 1, "calloc: %m");
    f->id = ++med.next_id;
    f->src = src;
    f->chan = hdr->chan;
    f->tx_dbm = hdr->tx_dbm;
    f->t_start = now;
    f->t_bits = now + preamble_us;
    f->t_end = f->t_bits + bytes * 8 * 1000000 / hdr->rate_bps;
    f->shr_us = preamble_us + (uint64_t)hdr->shr_len * 8 * 1000000 / hdr->rate_bps;
    sfd_us = (uint64_t)sn.sfd_bits * 1000000 / hdr->rate_bps;
    f->pre_end = f->shr_us > sfd_us ? now + f->shr_us - sfd_us : now;
    f->bits = 8 * ((unsigned int)hdr->phr_len + hdr->len + hdr->fcs_len);
    f->rate_bps = hdr->rate_bps;
    // The message as a receiver locked by a sniff gets it (§10)
    f->len = sizeof(*hdr) + hdr->len;
    f->msg = malloc(f->len);
    FATAL_ON(!f->msg, 1, "malloc: %m");
    memcpy(f->msg, hdr, f->len);
    copy = f->msg;
    copy->flags = SIM_FRAME_MEDIUM | (preamble_us ? SIM_FRAME_LONG_PREAMBLE : 0);
    copy->frame_id = f->id;
    copy->src = src;
    copy->rx_dbm = 0;
    f->next = med.air;
    med.air = f;
    med.frames++;
    return f;
}

uint32_t med_frame_id(const struct med_frame *f)
{
    return f->id;
}

uint64_t med_frame_end(const struct med_frame *f)
{
    return f->t_end;
}

void med_add_rx(struct med_frame *f, int node)
{
    struct med_rx *rx;

    BUG_ON(f->n_rx && f->rx[f->n_rx - 1].node >= node);
    if (f->n_rx == f->cap_rx) {
        f->cap_rx = f->cap_rx ? f->cap_rx * 2 : 8;
        f->rx = realloc(f->rx, f->cap_rx * sizeof(*f->rx));
        FATAL_ON(!f->rx, 1, "realloc: %m");
    }
    rx = &f->rx[f->n_rx++];
    f->rx_mask[node / 64] |= 1ULL << (node % 64);
    rx->node = node;
    rx->dbm = med_rx_dbm(node, f->src, f->tx_dbm);
    rx->mw = dbm_to_mw(rx->dbm);
}

int med_num_rx(const struct med_frame *f)
{
    return f->n_rx;
}

int med_rx_node(const struct med_frame *f, int i)
{
    return f->rx[i].node;
}

// r locks onto f: the frames on air at r on f's channel are its interferers
static struct med_lock *med_lock(struct med_frame *f, const struct med_rx *rx)
{
    struct med_lock *l = calloc(1, sizeof(*l));
    const struct med_rx *irx;
    const struct med_frame *g;

    FATAL_ON(!l, 1, "calloc: %m");
    l->node = rx->node;
    l->src = f->src;
    l->frame_id = f->id;
    l->chan = f->chan;
    l->t_start = f->t_start;
    l->t_end = f->t_end;
    l->t_bits = f->t_bits;
    l->shr_us = f->shr_us;
    l->bits = f->bits;
    l->dbm = rx->dbm;
    l->mw = rx->mw;
    l->active = true;
    for (g = med.air; g; g = g->next) {
        if (g == f || g->chan != f->chan || g->src == rx->node)
            continue;
        irx = med_find_rx(g, rx->node);
        if (irx)
            med_add_intf(l, g->t_start, g->t_end, irx->mw);
    }
    med.locks[rx->node] = l;
    return l;
}

struct med_lock *med_decide(struct med_frame *f, int i, uint64_t now, float *rx_dbm)
{
    const struct med_rx *rx = &f->rx[i];
    struct med_lock *l = med.locks[rx->node];
    uint8_t state = med_state(rx->node);
    double snr;

    *rx_dbm = rx->dbm;
    if (l && l->t_end <= now) {
        // Its frame ended: the verdict is queued, the receiver listens again
        med.locks[rx->node] = NULL;
        l = NULL;
    } else if (l && l->delivered && state != SIM_RADIO_RECEIVE) {
        med_end_lock(l, MED_LEFT, now);
        l = NULL;
    }
    snr = rx->dbm - mw_to_dbm(med.noise_mw + med_energy_mw(rx->node, f->chan, f, NULL));
    if (l && l->chan == f->chan) {
        if (now - l->t_start < l->shr_us && rx->dbm >= l->dbm + med.capture_db) {
            med_end_lock(l, MED_CAPTURED, now);
            return med_lock(f, rx);
        }
        med_add_intf(l, f->t_start, f->t_end, rx->mw);
        med_log(f->t_end, f->src, rx->node, f->id, MED_BUSY, rx->dbm, snr);
        return NULL;
    }
    if (l || (state != SIM_RADIO_LISTEN && state != SIM_RADIO_RECEIVE) || med_chan(rx->node) != f->chan) {
        med_log(f->t_end, f->src, rx->node, f->id, MED_NOT_LISTENING, rx->dbm, snr);
        return NULL;
    }
    if (snr < med.lock_snr) {
        med_log(f->t_end, f->src, rx->node, f->id, MED_BELOW_LOCK, rx->dbm, snr);
        return NULL;
    }
    return med_lock(f, rx);
}

static void sn_end(int node, int outcome, uint64_t now, const struct sim_sniff_result *res);

void med_tx_start(int node, uint64_t now)
{
    struct med_lock *l = med.locks[node];

    if (sn.s[node] && sn.s[node]->active)
        sn_end(node, SN_TX_ABORT, now, NULL);

    if (!l)
        return;
    if (l->t_end <= now)
        med.locks[node] = NULL;
    else
        med_end_lock(l, MED_TX_ABORT, now);
}

void med_delivered(int node, uint32_t frame_id)
{
    if (med.locks[node] && med.locks[node]->frame_id == frame_id)
        med.locks[node]->delivered = true;
}

void med_node_stopped(int node, uint64_t now)
{
    if (sn.s[node] && sn.s[node]->active)
        sn_end(node, SN_STOPPED, now, NULL);
    if (med.locks[node])
        med_end_lock(med.locks[node], MED_LEFT, now);
}

void med_lock_free(struct med_lock *l)
{
    if (med.locks[l->node] == l)
        med.locks[l->node] = NULL;
    free(l->intf);
    free(l);
}

bool med_verdict(struct med_lock *l, uint64_t now, struct sim_verdict *v)
{
    double logp, p_ok, sinr;
    bool ok;
    int outcome;

    if (!l->active) {
        med_lock_free(l);
        return false;
    }
    if (l->delivered && med_state(l->node) != SIM_RADIO_RECEIVE) {
        med_end_lock(l, MED_LEFT, now);
        med_lock_free(l);
        return false;
    }
    logp = med_segments(l, l->t_end, &sinr);
    p_ok = exp(logp);
    // One draw, only when the outcome is not certain (§2)
    ok = p_ok >= 1 || (p_ok > 0 && !(-expm1(logp) > med_uniform(l->node)));
    outcome = ok ? MED_OK : l->n_intf ? MED_INTERFERENCE : MED_NOISE;
    med_log(l->t_end, l->src, l->node, l->frame_id, outcome, l->dbm, sinr);
    memset(v, 0, sizeof(*v));
    memcpy(v->magic, "xv", 2);
    v->ok = ok;
    v->cause = ok ? SIM_RX_OK : outcome == MED_NOISE ? SIM_RX_NOISE : SIM_RX_INTERFERENCE;
    v->frame_id = l->frame_id;
    v->rx_dbm = l->dbm;
    v->sinr_db = sinr;
    med_lock_free(l);
    return true;
}

float med_sense(int node, uint16_t chan, uint64_t now)
{
    int count = 0;
    double mw;

    med_prune(now);
    mw = med_energy_mw(node, chan, NULL, &count);
    med.cca_queries++;
    if (count)
        med.cca_energy++;
    return mw_to_dbm(med.noise_mw + mw);
}

/* ---- Preamble sniffing (§10) */

static double sn_uniform(int node)
{
    if (!sn.rng_set[node]) {
        sn.rng[node] = mix64(med.seed + mix64((uint64_t)node + 1) + SN_RNG_K);
        sn.rng_set[node] = true;
    }
    return (mix64(sn.rng[node] += 0x9e3779b97f4a7c15ULL) >> 11) * 0x1.0p-53;
}

// Detection probability of one dwell at this SINR (dB)
static double sn_curve(double sinr)
{
    double ber;
    int i;

    if (sn.curve_ber_bits) {
        ber = 0.5 * exp(-0.5 * sinr);
        if (ber > 0.5)
            ber = 0.5;
        return exp(sn.curve_ber_bits * log1p(-ber));
    }
    if (sinr <= sn.curve_x[0])
        return sn.curve_p[0];
    for (i = 1; i < sn.curve_n; i++)
        if (sinr < sn.curve_x[i])
            return sn.curve_p[i - 1] + (sn.curve_p[i] - sn.curve_p[i - 1]) *
                   (sinr - sn.curve_x[i - 1]) / (sn.curve_x[i] - sn.curve_x[i - 1]);
    return sn.curve_p[sn.curve_n - 1];
}

static void sn_log(const struct sn_sniff *x, int node, int outcome, uint64_t now, const struct sim_sniff_result *res)
{
    sn.outcomes[outcome]++;
    if (!sn.log)
        return;
    fprintf(sn.log, "%llu %d %u %u %llu %llu %s %llu %u %u %.2f %.2f %u\n", (unsigned long long)x->t_req, node,
            x->id, x->chan, (unsigned long long)x->t0, (unsigned long long)x->t1, sn_outcome_names[outcome],
            (unsigned long long)(res ? res->t_us : now), res ? res->frame_id : 0, res ? res->src : 0,
            res ? res->rssi_dbm : 0.0, res ? res->sinr_db : 0.0, res ? res->locked : 0);
}

static void sn_end(int node, int outcome, uint64_t now, const struct sim_sniff_result *res)
{
    struct sn_sniff *x = sn.s[node];

    sn_log(x, node, outcome, now, res);
    x->active = false;
    x->n = 0;
}

// h's first dwell in the window, or none
static void sn_first_dwell(const struct sn_sniff *x, struct sn_heard *h)
{
    uint64_t from = h->start > x->t0 ? h->start : x->t0;
    uint64_t limit = h->det_end < x->t1 ? h->det_end : x->t1;

    h->next = from + h->dwell <= limit ? from + h->dwell : 0;
}

static void sn_add(struct sn_sniff *x, const struct med_frame *f, const struct med_rx *rx)
{
    struct sn_heard *h;

    if (x->n == x->cap) {
        x->cap = x->cap ? x->cap * 2 : 8;
        x->h = realloc(x->h, x->cap * sizeof(*x->h));
        FATAL_ON(!x->h, 1, "realloc: %m");
    }
    h = &x->h[x->n++];
    h->id = f->id;
    h->src = f->src;
    h->start = f->t_start;
    h->end = f->t_end;
    h->pre_end = f->pre_end;
    h->det_end = sn.frame_mode ? f->t_end : f->pre_end;
    h->dwell = ((uint64_t)sn.dwell_bits * 1000000 + f->rate_bps - 1) / f->rate_bps;
    if (!h->dwell)
        h->dwell = 1;
    h->dbm = rx->dbm;
    h->mw = rx->mw;
    sn_first_dwell(x, h);
}

static uint64_t sn_next(const struct sn_sniff *x)
{
    uint64_t next = x->t1;
    int i;

    if (x->on_pending && x->t_on < next)
        next = x->t_on;
    for (i = 0; i < x->n; i++)
        if (x->h[i].next && x->h[i].next < next)
            next = x->h[i].next;
    return next;
}

uint32_t med_sniff_start(int node, const struct sim_sniff *req, uint64_t now, uint64_t *next)
{
    struct sn_sniff *x = sn.s[node];
    const struct med_frame *f;
    const struct med_rx *rx;

    if (!sn.next_serial)
        INFO("medium: sniffing: %s detection, dwell %u bits, threshold %s%.1f dBm, curve %s%d, SFD %u bits, "
             "settling %u us", sn.frame_mode ? "frame" : "preamble", sn.dwell_bits, sn.has_threshold ? "" : "off ",
             sn.has_threshold ? sn.threshold : 0.0, sn.curve_ber_bits ? "ber:" : "table of ",
             sn.curve_ber_bits ? sn.curve_ber_bits : sn.curve_n, sn.sfd_bits, sn.settle_us);
    if (!x) {
        x = sn.s[node] = calloc(1, sizeof(*x));
        FATAL_ON(!x, 1, "calloc: %m");
    }
    if (x->active)
        sn_end(node, SN_REPLACED, now, NULL);
    med_prune(now);
    x->active = true;
    x->serial = ++sn.next_serial;
    x->id = req->sniff_id;
    x->chan = req->channel;
    x->flags = req->flags;
    // Off, idle or listening; receiving and sending are not states to end in
    x->end_state = req->end_state <= SIM_RADIO_LISTEN ? req->end_state : SIM_RADIO_OFF;
    x->t_req = now;
    x->t_on = now;
    x->t1 = now + req->duration_us;
    x->n = 0;
    // Half duplex: deaf until its own frame has left the air
    for (f = med.air; f; f = f->next)
        if (f->src == node && f->t_end > x->t_on)
            x->t_on = f->t_end;
    if (x->t_on > x->t1)
        x->t_on = x->t1;
    // The conductor asks med_sniff_rx_on() at once; a window that its own
    // frame fills is never listened to
    x->on_pending = x->t_on == now || x->t_on < x->t1;
    // Then the receiver settles before it can detect
    x->t0 = x->t_on + sn.settle_us;
    if (x->t0 > x->t1)
        x->t0 = x->t1;
    for (f = med.air; f; f = f->next) {
        if (f->chan != x->chan || f->src == node)
            continue;
        rx = med_find_rx(f, node);
        if (rx)
            sn_add(x, f, rx);
    }
    *next = x->queued = sn_next(x);
    return x->serial;
}

bool med_sniff_rx_on(int node, uint32_t serial, uint64_t now, uint16_t *chan)
{
    struct sn_sniff *x = sn.s[node];

    if (!x || !x->active || x->serial != serial || !x->on_pending || now < x->t_on)
        return false;
    x->on_pending = false;
    *chan = x->chan;
    return true;
}

uint64_t med_sniff_frame(struct med_frame *f, int i, uint32_t *serial)
{
    const struct med_rx *rx = &f->rx[i];
    struct sn_sniff *x = sn.s[rx->node];
    struct sn_heard *h;

    if (!x || !x->active || f->chan != x->chan || f->t_start >= x->t1)
        return MED_SNIFF_NONE;
    sn_add(x, f, rx);
    h = &x->h[x->n - 1];
    *serial = x->serial;
    // One chain of check events per sniff: a new event only if it comes
    // before the earliest one queued (a later one finds this dwell itself)
    if (!h->next || h->next >= x->queued)
        return MED_SNIFF_NONE;
    x->queued = h->next;
    return h->next;
}

// Lowest SINR of h over [from, to), the other heard frames as interferers
static double sn_sinr_min(const struct sn_sniff *x, const struct sn_heard *h, uint64_t from, uint64_t to)
{
    double sinr, min = INFINITY, intf;
    uint64_t t;
    int i, j;

    // The SINR is lowest where the most interferers overlap: try each start
    // of an interferer inside the range, and the range's start
    for (i = -1; i < x->n; i++) {
        if (i >= 0 && (&x->h[i] == h || x->h[i].start <= from || x->h[i].start >= to))
            continue;
        t = i < 0 ? from : x->h[i].start;
        intf = 0;
        for (j = 0; j < x->n; j++)
            if (&x->h[j] != h && x->h[j].start <= t && x->h[j].end > t)
                intf += x->h[j].mw;
        sinr = mw_to_dbm(h->mw / (med.noise_mw + intf));
        if (sinr < min)
            min = sinr;
    }
    return min;
}

uint64_t med_lock_end(const struct med_lock *l)
{
    return l->t_end;
}

uint8_t med_sniff_end_state(int node, uint64_t now)
{
    const struct med_lock *l = med.locks[node];
    const struct med_frame *f;

    // A receiver locked onto a frame (at its start, during the window) stays
    // in RX: the radio reports its next state itself
    if (l && l->active && l->t_end > now)
        return SIM_RADIO_STATES;
    // Its own frame fills the window: the radio is still sending, and
    // reports its next state at that frame's end itself
    for (f = med.air; f; f = f->next)
        if (f->src == node && f->t_end > now)
            return SIM_RADIO_STATES;
    return sn.s[node] ? sn.s[node]->end_state : SIM_RADIO_OFF;
}

// After a detection of h at now: lock the receiver onto it if it can still
// be received (§10). Returns the lock, or NULL (*already: it holds h).
static struct med_lock *sn_lock(int node, const struct sn_heard *h, uint64_t now, bool *already)
{
    struct med_lock *l = med.locks[node];
    struct med_frame *f;
    const struct med_rx *rx;
    double snr;

    *already = false;
    if (l && l->t_end <= now) {
        med.locks[node] = NULL;
        l = NULL;
    }
    if (l) {
        *already = l->active && l->frame_id == h->id;
        return NULL;
    }
    if (now > h->pre_end)
        return NULL;
    med_prune(now);             // frames that ended are no longer energy on the channel
    for (f = med.air; f && f->id != h->id; f = f->next)
        ;
    if (!f)
        return NULL;
    rx = med_find_rx(f, node);
    if (!rx)
        return NULL;
    snr = rx->dbm - mw_to_dbm(med.noise_mw + med_energy_mw(node, f->chan, f, NULL));
    if (snr < med.lock_snr)
        return NULL;
    l = med_lock(f, rx);
    if (l->t_bits < now)
        l->t_bits = now;
    sn.locks++;
    return l;
}

bool med_sniff_check(int node, uint32_t serial, uint64_t now, struct sim_sniff_result *res,
                     struct med_lock **lock, const void **msg, int *len, float *rx_dbm, uint64_t *next)
{
    struct sn_sniff *x = sn.s[node];
    struct sn_heard *h, *best;
    const struct med_frame *f;
    const struct med_lock *l;
    double sinr, p;
    bool detected, already;
    int i;

    *lock = NULL;
    *next = MED_SNIFF_NONE;
    if (!x || !x->active || x->serial != serial)
        return false;
    if (now >= x->queued)
        x->queued = MED_SNIFF_NONE;   // this is the earliest event of the chain
    memset(res, 0, sizeof(*res));
    memcpy(res->magic, "xd", 2);
    res->sniff_id = x->id;
    // Each dwell that ended by now, in time order (then frame start and ID)
    for (;;) {
        best = NULL;
        for (i = 0; i < x->n; i++) {
            h = &x->h[i];
            if (!h->next || h->next > now)
                continue;
            if (!best || h->next < best->next || (h->next == best->next &&
                (h->start < best->start || (h->start == best->start && h->id < best->id))))
                best = h;
        }
        if (!best)
            break;
        h = best;
        sinr = sn_sinr_min(x, h, h->next - h->dwell, h->next);
        p = sn.has_threshold && h->dbm < sn.threshold ? 0 : sn_curve(sinr);
        // A receiver locked onto another frame (at its start, during the
        // window) is synchronised on it: it detects no other preamble
        l = med.locks[node];
        if (l && l->active && l->frame_id != h->id && l->t_start < h->next && l->t_end > h->next - h->dwell)
            p = 0;
        detected = p >= 1;
        if (p > 0 && p < 1) {
            sn.draws++;
            detected = sn_uniform(node) < p;
        }
        if (detected) {
            res->detected = 1;
            res->frame_id = h->id;
            res->src = h->src;
            res->t_us = h->next;
            res->rssi_dbm = h->dbm;
            res->sinr_db = sinr;
            if (x->flags & SIM_SNIFF_LOCK) {
                *lock = sn_lock(node, h, now, &already);
                res->locked = *lock ? SIM_SNIFF_LOCKED : already ? SIM_SNIFF_RECEIVING : 0;
                if (*lock) {
                    for (f = med.air; f->id != h->id; f = f->next)
                        ;
                    *msg = f->msg;
                    *len = f->len;
                    *rx_dbm = h->dbm;
                }
            } else if (l && l->active && l->frame_id == h->id && l->t_end > now) {
                // Without the flag too: the receiver already receives it
                res->locked = SIM_SNIFF_RECEIVING;
            }
            sn_end(node, SN_DETECTED, now, res);
            return true;
        }
        h->next += h->dwell;
        if (h->next > (h->det_end < x->t1 ? h->det_end : x->t1))
            h->next = 0;
    }
    if (now >= x->t1) {
        double mw = 0;

        for (i = 0; i < x->n; i++)
            if (x->h[i].start <= x->t1 && x->h[i].end > x->t1)
                mw += x->h[i].mw;
        res->t_us = x->t1;
        res->rssi_dbm = mw_to_dbm(med.noise_mw + mw);
        sn_end(node, SN_NONE, now, res);
        return true;
    }
    *next = sn_next(x);
    if (*next >= x->queued) {
        *next = MED_SNIFF_NONE;     // an event of the chain is already due by then
    } else {
        x->queued = *next;
    }
    return false;
}

int med_sniff_channel(int node)
{
    const struct sn_sniff *x = sn.s[node];

    return x && x->active ? x->chan : -1;
}

void med_sniff_cancel(int node, uint64_t now)
{
    if (sn.s[node] && sn.s[node]->active)
        sn_end(node, SN_LEFT, now, NULL);
}

void med_rx_leave(int node, uint64_t now)
{
    struct med_lock *l = med.locks[node];

    if (l && l->active && l->t_end > now)
        med_end_lock(l, MED_LEFT, now);
}

void med_finish(void)
{
    struct med_frame *f;
    int i;

    if (!med.central)
        return;
    INFO("medium: %llu frames, outcomes at receivers:", (unsigned long long)med.frames);
    for (i = 0; i < MED_OUTCOMES; i++)
        INFO("medium:   %-14s %llu", outcome_names[i], (unsigned long long)med.outcomes[i]);
    INFO("medium: %llu CCA queries, %llu with a frame on air", (unsigned long long)med.cca_queries,
         (unsigned long long)med.cca_energy);
    if (sn.next_serial) {
        INFO("medium: %u sniffs, outcomes:", sn.next_serial);
        for (i = 0; i < SN_OUTCOMES; i++)
            INFO("medium:   sniff %-8s %llu", sn_outcome_names[i], (unsigned long long)sn.outcomes[i]);
        INFO("medium: %llu locks made by a sniff, %llu detection draws", (unsigned long long)sn.locks,
             (unsigned long long)sn.draws);
    }
    if (med.log)
        fclose(med.log);
    med.log = NULL;
    if (sn.log)
        fclose(sn.log);
    sn.log = NULL;
    for (i = 0; i < SIM_MAX_NODES; i++) {
        if (sn.s[i])
            free(sn.s[i]->h);
        free(sn.s[i]);
        sn.s[i] = NULL;
    }
    while ((f = med.air)) {
        med.air = f->next;
        free(f->rx);
        free(f->msg);
        free(f);
    }
}
