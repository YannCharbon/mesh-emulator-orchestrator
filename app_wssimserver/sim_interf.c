/* SPDX-License-Identifier: Apache-2.0
 *
 * External interferers of the central medium: see sim_interf.h and
 * docs/MEDIUM.md §13.
 *
 * Units: time in µs, powers in dBm at the receiver's antenna, summed in mW.
 *
 * An interferer (struct isrc) is a source that is on during intervals of
 * virtual time. The intervals are generated in time order, on demand, from
 * the source's own random stream: whatever order the medium asks in, the
 * n-th interval of a source is always the same for a given seed. Intervals
 * that ended long ago are freed (INTF_LOOKBACK_US).
 */
#define _GNU_SOURCE
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <setjmp.h>
#include <unistd.h>
#include "common/log.h"
#include "sim_interf.h"
#include "sim_medium.h"
#include "sim_proto.h"

// Separates the interferers' streams from the verdict, sniff and port streams
#define INTF_RNG_K 0x696e746572660001ULL
// Intervals that ended this long before the latest time seen are freed. A
// query starts at the latest time, at the start of a frame being locked onto
// (a sniff can lock onto a frame whose preamble started earlier), or at a
// sniff's next step: only a frame on air for more than this (a long "xl"
// preamble) would miss intervals.
#define INTF_LOOKBACK_US 60000000ULL
#define INTF_MAX 64
#define INTF_CHANS_MAX 512
#define INTF_ADJ_MAX 16
#define INTF_NODES_MAX 1024     // at= entries per interferer
#define INTF_CF_MAX 16          // the longest adjacent-channel table (MED_ADJ_MAX, INTF_ADJ_MAX)
#define INTF_CACHE 256

enum { PAT_CONST, PAT_PERIODIC, PAT_GE, PAT_BURST };
static const char *const pat_names[] = { "const", "periodic", "ge", "burst" };

struct ivl {
    uint64_t s, e;
    uint16_t chan;              // hopping: the channel of this interval
};

struct isrc {
    char name[32];
    int index;
    uint16_t chans[INTF_CHANS_MAX];
    int n_chans;
    bool hop;
    bool noise;                 // kind=noise (else fsk)
    uint32_t rate_bps;          // kind=fsk
    uint32_t pre_us;            // kind=fsk: preamble at the start of each burst
    bool pre_given;
    int n_adj;                  // adj= given: coupling to the channels around
    float adj[INTF_ADJ_MAX];
    bool adj_on[INTF_ADJ_MAX];
    int pattern;
    uint64_t a, b, c;           // periodic: period, on, phase; ge: mean on, mean off; burst: on, mean off
    bool phase_given;
    bool has_level;
    float level;
    int n_at;
    struct { int from, to; float dbm; } at[INTF_NODES_MAX];
    bool has_pos;
    double pos[3];
    float eirp;
    float *lvl;                 // per node, dBm; NAN: does not reach it
    double *lvl_mw;
    // The schedule
    uint64_t rng;
    uint64_t gen_t;             // every interval that starts before gen_t is known
    bool gen_on, gen_init;
    uint64_t period_k;
    struct ivl *iv;
    int head, n, cap;
    uint64_t n_gen, on_gen;     // generated so far: intervals, on-time
    uint64_t from, until;       // active window (from=, until=; a scenario's add and remove)
    double cf[INTF_CF_MAX + 1]; // coupling 0, 1, ... channels away
    int n_cf;                   // cf[d] = 0 from d = n_cf
    int cache_chan[INTF_CACHE]; // wideband: the summed coupling into a channel
    double cache_sum[INTF_CACHE];
};

static struct {
    bool given;
    struct isrc *src[INTF_MAX];
    int n;
    bool dynamic, started;
    const char *rt_err;             // runtime parse: the first error, instead of FATAL
    const char *log_path;
    FILE *log;
    intf_pos_fn pos_fn;
    uint64_t seed;
    uint64_t now;               // latest time seen
} intf;

const char *const intf_option_names[] = {
    "interferer", "interferer-file", "interferer-log",
    "medium-adjacent-table", "medium-capture-model", "medium-capture-smooth", "medium-int-curve-presync",
    "sniff-adjacent", "sniff-energy", "sniff-energy-us", "sniff-false",
    NULL
};

void intf_usage(FILE *stream)
{
    fprintf(stream, "  Interference and spectrum (central medium, docs/MEDIUM.md §13):\n");
    fprintf(stream, "  --interferer=SPEC         an external interferer, KEY=VALUE,... (repeatable): chan=LIST\n");
    fprintf(stream, "                            (3, 0-34, 3/5/9), level=DBM, at=IDS:DBM (repeatable),\n");
    fprintf(stream, "                            pattern=const|periodic:PERIOD:ON[:PHASE]|ge:MEAN_ON:MEAN_OFF|\n");
    fprintf(stream, "                            burst:ON:MEAN_OFF (times in us, or with ms or s), hop=on|off,\n");
    fprintf(stream, "                            kind=noise|fsk, rate=BPS, pre=US, adj=DB/DB/..., pos=X/Y[/Z],\n");
    fprintf(stream, "                            eirp=DBM, name=NAME\n");
    fprintf(stream, "  --interferer-file=FILE    one SPEC per line (# comments)\n");
    fprintf(stream, "  --interferer-log=FILE     one line per on-interval (start_us end_us name chan)\n");
    fprintf(stream, "  --medium-adjacent-table=[MODE:]DB/DB/...  attenuation of a frame 1, 2, ... channels away\n");
    fprintf(stream, "                            (off for none; fg25: 10, nothing further away)\n");
    fprintf(stream, "  --medium-capture-model=[MODE:]step|smooth  capture and break as steps at their margins\n");
    fprintf(stream, "                            (step) or probabilities fitted to the kits (smooth)\n");
    fprintf(stream, "  --medium-capture-smooth=[MODE:]KEY=V,...  smooth model: m50, s, brk_s, early, slope, w,\n");
    fprintf(stream, "                            rise, fall\n");
    fprintf(stream, "  --medium-int-curve-presync=[MODE:]X0,B|same  interference curve of the frames already on\n");
    fprintf(stream, "                            air when the received frame's SHR ends (smooth: fitted)\n");
    fprintf(stream, "  --sniff-adjacent=off|on|detect  frames on the next channels take part in sniffing as\n");
    fprintf(stream, "                            interference and energy (on), also as detectable (detect)\n");
    fprintf(stream, "  --sniff-energy=DBM|off    also detect when the summed energy stays at or above DBM\n");
    fprintf(stream, "  --sniff-energy-us=N       averaging time of the energy detection (107)\n");
    fprintf(stream, "  --sniff-false=P|RATE/s    false detections on noise: probability per sniff window, or\n");
    fprintf(stream, "                            a rate per second of listening (0)\n");
}

bool intf_options_given(void)
{
    return intf.given;
}

void intf_set_position_model(intf_pos_fn fn)
{
    intf.pos_fn = fn;
}

/* ---- parsing */

// At run time (a scenario's "intf add"), a bad SPEC is an answer, not the
// end of the server: the parser's errors jump back to intf_add_runtime()
static jmp_buf *intf_rt_jmp;
static char intf_rt_msg[300];
static struct isrc *intf_parsing;
static char *intf_parsing_copy;
#define IFATAL(CODE, ...) do {                                                   \
        if (intf_rt_jmp) {                                                       \
            snprintf(intf_rt_msg, sizeof(intf_rt_msg), "" __VA_ARGS__);          \
            longjmp(*intf_rt_jmp, 1);                                            \
        }                                                                        \
        FATAL(CODE, __VA_ARGS__);                                                \
    } while (0)
#define IFATAL_ON(COND, CODE, ...) do { if (COND) IFATAL(CODE, __VA_ARGS__); } while (0)


// A duration: a number of µs, or with a unit (us, ms, s)
static uint64_t intf_time(const char *spec, const char *v)
{
    char *end;
    double d = strtod(v, &end), mult = 1;

    if (!strcmp(end, "ms"))
        mult = 1e3;
    else if (!strcmp(end, "s"))
        mult = 1e6;
    else
        IFATAL_ON(*end && strcmp(end, "us"), 1, "--interferer: bad time \"%s\" in %s", v, spec);
    IFATAL_ON(end == v || !(d >= 0) || d * mult > 1e15, 1, "--interferer: bad time \"%s\" in %s", v, spec);
    return llround(d * mult);
}

static float intf_float(const char *spec, const char *key, const char *v, float min, float max)
{
    char *end;
    float f = strtof(v, &end);

    IFATAL_ON(end == v || *end || !(f >= min && f <= max), 1, "--interferer: %s: expected a number from %g to %g: %s",
             key, min, max, spec);
    return f;
}

static long intf_long(const char *spec, const char *key, const char *v, long min, long max)
{
    char *end;
    long n = strtol(v, &end, 10);

    IFATAL_ON(end == v || *end || n < min || n > max, 1, "--interferer: %s: expected a whole number from %ld to %ld: %s",
             key, min, max, spec);
    return n;
}

static void intf_parse_chans(struct isrc *s, const char *spec, char *v)
{
    char *tok, *save, *dash;
    long a, b, c;

    for (tok = strtok_r(v, "/", &save); tok; tok = strtok_r(NULL, "/", &save)) {
        dash = strchr(tok, '-');
        if (dash)
            *dash = '\0';
        a = intf_long(spec, "chan", tok, 0, 65535);
        b = dash ? intf_long(spec, "chan", dash + 1, a, 65535) : a;
        for (c = a; c <= b; c++) {
            IFATAL_ON(s->n_chans >= INTF_CHANS_MAX, 1, "--interferer: at most %d channels: %s", INTF_CHANS_MAX, spec);
            for (int k = 0; k < s->n_chans; k++)
                IFATAL_ON(s->chans[k] == c, 1, "--interferer: channel %ld given twice: %s", c, spec);
            s->chans[s->n_chans++] = c;
        }
    }
}

static void intf_parse_pattern(struct isrc *s, const char *spec, char *v)
{
    char *f[4], *save;
    int n = 0;

    for (char *tok = strtok_r(v, ":", &save); tok; tok = strtok_r(NULL, ":", &save)) {
        IFATAL_ON(n >= 4, 1, "--interferer: pattern: too many fields: %s", spec);
        f[n++] = tok;
    }
    IFATAL_ON(!n, 1, "--interferer: empty pattern: %s", spec);
    if (!strcmp(f[0], "const") && n == 1) {
        s->pattern = PAT_CONST;
    } else if (!strcmp(f[0], "periodic") && (n == 3 || n == 4)) {
        s->pattern = PAT_PERIODIC;
        s->a = intf_time(spec, f[1]);
        s->b = intf_time(spec, f[2]);
        IFATAL_ON(!s->a || !s->b || s->b >= s->a, 1,
                 "--interferer: periodic:PERIOD:ON needs 0 < ON < PERIOD: %s", spec);
        if (n == 4) {
            s->c = intf_time(spec, f[3]) % s->a;
            s->phase_given = true;
        }
    } else if (!strcmp(f[0], "ge") && n == 3) {
        s->pattern = PAT_GE;
        s->a = intf_time(spec, f[1]);
        s->b = intf_time(spec, f[2]);
        IFATAL_ON(!s->a || !s->b, 1, "--interferer: ge:MEAN_ON:MEAN_OFF needs both above 0: %s", spec);
    } else if (!strcmp(f[0], "burst") && n == 3) {
        s->pattern = PAT_BURST;
        s->a = intf_time(spec, f[1]);
        s->b = intf_time(spec, f[2]);
        IFATAL_ON(!s->a || !s->b, 1, "--interferer: burst:ON:MEAN_OFF needs both above 0: %s", spec);
    } else {
        IFATAL(1, "--interferer: pattern: expected const, periodic:PERIOD:ON[:PHASE], ge:MEAN_ON:MEAN_OFF "
              "or burst:ON:MEAN_OFF: %s", spec);
    }
}

static void intf_parse(const char *spec)
{
    struct isrc *s;
    char *copy, *tok, *save, *eq, *v, *colon, *dash;
    bool kind_given = false;

    IFATAL_ON(intf.n >= INTF_MAX, 1, "--interferer: at most %d interferers", INTF_MAX);
    s = intf_parsing = calloc(1, sizeof(*s));
    IFATAL_ON(!s, 1, "calloc: %m");
    s->index = intf.n;
    snprintf(s->name, sizeof(s->name), "i%d", intf.n);
    s->noise = true;
    s->rate_bps = 150000;
    s->pattern = PAT_CONST;
    s->until = UINT64_MAX;
    copy = intf_parsing_copy = strdup(spec);
    IFATAL_ON(!copy, 1, "strdup: %m");
    for (tok = strtok_r(copy, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
        eq = strchr(tok, '=');
        IFATAL_ON(!eq, 1, "--interferer: expected KEY=VALUE: \"%s\" in %s", tok, spec);
        *eq = '\0';
        v = eq + 1;
        if (!strcmp(tok, "name")) {
            IFATAL_ON(!*v || strlen(v) >= sizeof(s->name) || strpbrk(v, " \t"), 1,
                     "--interferer: name: 1 to %zu characters, no space: %s", sizeof(s->name) - 1, spec);
            strcpy(s->name, v);
        } else if (!strcmp(tok, "chan")) {
            intf_parse_chans(s, spec, v);
        } else if (!strcmp(tok, "level")) {
            if (strcmp(v, "off")) {
                s->level = intf_float(spec, "level", v, -200, 50);
                s->has_level = true;
            } else {
                s->has_level = false;
            }
        } else if (!strcmp(tok, "at")) {
            colon = strchr(v, ':');
            IFATAL_ON(!colon, 1, "--interferer: at=IDS:DBM: %s", spec);
            *colon = '\0';
            IFATAL_ON(s->n_at >= INTF_NODES_MAX, 1, "--interferer: at most %d at= entries", INTF_NODES_MAX);
            dash = strchr(v, '-');
            if (dash)
                *dash = '\0';
            s->at[s->n_at].from = intf_long(spec, "at", v, 0, SIM_MAX_NODES - 1);
            s->at[s->n_at].to = dash ? intf_long(spec, "at", dash + 1, s->at[s->n_at].from, SIM_MAX_NODES - 1)
                                     : s->at[s->n_at].from;
            s->at[s->n_at].dbm = strcmp(colon + 1, "off") ? intf_float(spec, "at", colon + 1, -200, 50) : NAN;
            s->n_at++;
        } else if (!strcmp(tok, "pattern")) {
            intf_parse_pattern(s, spec, v);
        } else if (!strcmp(tok, "hop")) {
            IFATAL_ON(strcmp(v, "on") && strcmp(v, "off"), 1, "--interferer: hop=on|off: %s", spec);
            s->hop = !strcmp(v, "on");
        } else if (!strcmp(tok, "kind")) {
            IFATAL_ON(strcmp(v, "noise") && strcmp(v, "fsk"), 1, "--interferer: kind=noise|fsk: %s", spec);
            s->noise = !strcmp(v, "noise");
            kind_given = true;
        } else if (!strcmp(tok, "rate")) {
            s->rate_bps = intf_long(spec, "rate", v, 1000, 10000000);
        } else if (!strcmp(tok, "pre")) {
            s->pre_us = intf_time(spec, v);
            s->pre_given = true;
        } else if (!strcmp(tok, "adj")) {
            char *t2, *save2;

            s->n_adj = 0;
            for (t2 = strtok_r(v, "/", &save2); t2; t2 = strtok_r(NULL, "/", &save2)) {
                IFATAL_ON(s->n_adj >= INTF_ADJ_MAX, 1, "--interferer: adj: at most %d values: %s", INTF_ADJ_MAX, spec);
                s->adj_on[s->n_adj] = strcmp(t2, "off");
                s->adj[s->n_adj] = s->adj_on[s->n_adj] ? intf_float(spec, "adj", t2, -100, 200) : 0;
                s->n_adj++;
            }
            IFATAL_ON(!s->n_adj, 1, "--interferer: adj: no value: %s", spec);
        } else if (!strcmp(tok, "pos")) {
            char *t2, *save2;
            int n = 0;

            for (t2 = strtok_r(v, "/", &save2); t2; t2 = strtok_r(NULL, "/", &save2)) {
                IFATAL_ON(n >= 3, 1, "--interferer: pos=X/Y[/Z]: %s", spec);
                s->pos[n++] = intf_float(spec, "pos", t2, -1e6, 1e6);
            }
            IFATAL_ON(n < 2, 1, "--interferer: pos=X/Y[/Z]: %s", spec);
            s->has_pos = true;
        } else if (!strcmp(tok, "eirp")) {
            s->eirp = intf_float(spec, "eirp", v, -100, 60);
        } else if (!strcmp(tok, "from")) {
            s->from = intf_time(spec, v);
        } else if (!strcmp(tok, "until")) {
            s->until = intf_time(spec, v);
        } else {
            IFATAL(1, "--interferer: unknown key \"%s\": %s", tok, spec);
        }
    }
    free(copy);
    intf_parsing_copy = NULL;
    IFATAL_ON(!s->n_chans, 1, "--interferer: chan= is required: %s", spec);
    IFATAL_ON(!s->has_level && !s->n_at && !s->has_pos, 1, "--interferer: needs level=, at= or pos=: %s", spec);
    IFATAL_ON(s->hop && s->pattern == PAT_CONST, 1, "--interferer: hop=on needs a pattern with bursts: %s", spec);
    IFATAL_ON(s->pre_given && (s->noise || !kind_given), 1, "--interferer: pre= needs kind=fsk: %s", spec);
    IFATAL_ON(s->until <= s->from, 1, "--interferer: until= must be after from=: %s", spec);
    // An FSK interferer sends the Wi-SUN minimum preamble (8 bytes) at the
    // start of each burst, unless pre= says otherwise
    if (!s->noise && !s->pre_given)
        s->pre_us = 64ULL * 1000000 / s->rate_bps;
    intf.src[intf.n++] = s;
}

static void intf_parse_file(const char *path)
{
    FILE *f = fopen(path, "r");
    char *line = NULL, *p, *e;
    size_t cap = 0;

    FATAL_ON(!f, 1, "--interferer-file: %s: %m", path);
    // getline: lines of any length
    while (getline(&line, &cap, f) >= 0) {
        if ((p = strchr(line, '#')))
            *p = '\0';
        for (p = line; *p == ' ' || *p == '\t'; p++)
            ;
        for (e = p + strlen(p); e > p && (e[-1] == '\n' || e[-1] == '\r' || e[-1] == ' ' || e[-1] == '\t'); e--)
            ;
        *e = '\0';
        if (*p)
            intf_parse(p);
    }
    free(line);
    fclose(f);
}

void intf_option(const char *name, const char *arg)
{
    med_note_option();
    if (!strcmp(name, "interferer")) {
        intf_parse(arg);
        intf.given = true;
    } else if (!strcmp(name, "interferer-file")) {
        intf_parse_file(arg);
        intf.given = true;
    } else if (!strcmp(name, "interferer-log")) {
        intf.log_path = arg;
        intf.given = true;
    } else if (!strcmp(name, "medium-adjacent-table")) {
        med_set_adjacent_table(arg);
    } else if (!strcmp(name, "medium-capture-model")) {
        med_set_capture_model(arg);
    } else if (!strcmp(name, "medium-capture-smooth")) {
        med_set_capture_smooth(arg);
    } else if (!strcmp(name, "medium-int-curve-presync")) {
        med_set_int_curve_presync(arg);
    } else if (!strcmp(name, "sniff-adjacent")) {
        med_set_sniff_adjacent(arg);
    } else if (!strcmp(name, "sniff-energy")) {
        med_set_sniff_energy(arg);
    } else if (!strcmp(name, "sniff-energy-us")) {
        med_set_sniff_energy_us(arg);
    } else if (!strcmp(name, "sniff-false")) {
        med_set_sniff_false(arg);
    } else {
        BUG("intf_option: %s", name);
    }
}

/* ---- the schedule */

static uint64_t mix64(uint64_t z)
{
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

static double intf_uniform(struct isrc *s)
{
    return (mix64(s->rng += 0x9e3779b97f4a7c15ULL) >> 11) * 0x1.0p-53;
}

// An exponential duration of this mean, at least 1 µs
static uint64_t intf_exp(struct isrc *s, uint64_t mean)
{
    double d = -(double)mean * log1p(-intf_uniform(s));
    uint64_t v = llround(d);

    return v ? v : 1;
}

static void intf_push(struct isrc *s, uint64_t start, uint64_t end)
{
    struct ivl *iv;

    if (s->head > 64 && s->head * 2 > s->n) {
        memmove(s->iv, s->iv + s->head, (s->n - s->head) * sizeof(*s->iv));
        s->n -= s->head;
        s->head = 0;
    }
    if (s->n == s->cap) {
        s->cap = s->cap ? s->cap * 2 : 64;
        s->iv = realloc(s->iv, s->cap * sizeof(*s->iv));
        FATAL_ON(!s->iv, 1, "realloc: %m");
    }
    iv = &s->iv[s->n++];
    iv->s = start;
    iv->e = end;
    iv->chan = s->hop ? s->chans[(int)(intf_uniform(s) * s->n_chans) % s->n_chans] : s->chans[0];
    s->n_gen++;
    s->on_gen += end - start;
    if (intf.log) {
        if (s->hop || s->n_chans == 1)
            fprintf(intf.log, "%llu %llu %s %u\n", (unsigned long long)start, (unsigned long long)end, s->name,
                    iv->chan);
        else
            fprintf(intf.log, "%llu %llu %s all\n", (unsigned long long)start, (unsigned long long)end, s->name);
    }
}

// Generate the intervals until every one that starts at or before upto is known
static void intf_gen(struct isrc *s, uint64_t upto)
{
    uint64_t start, on;

    if (s->pattern == PAT_CONST)
        return;
    if (!s->gen_init) {
        s->gen_init = true;
        s->gen_t = 0;
        switch (s->pattern) {
        case PAT_PERIODIC:
            if (!s->phase_given)
                s->c = (uint64_t)(intf_uniform(s) * s->a) % s->a;
            // The interval that started before 0 and is still on at 0
            if (s->c + s->b > s->a) {
                intf_push(s, 0, s->c + s->b - s->a);
                s->gen_t = s->c + s->b - s->a;
            }
            break;
        case PAT_GE:
            // The stationary state at 0; a sojourn is memoryless
            if (intf_uniform(s) < (double)s->a / (s->a + s->b)) {
                on = intf_exp(s, s->a);
                intf_push(s, 0, on);
                s->gen_t = on;
            }
            break;
        }
    }
    while (s->gen_t <= upto) {
        switch (s->pattern) {
        case PAT_PERIODIC:
            start = s->c + s->period_k * s->a;
            s->period_k++;
            intf_push(s, start, start + s->b);
            s->gen_t = start + s->b;
            break;
        case PAT_GE:
            start = s->gen_t + intf_exp(s, s->b);
            on = intf_exp(s, s->a);
            intf_push(s, start, start + on);
            s->gen_t = start + on;
            break;
        case PAT_BURST:
            start = s->gen_t + intf_exp(s, s->b);
            intf_push(s, start, start + s->a);
            s->gen_t = start + s->a;
            break;
        }
    }
}

/* ---- the run */

static void intf_cf_setup(struct isrc *s);

// Levels, coupling and stream of interferer i (at the start, or when a
// scenario adds it)
static void intf_src_init(struct isrc *s, int i)
{
    int k, node;
    float dbm;

    s->rng = mix64(intf.seed + mix64((uint64_t)i + 1) + INTF_RNG_K);
    intf_cf_setup(s);
    s->lvl = malloc(SIM_MAX_NODES * sizeof(*s->lvl));
    s->lvl_mw = malloc(SIM_MAX_NODES * sizeof(*s->lvl_mw));
    FATAL_ON(!s->lvl || !s->lvl_mw, 1, "malloc: %m");
    FATAL_ON(s->has_pos && !intf.pos_fn, 1,
             "interferer %s: pos= needs node positions (a propagation model): none is loaded", s->name);
    for (node = 0; node < SIM_MAX_NODES; node++) {
        s->lvl[node] = s->has_level ? s->level : NAN;
        if (s->has_pos && intf.pos_fn(node, s->pos, s->eirp, &dbm))
            s->lvl[node] = dbm;
    }
    for (k = 0; k < s->n_at; k++)
        for (node = s->at[k].from; node <= s->at[k].to; node++)
            s->lvl[node] = s->at[k].dbm;
    for (node = 0; node < SIM_MAX_NODES; node++)
        s->lvl_mw[node] = isnan(s->lvl[node]) ? 0 : pow(10, s->lvl[node] / 10);
    if (s->pattern == PAT_CONST)
        INFO("medium: interferer %s: %s, %d channel%s%s, constant", s->name, s->noise ? "noise" : "fsk",
             s->n_chans, s->n_chans > 1 ? "s" : "", s->has_level ? "" : ", levels per node");
    else
        INFO("medium: interferer %s: %s, %d channel%s%s%s, %s %llu/%llu us", s->name, s->noise ? "noise" : "fsk",
             s->n_chans, s->n_chans > 1 ? "s" : "", s->hop ? " (hopping)" : "",
             s->has_level ? "" : ", levels per node", pat_names[s->pattern], (unsigned long long)s->a,
             (unsigned long long)s->b);
    if (s->from || s->until != UINT64_MAX)
        INFO("medium: interferer %s: active from %.6f s%s%.6f s", s->name, s->from / 1e6,
             s->until == UINT64_MAX ? "" : " until ", s->until == UINT64_MAX ? 0.0 : s->until / 1e6);
}

void intf_start(uint64_t seed)
{
    int i;

    intf.seed = seed;
    FATAL_ON(intf.log_path && !intf.n, 1, "--interferer-log needs --interferer or --interferer-file");
    if (intf.log_path) {
        intf.log = fopen(intf.log_path, "w");
        FATAL_ON(!intf.log, 1, "%s: %m", intf.log_path);
    }
    for (i = 0; i < intf.n; i++)
        intf_src_init(intf.src[i], i);
    intf.started = true;
}

bool intf_any(void)
{
    return intf.n > 0 || intf.dynamic;
}

void intf_set_dynamic(void)
{
    intf.dynamic = true;
}

const char *intf_add_runtime(const char *spec, uint64_t now, char *err, size_t errlen)
{
    struct isrc *s;
    jmp_buf jb;
    int slot = -1;
    static struct isrc *volatile old;

    if (intf.n >= INTF_MAX) {
        // The slot of an interferer removed before now is reused
        for (int i = 0; i < intf.n && slot < 0; i++)
            if (intf.src[i]->until <= now)
                slot = i;
        if (slot < 0) {
            snprintf(err, errlen, "at most %d interferers active or not yet removed", INTF_MAX);
            return NULL;
        }
    }
    intf_parsing = NULL;
    intf_parsing_copy = NULL;
    old = NULL;
    if (setjmp(jb)) {
        intf_rt_jmp = NULL;
        snprintf(err, errlen, "%s", intf_rt_msg);
        free(intf_parsing_copy);
        free(intf_parsing);
        if (old) {          // the removed interferer back in its slot
            intf.src[intf.n++] = intf.src[slot];
            intf.src[slot] = old;
        }
        return NULL;
    }
    intf_rt_jmp = &jb;
    if (slot >= 0) {
        // intf_parse() appends: make room, then move the new one there
        old = intf.src[slot];
        intf.src[slot] = intf.src[--intf.n];
        intf_parse(spec);
        s = intf.src[intf.n - 1];
        intf.src[intf.n - 1] = intf.src[slot];
        intf.src[slot] = s;
        s->index = slot;
        INFO("medium: interferer %s (removed) frees its slot", old->name);
        free(old->iv);
        free(old->lvl);
        free(old->lvl_mw);
        free(old);
    } else {
        intf_parse(spec);
        s = intf.src[intf.n - 1];
        slot = intf.n - 1;
    }
    intf_rt_jmp = NULL;
    free(intf_parsing_copy);
    intf_parsing_copy = NULL;
    for (int i = 0; i < intf.n; i++)
        if (i != slot && !strcmp(intf.src[i]->name, s->name) && intf.src[i]->until > now) {
            snprintf(err, errlen, "an active interferer is already named %s", s->name);
            intf.src[slot] = intf.src[--intf.n];
            free(s);
            return NULL;
        }
    if (s->from < now)
        s->from = now;
    intf_src_init(s, s->index);
    INFO("medium: interferer %s added at %.6f s (scenario)", s->name, now / 1e6);
    return s->name;
}

int intf_remove_runtime(const char *name, uint64_t now)
{
    int n = 0;

    for (int i = 0; i < intf.n; i++) {
        struct isrc *s = intf.src[i];
        if ((strcmp(name, "all") && strcmp(name, s->name)) || s->until <= now)
            continue;
        s->until = now > s->from ? now : s->from;
        INFO("medium: interferer %s removed at %.6f s (scenario)", s->name, now / 1e6);
        n++;
    }
    return n;
}

void intf_advance(uint64_t now)
{
    struct isrc *s;
    int i;

    if (now > intf.now)
        intf.now = now;
    if (now < INTF_LOOKBACK_US)
        return;
    for (i = 0; i < intf.n; i++) {
        s = intf.src[i];
        while (s->head < s->n && s->iv[s->head].e + INTF_LOOKBACK_US <= now)
            s->head++;
    }
}

// The coupling of a source on channel from into channel rx (a factor)
// The coupling d channels away, from the source's adj= table, else the
// medium's table of its rate (fsk), else none (noise); computed once at the
// start (intf_cf_setup())
static double intf_coupling_d(const struct isrc *s, int d)
{
    if (!d)
        return 1;
    if (s->n_adj)
        return d <= s->n_adj && s->adj_on[d - 1] ? pow(10, -s->adj[d - 1] / 10) : 0;
    if (!s->noise)
        return med_adjacent_factor(s->rate_bps, d);
    return 0;
}

static void intf_cf_setup(struct isrc *s)
{
    s->n_cf = 0;
    for (int d = 0; d <= INTF_CF_MAX; d++) {
        s->cf[d] = intf_coupling_d(s, d);
        if (s->cf[d] > 0)
            s->n_cf = d + 1;
    }
    for (int k = 0; k < INTF_CACHE; k++)
        s->cache_chan[k] = -1;
}

// The coupling of a source on channel from into channel rx (a factor)
static double intf_coupling(const struct isrc *s, uint16_t from, uint16_t rx)
{
    int d = from > rx ? from - rx : rx - from;

    return d < s->n_cf ? s->cf[d] : 0;
}

// The power (mW) that an on-interval of s puts on chan at node
static double intf_piece_mw(struct isrc *s, const struct ivl *iv, int node, uint16_t chan)
{
    double sum = 0;
    int k;

    if (iv && s->hop)
        return s->lvl_mw[node] * intf_coupling(s, iv->chan, chan);
    // A wideband source: the sum over its channels, cached per channel
    k = chan % INTF_CACHE;
    if (s->cache_chan[k] != chan) {
        for (int c = 0; c < s->n_chans; c++)
            sum += intf_coupling(s, s->chans[c], chan);
        s->cache_chan[k] = chan;
        s->cache_sum[k] = sum;
    }
    return s->lvl_mw[node] * s->cache_sum[k];
}

// The first interval of s that ends after t (index), or s->n
static int intf_find(const struct isrc *s, uint64_t t)
{
    int lo = s->head, hi = s->n;

    while (lo < hi) {
        int mid = (lo + hi) / 2;

        if (s->iv[mid].e <= t)
            lo = mid + 1;
        else
            hi = mid;
    }
    return lo;
}

void intf_power(int node, uint16_t chan, uint64_t t, double *fsk, double *noise)
{
    struct isrc *s;
    double mw;
    int i, k;

    *fsk = *noise = 0;
    for (i = 0; i < intf.n; i++) {
        s = intf.src[i];
        if (!s->lvl_mw[node] || t < s->from || t >= s->until)
            continue;
        if (s->pattern == PAT_CONST) {
            mw = intf_piece_mw(s, NULL, node, chan);
        } else {
            intf_gen(s, t);
            k = intf_find(s, t);
            if (k >= s->n || s->iv[k].s > t)
                continue;
            mw = intf_piece_mw(s, &s->iv[k], node, chan);
        }
        if (s->noise)
            *noise += mw;
        else
            *fsk += mw;
    }
}

void intf_each(int node, uint16_t chan, uint64_t from, uint64_t to, intf_cb cb, void *ctx)
{
    struct intf_piece p;
    struct isrc *s;
    int i, k;

    if (from >= to)
        return;
    for (i = 0; i < intf.n; i++) {
        uint64_t wf, wt;

        s = intf.src[i];
        if (!s->lvl_mw[node])
            continue;
        // Its active window
        wf = from > s->from ? from : s->from;
        wt = to < s->until ? to : s->until;
        if (wf >= wt)
            continue;
        p.noise = s->noise;
        p.rate_bps = s->rate_bps;
        p.src = i;
        if (s->pattern == PAT_CONST) {
            p.start = wf;
            p.end = wt;
            p.ostart = s->from;
            p.oend = s->until;
            p.mw = intf_piece_mw(s, NULL, node, chan);
            p.pre_end = 0;
            if (p.mw > 0)
                cb(ctx, &p);
            continue;
        }
        intf_gen(s, wt);
        for (k = intf_find(s, wf); k < s->n && s->iv[k].s < wt; k++) {
            p.mw = intf_piece_mw(s, &s->iv[k], node, chan);
            if (!(p.mw > 0))
                continue;
            // An interval cut by the window starts (or ends) with it
            p.ostart = s->iv[k].s > s->from ? s->iv[k].s : s->from;
            p.oend = s->iv[k].e < s->until ? s->iv[k].e : s->until;
            p.start = p.ostart > wf ? p.ostart : wf;
            p.end = p.oend < wt ? p.oend : wt;
            if (p.start >= p.end)
                continue;
            p.pre_end = s->noise || !s->pre_us ? 0 : p.ostart + s->pre_us;
            if (p.pre_end > p.oend)
                p.pre_end = p.oend;
            cb(ctx, &p);
        }
    }
}

double intf_max_mw(int node, uint16_t chan, uint64_t from, uint64_t to)
{
    // The sum is highest at the start of some piece (or at from)
    struct { uint64_t t[256]; int n; } pts = { .n = 0 };
    double best = 0, fsk, noise;
    struct isrc *s;
    int i, k;

    if (from >= to)
        return 0;
    pts.t[pts.n++] = from;
    for (i = 0; i < intf.n; i++) {
        s = intf.src[i];
        if (!s->lvl_mw[node])
            continue;
        if (s->from > from && s->from < to && pts.n < 256)
            pts.t[pts.n++] = s->from;
        if (s->pattern == PAT_CONST)
            continue;
        intf_gen(s, to);
        for (k = intf_find(s, from); k < s->n && s->iv[k].s < to && pts.n < 256; k++)
            if (s->iv[k].s > from)
                pts.t[pts.n++] = s->iv[k].s;
    }
    for (i = 0; i < pts.n; i++) {
        intf_power(node, chan, pts.t[i], &fsk, &noise);
        if (fsk + noise > best)
            best = fsk + noise;
    }
    return best;
}

void intf_finish(void)
{
    struct isrc *s;
    uint64_t n, on;
    int i, k;

    for (i = 0; i < intf.n; i++) {
        s = intf.src[i];
        if (s->pattern == PAT_CONST) {
            INFO("medium: interferer %s: on all the time", s->name);
        } else {
            // What was generated beyond the run's end does not count
            n = s->n_gen;
            on = s->on_gen;
            for (k = s->head; k < s->n; k++) {
                if (s->iv[k].s >= intf.now) {
                    n--;
                    on -= s->iv[k].e - s->iv[k].s;
                } else if (s->iv[k].e > intf.now) {
                    on -= s->iv[k].e - intf.now;
                }
            }
            INFO("medium: interferer %s: %llu bursts, on %.3f %% of %.3f s", s->name, (unsigned long long)n,
                 intf.now ? 100.0 * on / intf.now : 0.0, intf.now / 1e6);
        }
        free(s->iv);
        free(s->lvl);
        free(s->lvl_mw);
        free(s);
        intf.src[i] = NULL;
    }
    intf.n = 0;
    if (intf.log)
        fclose(intf.log);
    intf.log = NULL;
}
