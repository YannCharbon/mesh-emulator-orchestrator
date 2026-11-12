/* SPDX-License-Identifier: Apache-2.0
 *
 * Link variation and the layered loss model: see sim_channel.h and
 * docs/MEDIUM.md §12.
 *
 * Random access in time. Each Gaussian process (shadowing, the I and Q parts
 * of the fading's scattered component) is defined on a grid of step
 * tau / CHAN_GRID: its value at grid point k is the stationary AR(1) sum
 *     x_k = sqrt(1 - a^2) / norm * sum_{m=0..M} a^m g_{k-m},  a = exp(-1 / CHAN_GRID)
 * of unit Gaussians g_j drawn by a counter-based generator from (seed,
 * process, link, j), truncated where a^M < 1e-3 and renormalised to unit
 * variance. The correlation of two grid points j apart is a^j, so exp(-dt /
 * tau). Between grid points the value is interpolated linearly and
 * rescaled to unit variance. Every value is computed from the generator
 * alone (the cache holds values, never state that depends on the query
 * history), so a link's value at t is the same whatever was asked before.
 *
 * The burst process is a renewal process: alternating good and bad
 * sojourns with exponential lengths, the n-th drawn from (seed, link, n),
 * the first state drawn from the stationary probabilities. A query walks
 * the sojourns from the cached one (or from 0 for an earlier time).
 */
#define _GNU_SOURCE
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "common/log.h"
#include "sim_channel.h"

// Separates the channel's streams from the medium's and the port's
#define CHAN_RNG_K 0x6368616e6e656c01ULL
#define CHAN_GRID 8             // grid points per time constant
#define CHAN_TRUNC 1e-3         // the AR(1) sum stops where a^M falls below this
#define CHAN_MAX_TERMS 128

enum { CP_SHADOW_LINK = 1, CP_SHADOW_NODE, CP_FADING, CP_BURST_LINK, CP_BURST_NODE };
enum { BURST_LINK, BURST_RX, BURST_NODE };
static const char *const burst_scopes[] = { "link", "receiver", "node" };

struct chan_ent {
    uint64_t key;               // 0: empty
    // Gaussian processes: the values at grid points k and k + 1 (I, Q)
    int64_t k;
    double x0[2], x1[2];
    // Burst process: sojourn n, [start, end), its state
    uint64_t n, start, end;
    bool bad;
};

static struct {
    bool given;
    bool layered;               // --medium-rx-model=layered
    bool curve_given;
    double x0, b;               // layered noise curve: 0.5 exp(-(SNR - x0) / b)
    bool combine_given;
    double combine_p;           // 0: the larger error rate (max); else the p-norm of the disturbances
    bool fading_given, fading;
    double k_factor, tc_s;
    bool shadow;
    double sigma_db, tau_s;
    double node_share;
    bool reciprocal;
    bool burst;
    int burst_scope;            // BURST_*: one process per link, per receiver, or per node
    double good_s, bad_s, burst_db;
    // --medium-var-dump
    int n_dumps;
    struct { int rx, tx; double t0, dt; long n; bool shuffle; } dumps[16];
    // run
    bool started;
    uint64_t seed_k;
    double a, w[CHAN_MAX_TERMS];
    int terms;
    struct chan_ent *tab;
    size_t cap, used;
    uint64_t samples, bad_samples, grid_points;
} ch = {
    .x0 = 9.0,
    .b = 0.9,
    .k_factor = 10,
    .tc_s = 0.4,
    .reciprocal = true,
};

const char *const chan_option_names[] = {
    "medium-rx-model", "medium-rx-curve", "medium-combine", "medium-fading", "medium-shadowing",
    "medium-node-share", "medium-reciprocal", "medium-burst", "medium-var-dump", NULL
};

/* ---- options */

static double chan_number(const char *opt, const char *arg, double min, double max)
{
    char *end;
    double v = strtod(arg, &end);

    FATAL_ON(end == arg || *end || !(v >= min && v <= max), 1, "--%s: expected a number from %g to %g: %s",
             opt, min, max, arg);
    return v;
}

// "A,B[,...]": n numbers separated by commas into v; returns how many
static int chan_list(const char *opt, const char *arg, char *words[], int max)
{
    static char buf[256];
    char *p, *save = NULL;
    int n = 0;

    FATAL_ON(strlen(arg) >= sizeof(buf), 1, "--%s: too long: %s", opt, arg);
    strcpy(buf, arg);
    for (p = strtok_r(buf, ",", &save); p && n < max; p = strtok_r(NULL, ",", &save))
        words[n++] = p;
    FATAL_ON(p, 1, "--%s: too many values: %s", opt, arg);
    return n;
}

bool chan_set_option(const char *name, const char *arg)
{
    char *w[8];
    int n;

    if (!strcmp(name, "medium-rx-model")) {
        if (!strcmp(arg, "empirical"))
            ch.layered = false;
        else if (!strcmp(arg, "layered"))
            ch.layered = true;
        else
            FATAL(1, "--medium-rx-model: expected empirical or layered: %s", arg);
    } else if (!strcmp(name, "medium-rx-curve")) {
        n = chan_list(name, arg, w, 2);
        FATAL_ON(n != 2, 1, "--medium-rx-curve: expected X0,B: %s", arg);
        ch.x0 = chan_number(name, w[0], -50, 100);
        ch.b = chan_number(name, w[1], 0.01, 100);
        ch.curve_given = true;
    } else if (!strcmp(name, "medium-combine")) {
        ch.combine_given = true;
        if (!strcmp(arg, "max"))
            ch.combine_p = 0;
        else if (!strcmp(arg, "sum"))
            ch.combine_p = 1;
        else
            ch.combine_p = chan_number(name, arg, 0.5, 50);
    } else if (!strcmp(name, "medium-fading")) {
        ch.fading_given = true;
        if (!strcmp(arg, "off")) {
            ch.fading = false;
        } else {
            n = chan_list(name, arg, w, 2);
            FATAL_ON(n != 2, 1, "--medium-fading: expected K,TC_S or off: %s", arg);
            ch.k_factor = chan_number(name, w[0], 0, 1000);
            ch.tc_s = chan_number(name, w[1], 1e-4, 1e7);
            ch.fading = true;
        }
    } else if (!strcmp(name, "medium-shadowing")) {
        if (!strcmp(arg, "off")) {
            ch.shadow = false;
        } else {
            n = chan_list(name, arg, w, 2);
            FATAL_ON(n != 2, 1, "--medium-shadowing: expected SIGMA_DB,TAU_S or off: %s", arg);
            ch.sigma_db = chan_number(name, w[0], 0, 30);
            ch.tau_s = chan_number(name, w[1], 1e-4, 1e7);
            ch.shadow = ch.sigma_db > 0;
        }
    } else if (!strcmp(name, "medium-node-share")) {
        ch.node_share = chan_number(name, arg, 0, 1);
    } else if (!strcmp(name, "medium-reciprocal")) {
        if (!strcmp(arg, "on"))
            ch.reciprocal = true;
        else if (!strcmp(arg, "off"))
            ch.reciprocal = false;
        else
            FATAL(1, "--medium-reciprocal: expected on or off: %s", arg);
    } else if (!strcmp(name, "medium-burst")) {
        if (!strcmp(arg, "off")) {
            ch.burst = false;
        } else {
            n = chan_list(name, arg, w, 4);
            FATAL_ON(n != 3 && n != 4, 1, "--medium-burst: expected GOOD_S,BAD_S,DB[,link|rx|node] or off: %s", arg);
            ch.good_s = chan_number(name, w[0], 1e-4, 1e7);
            ch.bad_s = chan_number(name, w[1], 1e-4, 1e7);
            ch.burst_db = chan_number(name, w[2], 0, 200);
            ch.burst_scope = BURST_LINK;
            if (n == 4) {
                if (!strcmp(w[3], "rx"))
                    ch.burst_scope = BURST_RX;
                else if (!strcmp(w[3], "node"))
                    ch.burst_scope = BURST_NODE;
                else
                    FATAL_ON(strcmp(w[3], "link"), 1, "--medium-burst: the scope is link, rx or node: %s", arg);
            }
            ch.burst = true;
        }
    } else if (!strcmp(name, "medium-var-dump")) {
        FATAL_ON(ch.n_dumps == 16, 1, "--medium-var-dump: at most 16");
        n = chan_list(name, arg, w, 6);
        FATAL_ON(n != 5 && n != 6, 1, "--medium-var-dump: expected RX,TX,FROM_S,STEP_S,COUNT[,shuffle]: %s", arg);
        ch.dumps[ch.n_dumps].rx = chan_number(name, w[0], 0, 4095);
        ch.dumps[ch.n_dumps].tx = chan_number(name, w[1], 0, 4095);
        ch.dumps[ch.n_dumps].t0 = chan_number(name, w[2], 0, 1e9);
        ch.dumps[ch.n_dumps].dt = chan_number(name, w[3], 1e-6, 1e9);
        ch.dumps[ch.n_dumps].n = chan_number(name, w[4], 1, 1e8);
        ch.dumps[ch.n_dumps].shuffle = n == 6;
        FATAL_ON(n == 6 && strcmp(w[5], "shuffle"), 1, "--medium-var-dump: the 6th value is shuffle: %s", arg);
        ch.n_dumps++;
    } else {
        return false;
    }
    ch.given = true;
    return true;
}

bool chan_options_given(void)
{
    return ch.given;
}

void chan_help(FILE *stream)
{
    fprintf(stream, "  Link variation and the receiver curve (central medium, docs/MEDIUM.md §12; off by default):\n");
    fprintf(stream, "  --medium-rx-model=empirical|layered  noise curve: today's empirical one (the default) or\n");
    fprintf(stream, "                            a steep receiver curve meant for fading (layered: fading 10,0.4)\n");
    fprintf(stream, "  --medium-rx-curve=X0,B    layered curve: BER 0.5 exp(-(SNR-X0)/B) (9.0,0.9)\n");
    fprintf(stream, "  --medium-combine=max|sum|P  noise and interference: the larger error rate (max, the\n");
    fprintf(stream, "                            empirical default) or the P-norm of their equivalent powers (sum = 1,\n");
    fprintf(stream, "                            the layered default)\n");
    fprintf(stream, "  --medium-fading=K,TC_S|off  Rician fading, K factor (linear), coherence time in s\n");
    fprintf(stream, "  --medium-shadowing=SIGMA_DB,TAU_S|off  slow shadowing (Gauss-Markov) (off)\n");
    fprintf(stream, "  --medium-node-share=F     share of the shadowing variance common to a node's links (0)\n");
    fprintf(stream, "  --medium-reciprocal=on|off  the same variation in both directions of a link (on)\n");
    fprintf(stream, "  --medium-burst=GOOD_S,BAD_S,DB[,link|rx|node]|off  loss bursts: mean good and bad\n");
    fprintf(stream, "                            durations, attenuation in the bad state, per link, receiver or node (off)\n");
    fprintf(stream, "  --medium-var-dump=RX,TX,FROM_S,STEP_S,COUNT[,shuffle]  print a link's variation and exit\n");
}

/* ---- generator */

static uint64_t mix64(uint64_t z)
{
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

// Uniform in (0, 1] and [0, 1) from (key, j)
static void chan_uniforms(uint64_t key, uint64_t j, double *u1, double *u2)
{
    uint64_t h = mix64(ch.seed_k + mix64(key));
    uint64_t r = mix64(h + j * 0x9e3779b97f4a7c15ULL);
    uint64_t s = mix64(r ^ 0xd1b54a32d192ed03ULL);

    *u1 = ((r >> 11) + 1) * 0x1.0p-53;
    *u2 = (s >> 11) * 0x1.0p-53;
}

// Two independent unit Gaussians from (key, j): Box-Muller
static void chan_gauss2(uint64_t key, int64_t j, double g[2])
{
    double u1, u2, r;

    chan_uniforms(key, (uint64_t)j, &u1, &u2);
    r = sqrt(-2 * log(u1));
    g[0] = r * cos(2 * M_PI * u2);
    g[1] = r * sin(2 * M_PI * u2);
}

static uint64_t chan_key(int proc, int a, int b)
{
    return (uint64_t)proc << 32 | (uint64_t)a << 16 | (uint64_t)b;
}

static struct chan_ent *chan_find(uint64_t key)
{
    size_t i, mask;
    struct chan_ent *old;
    size_t old_cap;

    if (2 * (ch.used + 1) > ch.cap) {
        old = ch.tab;
        old_cap = ch.cap;
        ch.cap = ch.cap ? ch.cap * 2 : 1024;
        ch.tab = calloc(ch.cap, sizeof(*ch.tab));
        FATAL_ON(!ch.tab, 1, "calloc: %m");
        ch.used = 0;
        for (i = 0; i < old_cap; i++)
            if (old[i].key)
                *chan_find(old[i].key) = old[i];
        free(old);
    }
    mask = ch.cap - 1;
    for (i = mix64(key) & mask; ch.tab[i].key; i = (i + 1) & mask)
        if (ch.tab[i].key == key)
            return &ch.tab[i];
    ch.tab[i].key = key;
    ch.tab[i].k = INT64_MIN;
    ch.used++;
    return &ch.tab[i];
}

// x_k of the process (I and Q)
static void chan_grid_value(uint64_t key, int64_t k, double x[2])
{
    double g[2];
    int m;

    x[0] = x[1] = 0;
    for (m = 0; m < ch.terms; m++) {
        chan_gauss2(key, k - m, g);
        x[0] += ch.w[m] * g[0];
        x[1] += ch.w[m] * g[1];
    }
    ch.grid_points++;
}

// The process of key, time constant tau_us, at t: two unit Gaussians
static void chan_process(uint64_t key, double tau_us, uint64_t t, double v[2])
{
    struct chan_ent *e = chan_find(key);
    double s = t / (tau_us / CHAN_GRID);
    int64_t k = (int64_t)floor(s);
    double f = s - k, w0 = 1 - f, w1 = f, norm;

    if (e->k != k) {
        if (e->k != INT64_MIN && e->k + 1 == k) {
            memcpy(e->x0, e->x1, sizeof(e->x0));
        } else {
            chan_grid_value(key, k, e->x0);
        }
        chan_grid_value(key, k + 1, e->x1);
        e->k = k;
    }
    norm = sqrt(w0 * w0 + w1 * w1 + 2 * w0 * w1 * ch.a);
    v[0] = (w0 * e->x0[0] + w1 * e->x1[0]) / norm;
    v[1] = (w0 * e->x0[1] + w1 * e->x1[1]) / norm;
}

// The burst state of key at t
static bool chan_burst_bad(uint64_t key, uint64_t t)
{
    struct chan_ent *e = chan_find(key);
    double u1, u2, mean;

    if (!e->end || t < e->start) {
        // From the start: the first state from the stationary probabilities
        chan_uniforms(key, 0, &u1, &u2);
        e->n = 0;
        e->start = 0;
        e->bad = u2 < ch.bad_s / (ch.good_s + ch.bad_s);
        mean = (e->bad ? ch.bad_s : ch.good_s) * 1e6;
        e->end = (uint64_t)ceil(-mean * log(u1));
        if (e->end < 1)
            e->end = 1;
    }
    while (t >= e->end) {
        e->n++;
        e->bad = !e->bad;
        e->start = e->end;
        chan_uniforms(key, e->n, &u1, &u2);
        mean = (e->bad ? ch.bad_s : ch.good_s) * 1e6;
        e->end = e->start + (uint64_t)ceil(-mean * log(u1));
        if (e->end == e->start)
            e->end++;
    }
    return e->bad;
}

/* ---- the run */

bool chan_varies(void)
{
    return ch.fading || ch.shadow || ch.burst;
}

bool chan_layered(void)
{
    return ch.layered;
}

bool chan_ber_custom(void)
{
    return ch.layered || (ch.combine_given && ch.combine_p != 0);
}

static void chan_resolve(void)
{
    // The layered curve is calibrated against the empirical one with Rician
    // fading K = 10 (docs/MEDIUM.md §12.4): on by default with it
    if (ch.layered && !ch.fading_given)
        ch.fading = true;
    if (!ch.combine_given)
        ch.combine_p = ch.layered ? 1 : 0;
}

void chan_start(uint64_t seed)
{
    char comb[32];
    double c, sum = 0;
    int m;

    chan_resolve();
    ch.seed_k = mix64(seed + CHAN_RNG_K);
    ch.a = exp(-1.0 / CHAN_GRID);
    ch.terms = (int)ceil(log(CHAN_TRUNC) / log(ch.a)) + 1;
    BUG_ON(ch.terms > CHAN_MAX_TERMS);
    c = sqrt(1 - ch.a * ch.a);
    for (m = 0; m < ch.terms; m++) {
        ch.w[m] = c * pow(ch.a, m);
        sum += ch.w[m] * ch.w[m];
    }
    for (m = 0; m < ch.terms; m++)
        ch.w[m] /= sqrt(sum);
    ch.started = true;
    if (!ch.given)
        return;
    if (ch.combine_p == 0)
        snprintf(comb, sizeof(comb), "the larger error rate");
    else
        snprintf(comb, sizeof(comb), "p-norm %g", ch.combine_p);
    if (ch.layered)
        INFO("medium: receiver curve layered (BER 0.5 exp(-(SNR - %.2f) / %.2f)), noise and interference: %s",
             ch.x0, ch.b, comb);
    else
        INFO("medium: receiver curve empirical, noise and interference: %s", comb);
    if (ch.fading)
        INFO("medium: fading Rician K %g, coherence time %g s", ch.k_factor, ch.tc_s);
    if (ch.shadow)
        INFO("medium: shadowing sigma %g dB, decorrelation time %g s, node share %g", ch.sigma_db, ch.tau_s,
             ch.node_share);
    if (ch.burst)
        INFO("medium: bursts per %s: good %g s, bad %g s, %g dB", burst_scopes[ch.burst_scope], ch.good_s,
             ch.bad_s, ch.burst_db);
    if (chan_varies())
        INFO("medium: links vary%s", ch.reciprocal ? ", the same in both directions" : ", each direction apart");
}

void chan_finish(void)
{
    if (!chan_varies())
        return;
    INFO("medium: link variation: %llu samples, %llu in a bad state, %zu processes, %llu grid points",
         (unsigned long long)ch.samples, (unsigned long long)ch.bad_samples, ch.used,
         (unsigned long long)ch.grid_points);
    free(ch.tab);
    ch.tab = NULL;
    ch.cap = ch.used = 0;
}

void chan_apply(int rx, int tx, uint64_t t, bool perfect, float *dbm, struct chan_sample *s)
{
    int a = rx, b = tx;
    double v[2], u[2], los, sc, re, im, gain, sh;

    memset(s, 0, sizeof(*s));
    s->mean_dbm = *dbm;
    if (perfect || !chan_varies())
        return;
    if (ch.reciprocal && a > b) {
        a = tx;
        b = rx;
    }
    ch.samples++;
    if (ch.shadow) {
        chan_process(chan_key(CP_SHADOW_LINK, a, b), ch.tau_s * 1e6, t, v);
        sh = sqrt(1 - ch.node_share) * v[0];
        if (ch.node_share > 0) {
            chan_process(chan_key(CP_SHADOW_NODE, rx, 0), ch.tau_s * 1e6, t, v);
            chan_process(chan_key(CP_SHADOW_NODE, tx, 0), ch.tau_s * 1e6, t, u);
            sh += sqrt(ch.node_share / 2) * (v[0] + u[0]);
        }
        s->shadow_db = ch.sigma_db * sh;
    }
    if (ch.fading) {
        chan_process(chan_key(CP_FADING, a, b), ch.tc_s * 1e6, t, v);
        los = sqrt(ch.k_factor / (ch.k_factor + 1));
        sc = sqrt(1 / (2 * (ch.k_factor + 1)));
        re = los + sc * v[0];
        im = sc * v[1];
        gain = re * re + im * im;
        if (gain < 1e-15)
            gain = 1e-15;
        s->fading_db = 10 * log10(gain);
    }
    if (ch.burst) {
        // Per receiver: the receiver's own process; per node: either end's
        // process (a node in its bad state attenuates all its links, both
        // ways); the node processes are the same in both scopes
        if (ch.burst_scope == BURST_RX)
            s->bad = chan_burst_bad(chan_key(CP_BURST_NODE, rx, 0), t);
        else if (ch.burst_scope == BURST_NODE)
            s->bad = chan_burst_bad(chan_key(CP_BURST_NODE, rx, 0), t) | chan_burst_bad(chan_key(CP_BURST_NODE, tx, 0), t);
        else
            s->bad = chan_burst_bad(chan_key(CP_BURST_LINK, a, b), t);
        if (s->bad) {
            s->burst_db = -ch.burst_db;
            ch.bad_samples++;
        }
    }
    *dbm = s->mean_dbm + s->shadow_db + s->fading_db + s->burst_db;
}

int chan_log_columns(char *buf, size_t size, const struct chan_sample *s)
{
    if (!chan_varies()) {
        buf[0] = '\0';
        return 0;
    }
    return snprintf(buf, size, " %.2f %.2f %.2f %d", s->mean_dbm, s->shadow_db, s->fading_db, s->bad);
}

/* ---- the receiver's decision */

static double chan_curve(double x_db)
{
    double x0 = ch.layered ? ch.x0 : 0, b = ch.layered ? ch.b : 2;
    double ber = 0.5 * exp(-(x_db - x0) / b);

    return ber > 0.5 ? 0.5 : ber;
}

double chan_noise_ber(double snr_db)
{
    return chan_curve(snr_db);
}

double chan_ber(double p_mw, double n_mw, double i_mw, bool int_noise, double x0i, double bi)
{
    double x0 = ch.layered ? ch.x0 : 0, b = ch.layered ? ch.b : 2;
    double sir, snr_eq, i_eq, m, d;

    if (int_noise)
        return chan_curve(10 * log10(p_mw / (n_mw + i_mw)));
    if (i_mw <= 0)
        return chan_curve(10 * log10(p_mw / n_mw));
    // The interference as the noise power that gives the same error rate on
    // the noise curve: where the noise curve equals the interference curve
    sir = 10 * log10(p_mw / i_mw);
    snr_eq = x0 + (sir - x0i) * b / bi;
    i_eq = p_mw / pow(10, snr_eq / 10);
    m = n_mw > i_eq ? n_mw : i_eq;
    if (ch.combine_p == 0)
        d = m;
    else
        d = m * pow(pow(n_mw / m, ch.combine_p) + pow(i_eq / m, ch.combine_p), 1 / ch.combine_p);
    return chan_curve(10 * log10(p_mw / d));
}

/* ---- --medium-var-dump */

void chan_dump_if_asked(void)
{
    const char *seed_env = getenv("WSSIM_SEED");
    uint64_t seed = seed_env && *seed_env ? strtoull(seed_env, NULL, 0) : 1;
    struct chan_sample *out;
    uint64_t *order, j, r, tmp;
    float dbm;
    int d;

    if (!ch.n_dumps)
        return;
    chan_start(seed);
    for (d = 0; d < ch.n_dumps; d++) {
        long n = ch.dumps[d].n;

        out = calloc(n, sizeof(*out));
        order = calloc(n, sizeof(*order));
        FATAL_ON(!out || !order, 1, "calloc: %m");
        for (j = 0; j < (uint64_t)n; j++)
            order[j] = j;
        if (ch.dumps[d].shuffle) {
            // Fisher-Yates from a stream of its own: queries in another order
            for (j = n - 1; j > 0; j--) {
                r = mix64(seed + 0x7368756666ULL + j) % (j + 1);
                tmp = order[j];
                order[j] = order[r];
                order[r] = tmp;
            }
        }
        for (j = 0; j < (uint64_t)n; j++) {
            uint64_t i = order[j];

            dbm = 0;
            chan_apply(ch.dumps[d].rx, ch.dumps[d].tx, (uint64_t)llround((ch.dumps[d].t0 + i * ch.dumps[d].dt) * 1e6),
                       false, &dbm, &out[i]);
        }
        for (j = 0; j < (uint64_t)n; j++)
            printf("%d %d %llu %.6f %.6f %d\n", ch.dumps[d].rx, ch.dumps[d].tx,
                   (unsigned long long)llround((ch.dumps[d].t0 + j * ch.dumps[d].dt) * 1e6), out[j].shadow_db,
                   out[j].fading_db, out[j].bad);
        free(out);
        free(order);
    }
    exit(0);
}
