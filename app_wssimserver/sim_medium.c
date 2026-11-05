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
    uint64_t shr_us;            // airtime of the SHR: the capture window
    unsigned int bits;          // counted bits: 8 · (PHR + PSDU + FCS)
    int8_t tx_dbm;
    int n_rx, cap_rx;
    struct med_rx *rx;          // ascending node IDs
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

// The frame of l over [t_start, upto) (upto at most t_end), cut into segments
// wherever an interferer starts or ends (§2). Returns ln(P_success) of that
// part, and the lowest segment SINR in *sinr_min. An empty range (a frame
// without airtime, or nothing received yet) is the instant t_start, with all
// the bits.
static double med_segments(const struct med_lock *l, uint64_t upto, double *sinr_min)
{
    uint64_t end = upto < l->t_end ? upto : l->t_end;
    uint64_t airtime = l->t_end - l->t_start;
    uint64_t stack_b[16], *b = stack_b;
    double logp = 0, intf, sinr, ber, bits;
    int nb = 0, i, k;

    *sinr_min = INFINITY;
    if (2 * l->n_intf + 2 > (int)(sizeof(stack_b) / sizeof(*stack_b))) {
        b = malloc((2 * l->n_intf + 2) * sizeof(*b));
        FATAL_ON(!b, 1, "malloc: %m");
    }
    b[nb++] = l->t_start;
    if (end > l->t_start) {
        b[nb++] = end;
        for (i = 0; i < l->n_intf; i++) {
            if (l->intf[i].start > l->t_start && l->intf[i].start < end)
                b[nb++] = l->intf[i].start;
            if (l->intf[i].end > l->t_start && l->intf[i].end < end)
                b[nb++] = l->intf[i].end;
        }
        qsort(b, nb, sizeof(*b), cmp_u64);
    }
    for (k = 0; k < nb; k++) {
        if (k + 1 < nb && b[k + 1] == b[k])
            continue;
        if (k + 1 == nb && end > l->t_start)
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
        bits = end > l->t_start ? (double)l->bits * (b[k + 1] - b[k]) / airtime : l->bits;
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
    INFO("medium: central, lock threshold %.1f dB, capture margin %.1f dB%s", med.lock_snr, med.capture_db,
         med.has_tx_ref ? "" : ", link values are the received power");
    if (med.has_tx_ref)
        INFO("medium: link values are the received power at %.1f dBm TX power", med.tx_ref);
}

struct med_frame *med_frame_start(int src, const struct sim_frame_hdr *hdr, uint64_t now)
{
    struct med_frame *f;
    uint64_t bytes = (uint64_t)hdr->shr_len + hdr->phr_len + hdr->len + hdr->fcs_len;

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
    f->t_end = now + bytes * 8 * 1000000 / hdr->rate_bps;
    f->shr_us = (uint64_t)hdr->shr_len * 8 * 1000000 / hdr->rate_bps;
    f->bits = 8 * ((unsigned int)hdr->phr_len + hdr->len + hdr->fcs_len);
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

void med_tx_start(int node, uint64_t now)
{
    struct med_lock *l = med.locks[node];

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
    if (med.log)
        fclose(med.log);
    med.log = NULL;
    while ((f = med.air)) {
        med.air = f->next;
        free(f->rx);
        free(f);
    }
}
