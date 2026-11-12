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
#include "sim_channel.h"
#include "sim_interf.h"
#include "sim_medium.h"
#include "sim_propagation.h"

// kTB at 293 K and 100 kHz, NF 4 dB: the constant of the radio's loss curve
// (app_wshwsim/sl_rf_driver.c)
#define MED_NOISE_DBM (-153.930645 + 30 + 4)
// Separates the verdict streams from the port's random source and the drift
#define MED_RNG_K 0x6d656469756d0001ULL
// The smooth capture model's streams (§13.3)
#define MED_RNG_CAPTURE_K 0x6361707475720001ULL

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
    struct chan_sample cs;      // the link's variation at the frame's start (§12)
};

struct med_frame {
    uint32_t id;
    int src;
    uint16_t chan;
    uint8_t phy;                // MED_PHY_*: the reception profile of its data rate (§2.1)
    uint64_t own_shr_us;        // airtime of its own SHR, without an "xl" preamble: [t_bits, t_bits + own_shr_us)
    uint64_t own_pre_us;        // the preamble part of it (the SFD follows)
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
    bool noise;                 // a noise-like external interferer (§13)
    bool pre;                   // on air before the received frame's SHR ended (§13.3)
};

struct med_lock {
    int node, src;
    uint32_t frame_id;
    uint16_t chan;
    uint8_t phy;
    uint64_t t_start, t_end, shr_us;
    uint64_t t_shr;             // start of the frame's own SHR (after an "xl" preamble)
    uint64_t own_shr_us, own_pre_us;
    uint32_t rate_bps;
    bool broken;                // a stronger frame broke the sync search: lost, verdict sent at once (§3)
    int refs;                   // verdict events that hold it (2 once broken)
    uint64_t t_bits;            // the counted bits are spread over [t_bits, t_end)
    unsigned int bits;
    float dbm;
    double mw;
    struct chan_sample cs;
    bool active;                // false once captured, aborted or left
    bool delivered;             // the receiver ran after its "xx"
    int n_intf, cap_intf;
    struct med_intf *intf;
    double ext_mw;              // highest external interference over the frame (§13)
};

/* Reception profiles per PHY (§2.1): interference error rate, capture,
 * adjacent channel. A frame takes the profile of its data rate: 50 kbit/s
 * the mode-1a values, 150 kbit/s the mode-3 values, any other rate "other".
 * Defaults (--medium-model=fg25): the FG25 kit measurements of 2026-11-09,
 * firmware repo docs/COCHANNEL_RESULTS.md (section in each comment). */
enum { MED_PHY_1A, MED_PHY_3, MED_PHY_OTHER, MED_PHYS };
static const char *const phy_names[MED_PHYS] = { "1a", "3", "other" };
#define MED_EARLY_SHR (-1)      // early capture window: the whole SHR (EM4)
#define MED_ADJ_MAX 16

struct med_phy {
    bool int_noise;             // interference as noise in the noise curve (EM4)
    float int_x0, int_b;        // BER_int = min(0.5, 0.5 exp(-(SIR - x0) / b))
    float capture_db;           // a frame this much stronger captures, inside the windows
    int early_bits;             // early window, bits from the SHR's start (MED_EARLY_SHR: whole SHR)
    bool sfd_capture;           // capture also while the received frame's SFD is on air
    bool has_break;             // a stronger frame between the windows breaks the sync search
    float break_db;
    int n_adj;                  // the channels 1 .. n_adj away interfere, adj[k - 1] weaker (§13.2)
    float adj[MED_ADJ_MAX];
    bool adj_on[MED_ADJ_MAX];
    bool presync;               // interferers on air at the end of the SHR: their own curve (§13.3)
    bool presync_given;
    float pre_x0, pre_b;
    bool smooth;                // capture and break by probability (§13.3)
    float cap_m50, cap_s;       // capture probability against the margin: logistic, dB
    float brk_s;                // break probability: logistic around break_db, dB
    float early_c, early_slope; // early window: centre in bits at 5 dB of margin, bits per dB
    float edge_w;               // width of the windows' edges, bits
    float sfd_rise, sfd_fall;   // SFD window: from the preamble's end + rise to the SHR's end + fall, bits
};

static const struct med_phy med_phy_fg25[MED_PHYS] = {
    // EU mode 1a: §5 (x0 -2.40 dB, b 0.90 dB; early window about 400 µs,
    // no capture around the SFD); capture margin, break margin, adjacent
    // channel: the mode-3 values (§6; not measured in mode 1a)
    [MED_PHY_1A] = { .int_x0 = -2.40, .int_b = 0.90, .capture_db = 3, .early_bits = 20, .has_break = true,
                     .break_db = 2.5, .n_adj = 1, .adj = { 10 }, .adj_on = { true } },
    // EU mode 3: §4.1 (x0 -2.85 dB, b 0.90 dB), §4.2 (capture margin about
    // 3 dB; early window to the 50 % transition at 250 µs = 37 bits;
    // capture during the SFD, 650-700 µs; both lost in between from about
    // 2.5 dB, §6 item 3), §4.4 (adjacent channel 10 dB weaker)
    [MED_PHY_3]  = { .int_x0 = -2.85, .int_b = 0.90, .capture_db = 3, .early_bits = 37, .sfd_capture = true,
                     .has_break = true, .break_db = 2.5, .n_adj = 1, .adj = { 10 }, .adj_on = { true } },
    // Other rates: not measured; the mode-3 values (§6 item 1: one curve for
    // every FSK mode)
    [MED_PHY_OTHER] = { .int_x0 = -2.85, .int_b = 0.90, .capture_db = 3, .early_bits = 37, .sfd_capture = true,
                        .has_break = true, .break_db = 2.5, .n_adj = 1, .adj = { 10 }, .adj_on = { true } },
};

// --medium-model=em4: the MVP before the calibration (interference in the
// noise curve, capture by 6 dB over the whole SHR, no adjacent channel)
static const struct med_phy med_phy_em4 = { .int_noise = true, .int_x0 = 0, .int_b = 2, .capture_db = 6,
                                            .early_bits = MED_EARLY_SHR };

/* --medium-capture-model=smooth (§13.3): probabilities fitted to the FG25
 * kit measurements (firmware repo docs/COCHANNEL_RESULTS.md §4.2, §5: all
 * 256 power pairs of the capture runs, sim_utils/vtime/capture_kit_points.json)
 * by sim_utils/vtime/capture_fit.py (maximum likelihood, with the kits' SIR
 * error; cross-validated in docs/MEDIUM.md §13.3). Capture: logistic in the
 * margin, centre cap_m50, scale cap_s, times a weight in time (early window,
 * SFD window) with edges of width edge_w bits. Break: logistic around the
 * break margin (brk_s assumed: measured only from +5 dB). Interferers already
 * on air at the end of the received frame's SHR: their own curve (pre_x0,
 * pre_b), fitted with the rest, one curve for both PHYs. */
struct med_smooth {
    float cap_m50, cap_s, brk_s, early_c, early_slope, edge_w, sfd_rise, sfd_fall, pre_x0, pre_b;
};
static const struct med_smooth med_smooth_fit[MED_PHYS] = {
    // Mode 1a (§5): the early window grows with the margin above 5 dB; no
    // SFD window (rise, fall unused)
    [MED_PHY_1A] = { 0, 0.85, 0.5, 12.55, 3.94, 4.64, -4, 1, -2.72, 0.54 },
    // Mode 3 (§4.2)
    [MED_PHY_3] = { 0.99, 0.49, 0.5, 38.0, 0, 1.59, -4.53, 0.75, -2.72, 0.54 },
    // Other rates: not measured; the mode-3 values
    [MED_PHY_OTHER] = { 0.99, 0.49, 0.5, 38.0, 0, 1.59, -4.53, 0.75, -2.72, 0.54 },
};

// A per-PHY option, applied over the model's values at med_start()
enum { PO_INT, PO_CAPTURE, PO_EARLY, PO_SFD, PO_BREAK, PO_ADJ, PO_ADJ_TABLE, PO_MODEL, PO_SMOOTH, PO_PRESYNC };
struct med_phy_opt {
    unsigned int mask;          // the profiles it sets
    int field;
    bool on;
    float a, b;
    int n;                      // PO_ADJ_TABLE: entries; PO_SMOOTH: the key
    float t[MED_ADJ_MAX];
    bool t_on[MED_ADJ_MAX];
};
#define MED_PHY_OPTS_MAX 64

static struct {
    bool given, tuning, central, mode_given;
    bool has_tx_ref;
    bool em4;                   // --medium-model=em4
    float tx_ref, lock_snr;
    struct med_phy phy[MED_PHYS];
    struct med_phy_opt opts[MED_PHY_OPTS_MAX];
    int n_opts;
    uint64_t broken;            // sync searches broken (§3)
    struct med_lock *last_broken; // broken by the last med_decide()
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
    uint64_t cca_ext;           // CCA queries with an external interferer on (§13)
    uint64_t smooth_draws;      // draws of the smooth capture model (§13.3)
    uint64_t rng_c[SIM_MAX_NODES];
    bool rng_c_set[SIM_MAX_NODES];
} med = {
    .lock_snr = 0,
};

/* ---- Preamble sniffing: settings and state (§10) */

#define SN_CURVE_MAX 64
// Separates the detection streams from the verdict streams
#define SN_RNG_K 0x736e696666000001ULL

enum { SN_ADJ_OFF, SN_ADJ_ON, SN_ADJ_DETECT };
// Separates the false-detection streams from the others
#define SN_RNG_FALSE_K 0x736e66616c730001ULL
// The source of a detection that is no emulated frame (frame_id 0)
#define SN_SRC_NONE 0xFFFF

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
    bool cand;                  // a candidate for detection (else only interference and energy, §13.4)
    bool adj;                   // a frame on another channel (--sniff-adjacent)
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
    uint64_t e_done;            // energy detection: the checks up to this time are done (§13.4)
    uint64_t f_time;            // a false detection at this time (MED_SNIFF_NONE: none)
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
    // §13.4
    int adjacent;               // SN_ADJ_*: frames on the next channels in sniffing
    bool has_energy;            // --sniff-energy: detection on the summed energy
    float energy;
    uint32_t energy_us;
    bool false_rate;            // --sniff-false: a rate per second (else a probability per window)
    double false_p;
    uint64_t rng_f[SIM_MAX_NODES];
    bool rng_f_set[SIM_MAX_NODES];
    uint64_t det_energy, det_false, det_ext, det_adj;
} sn = {
    .energy_us = 107,
    .dwell_bits = 16,
    .curve_ber_bits = 16,
    .sfd_bits = 16,
};

/* ---- options */

static float med_number(const char *opt, const char *arg, float min, float max)
{
    char *end;
    float v = strtof(arg, &end);

    FATAL_ON(end == arg || *end || !(v >= min && v <= max), 1, "%s: expected a number from %g to %g: %s", opt, min, max, arg);
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

void med_set_model(const char *arg)
{
    if (!strcmp(arg, "fg25"))
        med.em4 = false;
    else if (!strcmp(arg, "em4"))
        med.em4 = true;
    else
        FATAL(1, "--medium-model: expected fg25 or em4: %s", arg);
    med.given = med.tuning = true;
}

// [MODE:]VALUE: the profiles MODE names (1a, 1b = 1a, 3, other), all without
// MODE. Returns VALUE.
static const char *med_phy_prefix(const char *opt, const char *arg, unsigned int *mask)
{
    static const struct { const char *name; int phy; } names[] = {
        { "1a", MED_PHY_1A }, { "1b", MED_PHY_1A }, { "3", MED_PHY_3 }, { "other", MED_PHY_OTHER },
    };
    const char *colon = strchr(arg, ':');
    size_t i;

    *mask = (1u << MED_PHYS) - 1;
    if (!colon)
        return arg;
    for (i = 0; i < sizeof(names) / sizeof(*names); i++)
        if (strlen(names[i].name) == (size_t)(colon - arg) && !strncasecmp(arg, names[i].name, colon - arg)) {
            *mask = 1u << names[i].phy;
            return colon + 1;
        }
    FATAL(1, "%s: expected [1a|1b|3|other:]VALUE: %s", opt, arg);
}

static struct med_phy_opt *med_phy_opt(const char *opt, const char *arg, int field, const char **val)
{
    struct med_phy_opt *o;

    FATAL_ON(med.n_opts >= MED_PHY_OPTS_MAX, 1, "%s: at most %d per-PHY medium options", opt, MED_PHY_OPTS_MAX);
    o = &med.opts[med.n_opts++];
    *val = med_phy_prefix(opt, arg, &o->mask);
    o->field = field;
    o->on = true;
    med.given = med.tuning = true;
    return o;
}

void med_set_capture_db(const char *arg)
{
    const char *v;
    struct med_phy_opt *o = med_phy_opt("--medium-capture-db", arg, PO_CAPTURE, &v);

    o->a = med_number("--medium-capture-db", v, -100, 100);
}

// --medium-int-curve=[MODE:]X0,B|noise
void med_set_int_curve(const char *arg)
{
    const char *v;
    struct med_phy_opt *o = med_phy_opt("--medium-int-curve", arg, PO_INT, &v);
    char *end;

    if (!strcmp(v, "noise")) {
        o->on = false;
        return;
    }
    o->a = strtof(v, &end);
    FATAL_ON(end == v || *end != ',' || !isfinite(o->a) || o->a < -50 || o->a > 50, 1,
             "--medium-int-curve: expected [MODE:]X0,B (dB) or [MODE:]noise: %s", arg);
    v = end + 1;
    o->b = strtof(v, &end);
    FATAL_ON(end == v || *end || !(o->b > 0 && o->b <= 50), 1,
             "--medium-int-curve: expected [MODE:]X0,B with B above 0 dB: %s", arg);
}

// --medium-capture-early-bits=[MODE:]N|shr
void med_set_capture_early(const char *arg)
{
    const char *v;
    struct med_phy_opt *o = med_phy_opt("--medium-capture-early-bits", arg, PO_EARLY, &v);

    if (!strcmp(v, "shr")) {
        o->on = false;
        return;
    }
    o->a = med_number("--medium-capture-early-bits", v, 0, 100000);
    FATAL_ON(o->a != (int)o->a, 1, "--medium-capture-early-bits: expected a whole number or shr: %s", arg);
}

// --medium-capture-sfd=[MODE:]on|off
void med_set_capture_sfd(const char *arg)
{
    const char *v;
    struct med_phy_opt *o = med_phy_opt("--medium-capture-sfd", arg, PO_SFD, &v);

    if (!strcmp(v, "on"))
        o->on = true;
    else if (!strcmp(v, "off"))
        o->on = false;
    else
        FATAL(1, "--medium-capture-sfd: expected [MODE:]on|off: %s", arg);
}

// --medium-break-db=[MODE:]DB|off, --medium-adjacent-db=[MODE:]DB|off
static void med_set_db_or_off(const char *opt, const char *arg, int field)
{
    const char *v;
    struct med_phy_opt *o = med_phy_opt(opt, arg, field, &v);

    if (!strcmp(v, "off"))
        o->on = false;
    else
        o->a = med_number(opt, v, -100, 200);
}

void med_set_break_db(const char *arg)
{
    med_set_db_or_off("--medium-break-db", arg, PO_BREAK);
}

void med_set_adjacent_db(const char *arg)
{
    med_set_db_or_off("--medium-adjacent-db", arg, PO_ADJ);
}

/* ---- options of §13 (through sim_interf.c's intf_option()) */

void med_note_option(void)
{
    med.given = med.tuning = true;
}

// --medium-adjacent-table=[MODE:]DB/DB/...: 1, 2, ... channels away; off
// for no coupling at that distance; nothing beyond the last entry
void med_set_adjacent_table(const char *arg)
{
    const char *v;
    struct med_phy_opt *o = med_phy_opt("--medium-adjacent-table", arg, PO_ADJ_TABLE, &v);
    char *copy = strdup(v), *tok, *save;

    FATAL_ON(!copy, 1, "strdup: %m");
    for (tok = strtok_r(copy, "/", &save); tok; tok = strtok_r(NULL, "/", &save)) {
        FATAL_ON(o->n >= MED_ADJ_MAX, 1, "--medium-adjacent-table: at most %d values: %s", MED_ADJ_MAX, arg);
        o->t_on[o->n] = strcmp(tok, "off");
        o->t[o->n] = o->t_on[o->n] ? med_number("--medium-adjacent-table", tok, -100, 200) : 0;
        o->n++;
    }
    free(copy);
    FATAL_ON(!o->n, 1, "--medium-adjacent-table: expected [MODE:]DB/DB/...: %s", arg);
}

// --medium-capture-model=[MODE:]step|smooth
void med_set_capture_model(const char *arg)
{
    const char *v;
    struct med_phy_opt *o = med_phy_opt("--medium-capture-model", arg, PO_MODEL, &v);

    if (!strcmp(v, "smooth"))
        o->on = true;
    else if (!strcmp(v, "step"))
        o->on = false;
    else
        FATAL(1, "--medium-capture-model: expected [MODE:]step|smooth: %s", arg);
}

static const char *const smooth_keys[] = { "m50", "s", "brk_s", "early", "slope", "w", "rise", "fall" };
#define SMOOTH_KEYS ((int)(sizeof(smooth_keys) / sizeof(*smooth_keys)))

// --medium-capture-smooth=[MODE:]KEY=V,...
void med_set_capture_smooth(const char *arg)
{
    unsigned int mask;
    const char *v = med_phy_prefix("--medium-capture-smooth", arg, &mask);
    char *copy = strdup(v), *tok, *save, *eq;
    struct med_phy_opt *o;
    int k;

    FATAL_ON(!copy, 1, "strdup: %m");
    for (tok = strtok_r(copy, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
        eq = strchr(tok, '=');
        FATAL_ON(!eq, 1, "--medium-capture-smooth: expected [MODE:]KEY=V,...: %s", arg);
        *eq = '\0';
        for (k = 0; k < SMOOTH_KEYS && strcmp(tok, smooth_keys[k]); k++)
            ;
        FATAL_ON(k == SMOOTH_KEYS, 1, "--medium-capture-smooth: unknown key %s (m50, s, brk_s, early, slope, w, "
                 "rise, fall): %s", tok, arg);
        FATAL_ON(med.n_opts >= MED_PHY_OPTS_MAX, 1, "at most %d per-PHY medium options", MED_PHY_OPTS_MAX);
        o = &med.opts[med.n_opts++];
        o->mask = mask;
        o->field = PO_SMOOTH;
        o->n = k;
        o->a = med_number("--medium-capture-smooth", eq + 1, -1000, 1000);
        FATAL_ON((k == 1 || k == 2 || k == 5) && !(o->a > 0), 1, "--medium-capture-smooth: %s must be above 0: %s",
                 tok, arg);
    }
    free(copy);
}

// --medium-int-curve-presync=[MODE:]X0,B|same
void med_set_int_curve_presync(const char *arg)
{
    const char *v;
    struct med_phy_opt *o = med_phy_opt("--medium-int-curve-presync", arg, PO_PRESYNC, &v);
    char *end;

    if (!strcmp(v, "same")) {
        o->on = false;
        return;
    }
    o->a = strtof(v, &end);
    FATAL_ON(end == v || *end != ',' || !isfinite(o->a) || o->a < -50 || o->a > 50, 1,
             "--medium-int-curve-presync: expected [MODE:]X0,B (dB) or [MODE:]same: %s", arg);
    v = end + 1;
    o->b = strtof(v, &end);
    FATAL_ON(end == v || *end || !(o->b > 0 && o->b <= 50), 1,
             "--medium-int-curve-presync: expected [MODE:]X0,B with B above 0 dB: %s", arg);
}

void med_set_sniff_adjacent(const char *arg)
{
    if (!strcmp(arg, "off"))
        sn.adjacent = SN_ADJ_OFF;
    else if (!strcmp(arg, "on"))
        sn.adjacent = SN_ADJ_ON;
    else if (!strcmp(arg, "detect"))
        sn.adjacent = SN_ADJ_DETECT;
    else
        FATAL(1, "--sniff-adjacent: expected off, on or detect: %s", arg);
}

void med_set_sniff_energy(const char *arg)
{
    if (!strcmp(arg, "off")) {
        sn.has_energy = false;
        return;
    }
    sn.energy = med_number("--sniff-energy", arg, -200, 100);
    sn.has_energy = true;
}

void med_set_sniff_energy_us(const char *arg)
{
    float v = med_number("--sniff-energy-us", arg, 1, 1000000);

    FATAL_ON(v != (uint32_t)v, 1, "--sniff-energy-us: expected a whole number: %s", arg);
    sn.energy_us = v;
}

// --sniff-false=P (per window) or RATE/s (per second of listening)
void med_set_sniff_false(const char *arg)
{
    char *end;
    double v = strtod(arg, &end);

    if (!strcmp(end, "/s")) {
        FATAL_ON(end == arg || !(v >= 0 && v <= 1e6), 1, "--sniff-false: expected RATE/s from 0 to 1e6: %s", arg);
        sn.false_rate = true;
    } else {
        FATAL_ON(end == arg || *end || !(v >= 0 && v <= 1), 1,
                 "--sniff-false: expected a probability per window (0 to 1) or RATE/s: %s", arg);
        sn.false_rate = false;
    }
    sn.false_p = v;
}

// The profiles: the model's values, then the per-PHY options in their order
static void med_phy_setup(void)
{
    int p, i;

    for (p = 0; p < MED_PHYS; p++) {
        const struct med_smooth *sm = &med_smooth_fit[p];
        struct med_phy *x = &med.phy[p];

        *x = med.em4 ? med_phy_em4 : med_phy_fg25[p];
        // The smooth model's parameters, used once --medium-capture-model=smooth
        x->cap_m50 = sm->cap_m50;
        x->cap_s = sm->cap_s;
        x->brk_s = sm->brk_s;
        x->early_c = sm->early_c;
        x->early_slope = sm->early_slope;
        x->edge_w = sm->edge_w;
        x->sfd_rise = sm->sfd_rise;
        x->sfd_fall = sm->sfd_fall;
    }
    for (i = 0; i < med.n_opts; i++) {
        const struct med_phy_opt *o = &med.opts[i];

        for (p = 0; p < MED_PHYS; p++) {
            struct med_phy *x = &med.phy[p];

            if (!(o->mask & (1u << p)))
                continue;
            switch (o->field) {
            case PO_INT:
                x->int_noise = !o->on;
                if (o->on) {
                    x->int_x0 = o->a;
                    x->int_b = o->b;
                }
                break;
            case PO_CAPTURE: x->capture_db = o->a; break;
            case PO_EARLY:   x->early_bits = o->on ? (int)o->a : MED_EARLY_SHR; break;
            case PO_SFD:     x->sfd_capture = o->on; break;
            case PO_BREAK:   x->has_break = o->on; if (o->on) x->break_db = o->a; break;
            case PO_ADJ:
                // The next channel only (the table's first entry)
                if (!x->n_adj)
                    x->n_adj = 1;
                x->adj_on[0] = o->on;
                if (o->on)
                    x->adj[0] = o->a;
                break;
            case PO_ADJ_TABLE:
                x->n_adj = o->n;
                memcpy(x->adj, o->t, sizeof(o->t));
                memcpy(x->adj_on, o->t_on, sizeof(o->t_on));
                break;
            case PO_MODEL:
                x->smooth = o->on;
                if (o->on && !x->presync_given) {
                    x->presync = true;
                    x->pre_x0 = med_smooth_fit[p].pre_x0;
                    x->pre_b = med_smooth_fit[p].pre_b;
                }
                if (!o->on && !x->presync_given)
                    x->presync = false;
                break;
            case PO_SMOOTH: {
                float *f[] = { &x->cap_m50, &x->cap_s, &x->brk_s, &x->early_c, &x->early_slope, &x->edge_w,
                               &x->sfd_rise, &x->sfd_fall };

                *f[o->n] = o->a;
                break;
            }
            case PO_PRESYNC:
                x->presync = o->on;
                x->presync_given = true;
                if (o->on) {
                    x->pre_x0 = o->a;
                    x->pre_b = o->b;
                }
                break;
            }
        }
    }
}

// The profile of a data rate (§2.1)
static uint8_t med_phy_of(uint32_t rate_bps)
{
    return rate_bps == 50000 ? MED_PHY_1A : rate_bps == 150000 ? MED_PHY_3 : MED_PHY_OTHER;
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

    // From node positions (docs/MEDIUM.md §11), unless a -g value overrides
    if (prop_modelled(rx, src))
        return prop_link_dbm(rx, src, tx_dbm);
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

    if (intf_any())
        intf_advance(now);
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

// The share of a frame's power of this profile that falls d >= 1 channels
// away (§2.1, §13.2): the table's entry, none beyond the table
static double med_adj_factor(const struct med_phy *x, int d)
{
    if (d <= x->n_adj && x->adj_on[d - 1])
        return pow(10, -x->adj[d - 1] / 10);
    return 0;
}

double med_adjacent_factor(uint32_t rate_bps, int d)
{
    return d ? med_adj_factor(&med.phy[med_phy_of(rate_bps)], d) : 1;
}

// The part of g's power mw that falls on chan (§2.1): all of it on its own
// channel, the adjacent-channel table of g's profile further away
static double med_coupled_mw(const struct med_frame *g, uint16_t chan, double mw)
{
    double f;

    if (g->chan == chan)
        return mw;
    f = med_adj_factor(&med.phy[g->phy], g->chan > chan ? g->chan - chan : chan - g->chan);
    return f ? mw * f : 0;
}

// Sum of the powers at node of the frames on air on chan (and on the
// adjacent channels, attenuated), but skip
static double med_energy_mw(int node, uint16_t chan, const struct med_frame *skip, int *count)
{
    const struct med_rx *rx;
    const struct med_frame *g;
    double sum = 0, mw;

    for (g = med.air; g; g = g->next) {
        if (g == skip || g->src == node)
            continue;
        if (g->chan != chan && !med_coupled_mw(g, chan, 1))
            continue;
        rx = med_find_rx(g, node);
        if (!rx)
            continue;
        mw = med_coupled_mw(g, chan, rx->mw);
        sum += mw;
        if (count)
            (*count)++;
    }
    return sum;
}

// The external interferers' power at node on chan now, mW (§13)
static double med_ext_now(int node, uint16_t chan, uint64_t now)
{
    double fsk, noise;

    if (!intf_any())
        return 0;
    intf_power(node, chan, now, &fsk, &noise);
    return fsk + noise;
}

static void med_log(uint64_t t_end, int src, int dst, uint32_t id, int outcome, float dbm, double sinr,
                    const struct chan_sample *cs, double ext_mw)
{
    char var[64];

    med.outcomes[outcome]++;
    if (!med.log)
        return;
    chan_log_columns(var, sizeof(var), cs);
    // Columns: the 7 of §7, then the link variation's (§12), then, with
    // external interferers, the highest external power (§13.1)
    if (!intf_any())
        fprintf(med.log, "%llu %d %d %u %s %.2f %.2f%s\n", (unsigned long long)t_end, src, dst, id,
                outcome_names[outcome], dbm, sinr, var);
    else if (ext_mw > 0)
        fprintf(med.log, "%llu %d %d %u %s %.2f %.2f%s %.2f\n", (unsigned long long)t_end, src, dst, id,
                outcome_names[outcome], dbm, sinr, var, mw_to_dbm(ext_mw));
    else
        fprintf(med.log, "%llu %d %d %u %s %.2f %.2f%s -\n", (unsigned long long)t_end, src, dst, id,
                outcome_names[outcome], dbm, sinr, var);
}

// The highest external interference over f at node, for the log (§13)
static double med_frame_ext(const struct med_frame *f, int node)
{
    return intf_any() && med.log ? intf_max_mw(node, f->chan, f->t_start, f->t_end) : 0;
}

static void med_add_intf_x(struct med_lock *l, uint64_t start, uint64_t end, double mw, bool noise)
{
    if (l->n_intf == l->cap_intf) {
        l->cap_intf = l->cap_intf ? l->cap_intf * 2 : 4;
        l->intf = realloc(l->intf, l->cap_intf * sizeof(*l->intf));
        FATAL_ON(!l->intf, 1, "realloc: %m");
    }
    l->intf[l->n_intf].start = start;
    l->intf[l->n_intf].end = end;
    l->intf[l->n_intf].mw = mw;
    l->intf[l->n_intf].noise = noise;
    // On air before the received frame's SHR ends: its receiver's estimates
    // were made with it (§13.3)
    l->intf[l->n_intf].pre = start < l->t_shr + l->own_shr_us;
    l->n_intf++;
}

static void med_add_intf(struct med_lock *l, uint64_t start, uint64_t end, double mw)
{
    med_add_intf_x(l, start, end, mw, false);
}

// An external interferer's piece over a lock (§13)
static void med_add_ext(void *ctx, const struct intf_piece *p)
{
    struct med_lock *l = ctx;

    med_add_intf_x(l, p->start, p->end, p->mw, p->noise);
}

static int cmp_u64(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;

    return x < y ? -1 : x > y;
}

// Bit error rate of a frame received at mw with intf of interference (§2):
// the noise curve of the SNR and the interference curve of the SIR, the
// larger of the two; or, with the interference curve "noise" (EM4), the
// noise curve of the SINR. Of intf, i_noise is noise-like (external,
// §13.1): it adds to the noise, not to the SIR; i_main is the part that
// started after the SHR's end: without it, the frames already on air use
// the profile's presync curve if it has one (§13.3).
static double med_ber(const struct med_phy *x, double mw, double intf, double i_noise, double i_main)
{
    double ber, ber_i, fsk;
    bool pre;

    if (chan_ber_custom()) {
        // The layered receiver curve (§12): noise-like external power with
        // the noise, the rest as interference (§13.1)
        fsk = i_noise > 0 ? intf - i_noise : intf;
        pre = x->presync && fsk > 0 && !(i_main > 0);
        return chan_ber(mw, i_noise > 0 ? med.noise_mw + i_noise : med.noise_mw, fsk, x->int_noise,
                        pre ? x->pre_x0 : x->int_x0, pre ? x->pre_b : x->int_b);
    }
    if (x->int_noise) {
        ber = 0.5 * exp(-0.5 * mw_to_dbm(mw / (med.noise_mw + intf)));
    } else {
        ber = 0.5 * exp(-0.5 * mw_to_dbm(mw / (med.noise_mw + i_noise)));
        fsk = i_noise > 0 ? intf - i_noise : intf;
        if (fsk > 0) {
            pre = x->presync && !(i_main > 0);
            ber_i = 0.5 * exp(-(mw_to_dbm(mw / fsk) - (pre ? x->pre_x0 : x->int_x0)) / (pre ? x->pre_b : x->int_b));
            if (ber_i > ber)
                ber = ber_i;
        }
    }
    return ber > 0.5 ? 0.5 : ber;
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
    const struct med_phy *x = &med.phy[l->phy];
    double logp = 0, intf, i_noise, i_main, sinr, ber, bits;
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
        intf = i_noise = i_main = 0;
        for (i = 0; i < l->n_intf; i++)
            if (l->intf[i].start <= b[k] && l->intf[i].end > b[k]) {
                intf += l->intf[i].mw;
                if (l->intf[i].noise)
                    i_noise += l->intf[i].mw;
                else if (!l->intf[i].pre)
                    i_main += l->intf[i].mw;
            }
        sinr = mw_to_dbm(l->mw / (med.noise_mw + intf));
        if (sinr < *sinr_min)
            *sinr_min = sinr;
        ber = med_ber(x, l->mw, intf, i_noise, i_main);
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
    med_log(l->t_end, l->src, l->node, l->frame_id, outcome, l->dbm, sinr, &l->cs, l->ext_mw);
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
    med_phy_setup();
    chan_start(seed);
    for (int p = 0; p < MED_PHYS; p++)
        if (chan_combine_given() && med.phy[p].int_noise)
            WARN("medium: PHY %s: interference counts as noise (em4 or --medium-int-curve=noise): "
                 "--medium-combine does not apply to it", phy_names[p]);
    INFO("medium: central, model %s, lock threshold %.1f dB%s", med.em4 ? "em4" : "fg25", med.lock_snr,
         med.has_tx_ref ? "" : ", link values are the received power");
    for (int p = 0; p < MED_PHYS; p++) {
        const struct med_phy *x = &med.phy[p];
        char early[16], intf[48], brk[16], adj[16];

        if (x->early_bits == MED_EARLY_SHR)
            snprintf(early, sizeof(early), "SHR");
        else
            snprintf(early, sizeof(early), "%d bits", x->early_bits);
        if (x->int_noise)
            snprintf(intf, sizeof(intf), "as noise");
        else
            snprintf(intf, sizeof(intf), "x0 %.2f dB, b %.2f dB", x->int_x0, x->int_b);
        snprintf(brk, sizeof(brk), x->has_break ? "%.1f dB" : "off", x->break_db);
        snprintf(adj, sizeof(adj), x->n_adj && x->adj_on[0] ? "%.1f dB" : "off", x->adj[0]);
        INFO("medium: PHY %s: interference %s, capture %.1f dB in the first %s%s, break %s, adjacent channel %s",
             phy_names[p], intf, x->capture_db, early, x->sfd_capture ? " and the SFD" : "", brk, adj);
        // §13: only when set
        if (x->n_adj > 1) {
            char tab[16 * MED_ADJ_MAX] = "", one[16];

            for (int k = 0; k < x->n_adj; k++) {
                snprintf(one, sizeof(one), x->adj_on[k] ? "%s%.1f" : "%soff", k ? "/" : "", x->adj[k]);
                strcat(tab, one);
            }
            INFO("medium: PHY %s: adjacent-channel table (dB, 1 to %d channels away): %s", phy_names[p], x->n_adj, tab);
        }
        if (x->smooth)
            INFO("medium: PHY %s: smooth capture: margin 50 %% at %.1f dB, scale %.2f dB; break scale %.2f dB; "
                 "early window %.1f bits at 5 dB, %+.1f bits/dB; SFD window %+.1f/%+.1f bits; edges %.1f bits",
                 phy_names[p], x->cap_m50, x->cap_s, x->brk_s, x->early_c, x->early_slope, x->sfd_rise, x->sfd_fall,
                 x->edge_w);
        if (x->presync)
            INFO("medium: PHY %s: interference already on air at the SHR's end: x0 %.2f dB, b %.2f dB", phy_names[p],
                 x->pre_x0, x->pre_b);
    }
    intf_start(seed);
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
    f->phy = med_phy_of(hdr->rate_bps);
    f->own_shr_us = f->shr_us - preamble_us;
    f->own_pre_us = f->own_shr_us > sfd_us ? f->own_shr_us - sfd_us : 0;
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
    // Link variation (§12), over the mean from -g or the positions; an edge
    // without a value (a perfect link) does not vary
    chan_apply(node, f->src, f->t_start, f->chan, f->rate_bps,
               !prop_modelled(node, f->src) && sim_link(med.links, node, f->src)->rssi == 0, &rx->dbm, &rx->cs);
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
    l->phy = f->phy;
    l->t_shr = f->t_bits;
    l->own_shr_us = f->own_shr_us;
    l->own_pre_us = f->own_pre_us;
    l->rate_bps = f->rate_bps;
    l->refs = 1;
    // With the measured interference curve, the counted bits are those after
    // the SHR (PHR to FCS), as in the measurement (§2): interference during
    // the SHR is the capture rules' business
    if (!med.phy[f->phy].int_noise && f->t_bits + f->own_shr_us < f->t_end)
        l->t_bits = f->t_bits + f->own_shr_us;
    l->bits = f->bits;
    l->dbm = rx->dbm;
    l->mw = rx->mw;
    l->cs = rx->cs;
    l->active = true;
    for (g = med.air; g; g = g->next) {
        if (g == f || g->src == rx->node || !med_coupled_mw(g, f->chan, 1))
            continue;
        irx = med_find_rx(g, rx->node);
        if (irx)
            med_add_intf(l, g->t_start, g->t_end, med_coupled_mw(g, f->chan, irx->mw));
    }
    // External interferers over the frame (§13)
    if (intf_any()) {
        intf_each(rx->node, f->chan, f->t_start, f->t_end, med_add_ext, l);
        l->ext_mw = intf_max_mw(rx->node, f->chan, f->t_start, f->t_end);
    }
    med.locks[rx->node] = l;
    return l;
}

// Where the receiver of l is in its search for l's sync word at now (§3):
// capture by a stronger frame possible (the early window, a long "xl"
// preamble, the SFD where the profile says so), searching (a stronger frame
// breaks the search), or synchronised (no capture)
enum { MED_SYNC_CAPTURE, MED_SYNC_SEARCH, MED_SYNC_LOCKED };
enum { MED_ACT_NONE, MED_ACT_CAPTURE, MED_ACT_BREAK };
static int med_sync_phase(const struct med_lock *l, uint64_t now)
{
    const struct med_phy *x = &med.phy[l->phy];
    uint64_t d;

    if (now < l->t_shr)
        return MED_SYNC_CAPTURE;        // in an "xl" preamble
    d = now - l->t_shr;
    if (d >= l->own_shr_us)
        return MED_SYNC_LOCKED;
    if (x->early_bits == MED_EARLY_SHR)
        return MED_SYNC_CAPTURE;
    if (d < (uint64_t)x->early_bits * 1000000 / l->rate_bps)
        return MED_SYNC_CAPTURE;
    if (x->sfd_capture && d >= l->own_pre_us)
        return MED_SYNC_CAPTURE;
    return MED_SYNC_SEARCH;
}

// The receiver's stream of the smooth capture model (§13.3)
static double med_uniform_c(int node)
{
    if (!med.rng_c_set[node]) {
        med.rng_c[node] = mix64(med.seed + mix64((uint64_t)node + 1) + MED_RNG_CAPTURE_K);
        med.rng_c_set[node] = true;
    }
    return (mix64(med.rng_c[node] += 0x9e3779b97f4a7c15ULL) >> 11) * 0x1.0p-53;
}

static double med_logistic(double x)
{
    return 1 / (1 + exp(-x));
}

// --medium-capture-model=smooth (§13.3): a frame margin dB stronger than
// the frame l holds starts now. With d the time into l's own SHR in bits,
// Tp and Tshr its preamble and SHR in bits:
//   weight w(d) = max(early, sfd): early = 1 - logistic((d - E) / w),
//     E = early + slope max(0, margin - 5) within [0, Tp]; sfd (where the profile
//     captures during the SFD) = logistic((d - Tp - rise) / w)
//     (1 - logistic((d - Tshr - fall) / w)); during an "xl" preamble, 1;
//   P(capture) = w(d) logistic((margin - m50) / s);
//   P(break) = (1 - w(d)) logistic((margin - break) / brk_s) while d < Tshr.
// One draw from the receiver's capture stream when the outcome is not
// certain.
static int med_smooth_capture(const struct med_lock *l, const struct med_phy *x, double margin, uint64_t now)
{
    double d, tp, tshr, e, w, early, sfd, p_c, p_b = 0, u;

    if (now < l->t_shr) {
        w = 1;
        d = 0;
        tshr = 1;
    } else {
        d = (double)(now - l->t_shr) * l->rate_bps / 1e6;
        tp = (double)l->own_pre_us * l->rate_bps / 1e6;
        tshr = (double)l->own_shr_us * l->rate_bps / 1e6;
        // Past the windows' last edge: synchronised, no capture, no break
        if (d >= tshr + (x->sfd_capture ? x->sfd_fall : 0) + 8 * x->edge_w)
            return MED_ACT_NONE;
        e = x->early_c + x->early_slope * (margin > 5 ? margin - 5 : 0);
        if (e < 0)
            e = 0;
        if (e > tp)
            e = tp;
        early = x->early_bits == MED_EARLY_SHR ? (d < tshr ? 1 : 0) : 1 - med_logistic((d - e) / x->edge_w);
        sfd = x->sfd_capture ? med_logistic((d - tp - x->sfd_rise) / x->edge_w) *
                               (1 - med_logistic((d - tshr - x->sfd_fall) / x->edge_w)) : 0;
        w = early > sfd ? early : sfd;
    }
    p_c = w * med_logistic((margin - x->cap_m50) / x->cap_s);
    if (x->has_break && d < tshr && now >= l->t_shr)
        p_b = (1 - w) * med_logistic((margin - x->break_db) / x->brk_s);
    if (p_c < 1e-9 && p_b < 1e-9)
        return MED_ACT_NONE;
    if (p_c >= 1)
        return MED_ACT_CAPTURE;
    med.smooth_draws++;
    u = med_uniform_c(l->node);
    return u < p_c ? MED_ACT_CAPTURE : u < p_c + p_b ? MED_ACT_BREAK : MED_ACT_NONE;
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
    snr = rx->dbm - mw_to_dbm(med.noise_mw + med_energy_mw(rx->node, f->chan, f, NULL) + med_ext_now(rx->node, f->chan, now));
    if (l && l->chan == f->chan) {
        const struct med_phy *x = &med.phy[l->phy];
        int act = MED_ACT_NONE;

        if (x->smooth) {
            act = med_smooth_capture(l, x, rx->dbm - l->dbm, now);
        } else {
            switch (med_sync_phase(l, now)) {
            case MED_SYNC_CAPTURE:
                if (rx->dbm >= l->dbm + x->capture_db)
                    act = MED_ACT_CAPTURE;
                break;
            case MED_SYNC_SEARCH:
                if (x->has_break && rx->dbm >= l->dbm + x->break_db)
                    act = MED_ACT_BREAK;
                break;
            }
        }
        switch (act) {
        case MED_ACT_CAPTURE:
            med_end_lock(l, MED_CAPTURED, now);
            return med_lock(f, rx);
        case MED_ACT_BREAK:
            // Neither frame is received: the receiver lost the first sync
            // word and finds no second one (§3)
            med_add_intf(l, f->t_start, f->t_end, rx->mw);
            med_end_lock(l, MED_INTERFERENCE, now);
            l->broken = true;
            l->refs++;
            med.broken++;
            med.last_broken = l;
            med_log(f->t_end, f->src, rx->node, f->id, MED_BUSY, rx->dbm, snr, &rx->cs, med_frame_ext(f, rx->node));
            return NULL;
        }
        med_add_intf(l, f->t_start, f->t_end, rx->mw);
        med_log(f->t_end, f->src, rx->node, f->id, MED_BUSY, rx->dbm, snr, &rx->cs, med_frame_ext(f, rx->node));
        return NULL;
    }
    // A frame on an adjacent channel interferes with the one received
    if (l && l->active && med_coupled_mw(f, l->chan, 1))
        med_add_intf(l, f->t_start, f->t_end, med_coupled_mw(f, l->chan, rx->mw));
    if (l || (state != SIM_RADIO_LISTEN && state != SIM_RADIO_RECEIVE) || med_chan(rx->node) != f->chan) {
        med_log(f->t_end, f->src, rx->node, f->id, MED_NOT_LISTENING, rx->dbm, snr, &rx->cs, med_frame_ext(f, rx->node));
        return NULL;
    }
    if (snr < med.lock_snr) {
        med_log(f->t_end, f->src, rx->node, f->id, MED_BELOW_LOCK, rx->dbm, snr, &rx->cs, med_frame_ext(f, rx->node));
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
    if (--l->refs > 0)
        return;
    free(l->intf);
    free(l);
}

struct med_lock *med_take_broken(void)
{
    struct med_lock *l = med.last_broken;

    med.last_broken = NULL;
    return l;
}

bool med_verdict(struct med_lock *l, uint64_t now, struct sim_verdict *v)
{
    double logp, p_ok, sinr;
    bool ok;
    int outcome;

    if (l->broken && l->refs == 2) {
        // The verdict of a broken sync search, at once (its outcome is
        // logged): lost by interference. The event at the frame's end frees it.
        memset(v, 0, sizeof(*v));
        memcpy(v->magic, "xv", 2);
        v->ok = false;
        v->cause = SIM_RX_INTERFERENCE;
        v->frame_id = l->frame_id;
        v->rx_dbm = l->dbm;
        med_segments(l, now, &sinr);
        v->sinr_db = sinr;
        l->refs--;
        return true;
    }
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
    med_log(l->t_end, l->src, l->node, l->frame_id, outcome, l->dbm, sinr, &l->cs, l->ext_mw);
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
    if (intf_any()) {
        double ext = med_ext_now(node, chan, now);

        if (ext > 0) {
            mw += ext;
            med.cca_ext++;
        }
    }
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
        ber = chan_layered() ? chan_noise_ber(sinr) : 0.5 * exp(-0.5 * sinr);
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

static struct sn_heard *sn_push(struct sn_sniff *x)
{
    if (x->n == x->cap) {
        x->cap = x->cap ? x->cap * 2 : 8;
        x->h = realloc(x->h, x->cap * sizeof(*x->h));
        FATAL_ON(!x->h, 1, "realloc: %m");
    }
    return &x->h[x->n++];
}

static void sn_add(struct sn_sniff *x, const struct med_frame *f, const struct med_rx *rx)
{
    struct sn_heard *h = sn_push(x);

    h->cand = true;
    h->adj = false;
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

// A frame on another channel whose power leaks onto the sniff's channel
// (--sniff-adjacent, §13.4): interference and energy, and with "detect" a
// candidate at the power that leaks
static void sn_add_adj(struct sn_sniff *x, const struct med_frame *f, const struct med_rx *rx)
{
    struct sn_heard *h;
    double mw = med_coupled_mw(f, x->chan, rx->mw);

    if (!(mw > 0))
        return;
    sn_add(x, f, rx);
    h = &x->h[x->n - 1];
    h->adj = true;
    h->mw = mw;
    h->dbm = mw_to_dbm(mw);
    h->cand = sn.adjacent == SN_ADJ_DETECT;
    if (!h->cand)
        h->next = 0;
}

// An external interferer's on-interval during a sniff (§13.4):
// interference and energy, and, for an FSK interferer, a candidate during
// the preamble it sends at the start of each burst (frame_id 0)
static void sn_add_ext(void *ctx, const struct intf_piece *p)
{
    struct sn_sniff *x = ctx;
    struct sn_heard *h = sn_push(x);

    h->id = 0;
    h->src = SN_SRC_NONE;
    h->start = p->start;
    h->end = p->end;
    h->pre_end = p->pre_end;
    h->cand = !p->noise && (sn.frame_mode || p->pre_end > p->start);
    h->adj = false;
    h->det_end = sn.frame_mode ? p->end : p->pre_end;
    h->dwell = ((uint64_t)sn.dwell_bits * 1000000 + p->rate_bps - 1) / p->rate_bps;
    if (!h->dwell)
        h->dwell = 1;
    h->mw = p->mw;
    h->dbm = mw_to_dbm(p->mw);
    if (h->cand)
        sn_first_dwell(x, h);
    else
        h->next = 0;
}

// The false-detection stream of a node (§13.4)
static double sn_uniform_f(int node)
{
    if (!sn.rng_f_set[node]) {
        sn.rng_f[node] = mix64(med.seed + mix64((uint64_t)node + 1) + SN_RNG_FALSE_K);
        sn.rng_f_set[node] = true;
    }
    return (mix64(sn.rng_f[node] += 0x9e3779b97f4a7c15ULL) >> 11) * 0x1.0p-53;
}

// The next energy check of x not done yet (§13.4): an averaging time after
// the window's start or after the start of something heard in it
static uint64_t sn_energy_next(const struct sn_sniff *x)
{
    uint64_t best = MED_SNIFF_NONE, c;
    int i;

    if (!sn.has_energy)
        return MED_SNIFF_NONE;
    c = x->t0 + sn.energy_us;
    if (c > x->e_done && c <= x->t1)
        best = c;
    for (i = 0; i < x->n; i++) {
        if (x->h[i].start < x->t0)
            continue;
        c = x->h[i].start + sn.energy_us;
        if (c > x->e_done && c <= x->t1 && c < best)
            best = c;
    }
    return best;
}

// Energy at the sniffing node at t: the noise and everything heard, mW
static double sn_energy_at(const struct sn_sniff *x, uint64_t t)
{
    double mw = med.noise_mw;
    int i;

    for (i = 0; i < x->n; i++)
        if (x->h[i].start <= t && x->h[i].end > t)
            mw += x->h[i].mw;
    return mw;
}

// Lowest energy over [from, to): at from, or just after something ends
static double sn_energy_min(const struct sn_sniff *x, uint64_t from, uint64_t to)
{
    double min = sn_energy_at(x, from), e;
    int i;

    for (i = 0; i < x->n; i++)
        if (x->h[i].end > from && x->h[i].end < to) {
            e = sn_energy_at(x, x->h[i].end);
            if (e < min)
                min = e;
        }
    return min;
}

static uint64_t sn_next(const struct sn_sniff *x)
{
    uint64_t next = x->t1, e;
    int i;

    if (x->on_pending && x->t_on < next)
        next = x->t_on;
    for (i = 0; i < x->n; i++)
        if (x->h[i].next && x->h[i].next < next)
            next = x->h[i].next;
    e = sn_energy_next(x);
    if (e < next)
        next = e;
    if (x->f_time < next)
        next = x->f_time;
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
    x->e_done = 0;
    x->f_time = MED_SNIFF_NONE;
    for (f = med.air; f; f = f->next) {
        if (f->chan != x->chan || f->src == node) {
            // §13.4: a frame on another channel, where its power leaks
            if (sn.adjacent && f->src != node && f->chan != x->chan && med_coupled_mw(f, x->chan, 1) &&
                (rx = med_find_rx(f, node)))
                sn_add_adj(x, f, rx);
            continue;
        }
        rx = med_find_rx(f, node);
        if (rx)
            sn_add(x, f, rx);
    }
    // External interferers in the window (§13.4)
    if (intf_any())
        intf_each(node, x->chan, now, x->t1 + 1, sn_add_ext, x);    // + 1: on air at t1 counts (end energy)
    // A false detection on noise (§13.4): its time, drawn now
    if (sn.false_p > 0 && x->t1 > x->t0) {
        double u = sn_uniform_f(node), t;

        if (sn.false_rate) {
            t = -log1p(-u) / sn.false_p * 1e6;
            if (t < (double)(x->t1 - x->t0))
                x->f_time = x->t0 + (t < 1 ? 1 : llround(t));
        } else if (u < sn.false_p) {
            x->f_time = x->t0 + 1 + (uint64_t)(sn_uniform_f(node) * (x->t1 - x->t0));
        }
        if (x->f_time != MED_SNIFF_NONE && x->f_time > x->t1)
            x->f_time = x->t1;
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

    uint64_t nx;

    if (!x || !x->active || f->t_start >= x->t1)
        return MED_SNIFF_NONE;
    if (f->chan != x->chan) {
        // §13.4: a frame on another channel, where its power leaks
        if (!sn.adjacent || !med_coupled_mw(f, x->chan, 1))
            return MED_SNIFF_NONE;
        sn_add_adj(x, f, rx);
    } else {
        sn_add(x, f, rx);
    }
    h = &x->h[x->n - 1];
    *serial = x->serial;
    nx = h->next;
    // An energy check an averaging time after its start (§13.4)
    if (sn.has_energy && h->start >= x->t0 && h->start + sn.energy_us <= x->t1 &&
        h->start + sn.energy_us > x->e_done && (!nx || h->start + sn.energy_us < nx))
        nx = h->start + sn.energy_us;
    // One chain of check events per sniff: a new event only if it comes
    // before the earliest one queued (a later one finds this dwell itself)
    if (!nx || nx >= x->queued)
        return MED_SNIFF_NONE;
    x->queued = nx;
    return nx;
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
    snr = rx->dbm - mw_to_dbm(med.noise_mw + med_energy_mw(node, f->chan, f, NULL) + med_ext_now(node, f->chan, now));
    if (snr < med.lock_snr)
        return NULL;
    l = med_lock(f, rx);
    if (l->t_bits < now)
        l->t_bits = now;
    sn.locks++;
    return l;
}

// A detection at t of h (NULL: of no emulated frame, §13.4): the result,
// and the lock onto h's frame if the request asks for it and the frame can
// still be received (§10.3). Only a frame on the sniff's own channel locks.
static void sn_detected(int node, struct sn_sniff *x, struct sn_heard *h, uint64_t t, uint64_t now, float rssi,
                        double sinr, struct sim_sniff_result *res, struct med_lock **lock, const void **msg, int *len,
                        float *rx_dbm)
{
    const struct med_lock *l = med.locks[node];
    const struct med_frame *f;
    bool already;

    res->detected = 1;
    res->frame_id = h ? h->id : 0;
    res->src = h ? h->src : SN_SRC_NONE;
    res->t_us = t;
    res->rssi_dbm = rssi;
    res->sinr_db = sinr;
    if (h && h->id && !h->adj && (x->flags & SIM_SNIFF_LOCK)) {
        *lock = sn_lock(node, h, now, &already);
        res->locked = *lock ? SIM_SNIFF_LOCKED : already ? SIM_SNIFF_RECEIVING : 0;
        if (*lock) {
            for (f = med.air; f->id != h->id; f = f->next)
                ;
            *msg = f->msg;
            *len = f->len;
            *rx_dbm = h->dbm;
        }
    } else if (h && h->id && l && l->active && l->frame_id == h->id && l->t_end > now) {
        // Without the flag too: the receiver already receives it
        res->locked = SIM_SNIFF_RECEIVING;
    }
    sn_end(node, SN_DETECTED, now, res);
}

bool med_sniff_check(int node, uint32_t serial, uint64_t now, struct sim_sniff_result *res,
                     struct med_lock **lock, const void **msg, int *len, float *rx_dbm, uint64_t *next)
{
    struct sn_sniff *x = sn.s[node];
    struct sn_heard *h, *best;
    const struct med_lock *l;
    double sinr, p;
    bool detected;
    uint64_t t_e, t_f;
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
        // §13.4: the energy detector's next check and a false detection,
        // after the dwells that end at the same time
        t_e = sn_energy_next(x);
        t_f = x->f_time;
        if (t_e > now)
            t_e = MED_SNIFF_NONE;
        if (t_f > now)
            t_f = MED_SNIFF_NONE;
        if (!best && t_e == MED_SNIFF_NONE && t_f == MED_SNIFF_NONE)
            break;
        if (!best || t_e < best->next || t_f < best->next) {
            if (t_e <= t_f) {
                if (mw_to_dbm(sn_energy_min(x, t_e - sn.energy_us, t_e)) < sn.energy) {
                    x->e_done = t_e;
                    continue;
                }
                // The energy stayed above the threshold: detected, on
                // behalf of the strongest frame on air on the channel
                best = NULL;
                for (i = 0; i < x->n; i++) {
                    h = &x->h[i];
                    if (h->id && !h->adj && h->start <= t_e && h->end > t_e && (!best || h->mw > best->mw))
                        best = h;
                }
                sn.det_energy++;
                sn_detected(node, x, best, t_e, now, best ? best->dbm : mw_to_dbm(sn_energy_at(x, t_e)),
                            best ? sn_sinr_min(x, best, t_e - sn.energy_us, t_e) : 0, res, lock, msg, len, rx_dbm);
            } else {
                // A receiver synchronised on a frame does not trigger on noise
                l = med.locks[node];
                if (l && l->active && l->t_start <= t_f && l->t_end > t_f) {
                    x->f_time = MED_SNIFF_NONE;
                    continue;
                }
                sn.det_false++;
                sn_detected(node, x, NULL, t_f, now, mw_to_dbm(sn_energy_at(x, t_f)), 0, res, lock, msg, len,
                            rx_dbm);
            }
            return true;
        }
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
            if (!h->id)
                sn.det_ext++;
            else if (h->adj)
                sn.det_adj++;
            sn_detected(node, x, h, h->next, now, h->dbm, sinr, res, lock, msg, len, rx_dbm);
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
    INFO("medium: %llu sync searches broken by a stronger frame (lost by interference)",
         (unsigned long long)med.broken);
    INFO("medium: %llu CCA queries, %llu with a frame on air", (unsigned long long)med.cca_queries,
         (unsigned long long)med.cca_energy);
    if (sn.next_serial) {
        INFO("medium: %u sniffs, outcomes:", sn.next_serial);
        for (i = 0; i < SN_OUTCOMES; i++)
            INFO("medium:   sniff %-8s %llu", sn_outcome_names[i], (unsigned long long)sn.outcomes[i]);
        INFO("medium: %llu locks made by a sniff, %llu detection draws", (unsigned long long)sn.locks,
             (unsigned long long)sn.draws);
        if (sn.has_energy || sn.false_p > 0 || sn.adjacent || intf_any())
            INFO("medium: detections: %llu by energy, %llu false (noise), %llu of an external interferer, "
                 "%llu of a frame on another channel", (unsigned long long)sn.det_energy,
                 (unsigned long long)sn.det_false, (unsigned long long)sn.det_ext, (unsigned long long)sn.det_adj);
    }
    if (intf_any())
        INFO("medium: an external interferer on at %llu of the channel-energy queries", (unsigned long long)med.cca_ext);
    if (med.smooth_draws)
        INFO("medium: %llu draws of the smooth capture model", (unsigned long long)med.smooth_draws);
    intf_finish();
    if (med.log)
        fclose(med.log);
    med.log = NULL;
    if (sn.log)
        fclose(sn.log);
    sn.log = NULL;
    chan_finish();
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
