/* SPDX-License-Identifier: Apache-2.0
 *
 * Power ledger of the conductor: see sim_power.h and docs/POWER_MODEL.md.
 *
 * Units: time in µs (double: transitions are fractions of a µs), current in
 * mA, charge in µC (mA × µs / 1000).
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "common/log.h"
#include "ns_port/power.h"
#include "sim_power.h"

/* ---- profiles */

#define PWR_TX_LEVELS_MAX 16

enum { MCU_ACTIVE, MCU_SLEEP, MCU_DEEP, MCU_STATES };
enum { MT_SLEEP_ENTER, MT_SLEEP_WAKE, MT_DEEP_ENTER, MT_DEEP_WAKE, MT_COUNT };

static const char *const mcu_names[MCU_STATES] = { "active", "sleep", "deep_sleep" };
static const char *const mt_names[MT_COUNT] = { "sleep_enter", "sleep_wake", "deep_enter", "deep_wake" };
static const char *const radio_names[SIM_RADIO_STATES] = { "off", "idle", "listen", "receive", "tx" };
static const char *const cause_names[NSIM_CAUSE_COUNT] = { "tick", "protocol", "mac", "fhss", "radio", "other" };
// PHY modes (sim_proto.h, SIM_PHY_MODE_*), as wsbrd.conf's "mode" names them
static const char *const mode_names[SIM_PHY_MODES] = { "", "1a", "1b", "2a", "2b", "3", "4a", "4b", "5" };

struct pwr_profile {
    char path[512];
    char name[64];
    char kind[32];
    double supply_v;
    // [mcu]
    double clock_mhz;
    double i_mcu[MCU_STATES];
    double t_mt[MT_COUNT], i_mt[MT_COUNT];
    double wake_q;                              // µC charged per wake, on top of the cycles
    // [cycles]
    double cyc_wake, cyc_cause[NSIM_CAUSE_COUNT], cyc_event, cyc_frame_in, cyc_rx_frame, cyc_tx_frame;
    // [radio]
    double i_radio[SIM_RADIO_STATES];          // the TX entry is unused: tx_ma
    double i_cca, t_cca;
    double idle_holds;                          // 1: an idle radio keeps the fast clock
    int tx_n;
    double tx_dbm[PWR_TX_LEVELS_MAX], tx_ma[PWR_TX_LEVELS_MAX];
    // [radio_transitions]
    double t_rt[SIM_RADIO_STATES][SIM_RADIO_STATES], i_rt[SIM_RADIO_STATES][SIM_RADIO_STATES];
    struct ns_port_sleep_rule rule;
    // Per PHY mode (docs/POWER_MODEL.md §6.2): the profile with the values
    // of "key@MODE" lines, NULL for a mode without any. mode: the PHY mode
    // of this copy's values, 0 for the common values (the profile itself).
    int mode;
    struct pwr_profile *by_mode[SIM_PHY_MODES];
    struct pwr_profile *next;
};

// A "key@MODE = value" line, applied once the common values are read
struct pwr_mode_line {
    int mode, line;
    char key[160];
    char val[1024];
    struct pwr_mode_line *next;
};

#define F(field) offsetof(struct pwr_profile, field)
static const struct {
    const char *key;            // "section.key"
    size_t off;
    bool optional;              // 0 when not given
} profile_keys[] = {
    { "profile.supply_v",       F(supply_v) },
    { "mcu.clock_mhz",          F(clock_mhz) },
    { "mcu.active_ma",          F(i_mcu[MCU_ACTIVE]) },
    { "mcu.sleep_ma",           F(i_mcu[MCU_SLEEP]) },
    { "mcu.deep_sleep_ma",      F(i_mcu[MCU_DEEP]) },
    { "mcu.sleep_enter_us",     F(t_mt[MT_SLEEP_ENTER]) },
    { "mcu.sleep_enter_ma",     F(i_mt[MT_SLEEP_ENTER]) },
    { "mcu.sleep_wake_us",      F(t_mt[MT_SLEEP_WAKE]) },
    { "mcu.sleep_wake_ma",      F(i_mt[MT_SLEEP_WAKE]) },
    { "mcu.deep_enter_us",      F(t_mt[MT_DEEP_ENTER]) },
    { "mcu.deep_enter_ma",      F(i_mt[MT_DEEP_ENTER]) },
    { "mcu.deep_wake_us",       F(t_mt[MT_DEEP_WAKE]) },
    { "mcu.deep_wake_ma",       F(i_mt[MT_DEEP_WAKE]) },
    { "mcu.wake_charge_uc",     F(wake_q), true },
    { "cycles.wake",            F(cyc_wake) },
    { "cycles.tick",            F(cyc_cause[NSIM_CAUSE_TICK]) },
    { "cycles.protocol",        F(cyc_cause[NSIM_CAUSE_PROTOCOL]) },
    { "cycles.mac",             F(cyc_cause[NSIM_CAUSE_MAC]) },
    { "cycles.fhss",            F(cyc_cause[NSIM_CAUSE_FHSS]) },
    { "cycles.radio",           F(cyc_cause[NSIM_CAUSE_RADIO]) },
    { "cycles.other",           F(cyc_cause[NSIM_CAUSE_OTHER]) },
    { "cycles.event",           F(cyc_event) },
    { "cycles.frame_in",        F(cyc_frame_in) },
    { "cycles.rx_frame",        F(cyc_rx_frame) },
    { "cycles.tx_frame",        F(cyc_tx_frame) },
    { "radio.off_ma",           F(i_radio[SIM_RADIO_OFF]) },
    { "radio.idle_ma",          F(i_radio[SIM_RADIO_IDLE]) },
    { "radio.listen_ma",        F(i_radio[SIM_RADIO_LISTEN]) },
    { "radio.receive_ma",       F(i_radio[SIM_RADIO_RECEIVE]) },
    { "radio.cca_ma",           F(i_cca) },
    { "radio.cca_us",           F(t_cca) },
    { "radio.idle_holds_clock", F(idle_holds) },
};
#undef F

static char *trim(char *s)
{
    char *e;

    while (isspace((unsigned char)*s))
        s++;
    e = s + strlen(s);
    while (e > s && isspace((unsigned char)e[-1]))
        *--e = '\0';
    return s;
}

static double parse_number(const char *path, int line, const char *key, const char *str)
{
    char *end;
    double d;

    errno = 0;
    d = strtod(str, &end);
    FATAL_ON(end == str || *trim(end) || errno || !isfinite(d) || d < 0, 1,
             "%s:%d: %s: expected a number >= 0, got \"%s\"", path, line, key, str);
    return d;
}

static int radio_state_by_name(const char *name, size_t len)
{
    int i;

    for (i = 0; i < SIM_RADIO_STATES; i++)
        if (strlen(radio_names[i]) == len && !strncmp(radio_names[i], name, len))
            return i;
    return -1;
}

// "dBm:mA dBm:mA ...", in increasing dBm
static void parse_tx_levels(struct pwr_profile *p, int line, char *val)
{
    char *tok, *save, *colon, *end;
    double dbm;

    for (tok = strtok_r(val, " \t,", &save); tok; tok = strtok_r(NULL, " \t,", &save)) {
        FATAL_ON(p->tx_n >= PWR_TX_LEVELS_MAX, 1, "%s:%d: radio.tx_ma: more than %d levels",
                 p->path, line, PWR_TX_LEVELS_MAX);
        colon = strchr(tok, ':');
        FATAL_ON(!colon, 1, "%s:%d: radio.tx_ma: expected dBm:mA, got \"%s\"", p->path, line, tok);
        *colon = '\0';
        dbm = strtod(tok, &end);
        FATAL_ON(end == tok || *end, 1, "%s:%d: radio.tx_ma: bad dBm \"%s\"", p->path, line, tok);
        FATAL_ON(p->tx_n && dbm <= p->tx_dbm[p->tx_n - 1], 1,
                 "%s:%d: radio.tx_ma: levels must be in increasing dBm", p->path, line);
        p->tx_dbm[p->tx_n] = dbm;
        p->tx_ma[p->tx_n] = parse_number(p->path, line, "radio.tx_ma", colon + 1);
        p->tx_n++;
    }
}

// "<from>_<to> = <µs> <mA>"
static void parse_transition(struct pwr_profile *p, int line, const char *key, char *val)
{
    const char *sep = strchr(key, '_'), *us;
    char *ma;
    int from, to;

    from = sep ? radio_state_by_name(key, sep - key) : -1;
    to = sep ? radio_state_by_name(sep + 1, strlen(sep + 1)) : -1;
    FATAL_ON(from < 0 || to < 0 || from == to, 1,
             "%s:%d: radio_transitions: unknown transition \"%s\" (states: off idle listen receive tx)",
             p->path, line, key);
    us = strtok_r(val, " \t", &ma);
    FATAL_ON(!us || !*trim(ma), 1, "%s:%d: %s: expected \"<us> <mA>\"", p->path, line, key);
    p->t_rt[from][to] = parse_number(p->path, line, key, us);
    p->i_rt[from][to] = parse_number(p->path, line, key, trim(ma));
}

static void parse_string(char *dst, size_t size, const char *path, int line, const char *val)
{
    FATAL_ON(strlen(val) >= size, 1, "%s:%d: value too long", path, line);
    strcpy(dst, val);
}

#define PWR_NKEYS (sizeof(profile_keys) / sizeof(profile_keys[0]))

// A mode name ("1a", "3"; any case) to its SIM_PHY_MODE_*, -1 if none
static int mode_by_name(const char *name)
{
    int i;

    for (i = 1; i < SIM_PHY_MODES; i++)
        if (!strcasecmp(name, mode_names[i]))
            return i;
    return -1;
}

// One "section.key = value" of the [mcu], [cycles], [radio] or
// [radio_transitions] sections. seen[PWR_NKEYS] stands for radio.tx_ma.
static void profile_set(struct pwr_profile *p, bool *seen, int line, const char *key, char *val)
{
    size_t i;

    if (!strncmp(key, "radio_transitions.", strlen("radio_transitions."))) {
        parse_transition(p, line, key + strlen("radio_transitions."), val);
        return;
    }
    if (!strcmp(key, "radio.tx_ma")) {
        FATAL_ON(seen[PWR_NKEYS], 1, "%s:%d: %s given twice", p->path, line, key);
        seen[PWR_NKEYS] = true;
        p->tx_n = 0;
        parse_tx_levels(p, line, val);
        return;
    }
    for (i = 0; i < PWR_NKEYS; i++)
        if (!strcmp(key, profile_keys[i].key))
            break;
    FATAL_ON(i == PWR_NKEYS, 1, "%s:%d: unknown key %s", p->path, line, key);
    FATAL_ON(seen[i], 1, "%s:%d: %s given twice", p->path, line, key);
    seen[i] = true;
    *(double *)((char *)p + profile_keys[i].off) = parse_number(p->path, line, key, val);
}

static void profile_check(struct pwr_profile *p)
{
    const char *path = p->path;

    FATAL_ON(!p->tx_n, 1, "%s: missing radio.tx_ma", path);
    FATAL_ON(p->clock_mhz <= 0 || p->supply_v <= 0, 1, "%s: clock and supply must be > 0", path);
    p->rule.deep_min_us = ns_port_deep_min_us(p->i_mcu[MCU_SLEEP], p->i_mcu[MCU_DEEP],
                                              p->t_mt[MT_DEEP_ENTER], p->i_mt[MT_DEEP_ENTER],
                                              p->t_mt[MT_DEEP_WAKE], p->i_mt[MT_DEEP_WAKE]);
}

// The profile for each PHY mode that has "key@MODE" lines: a copy of the
// common values with that mode's lines applied, in file order.
static void profile_modes(struct pwr_profile *p, struct pwr_mode_line *lines)
{
    struct pwr_mode_line *l;
    struct pwr_profile *q;
    int m;

    for (m = 1; m < SIM_PHY_MODES; m++) {
        bool seen[PWR_NKEYS + 1] = { };

        q = NULL;
        for (l = lines; l; l = l->next) {
            if (l->mode != m)
                continue;
            if (!q) {
                q = malloc(sizeof(*q));
                FATAL_ON(!q, 1, "malloc: %m");
                *q = *p;
                q->mode = m;
                q->next = NULL;
                memset(q->by_mode, 0, sizeof(q->by_mode));
            }
            profile_set(q, seen, l->line, l->key, l->val);
        }
        if (q) {
            profile_check(q);
            p->by_mode[m] = q;
        }
    }
    while (lines) {
        l = lines->next;
        free(lines);
        lines = l;
    }
}

static struct pwr_profile *profile_load(const char *path)
{
    bool seen[PWR_NKEYS + 1] = { };
    char section[64] = "", key[160], buf[1024], *s, *eq, *val, *c, *at;
    struct pwr_profile *p = calloc(1, sizeof(*p));
    struct pwr_mode_line *lines = NULL, **tail = &lines, *l;
    int line = 0, mode;
    size_t i;
    FILE *f;

    FATAL_ON(!p, 1, "calloc: %m");
    FATAL_ON(strlen(path) >= sizeof(p->path), 1, "%s: path too long", path);
    strcpy(p->path, path);
    f = fopen(path, "r");
    FATAL_ON(!f, 1, "power profile %s: %m", path);
    while (fgets(buf, sizeof(buf), f)) {
        line++;
        if ((c = strpbrk(buf, "#;")))
            *c = '\0';
        s = trim(buf);
        if (!*s)
            continue;
        if (*s == '[') {
            c = strchr(s, ']');
            FATAL_ON(!c || c[1], 1, "%s:%d: bad section", path, line);
            *c = '\0';
            parse_string(section, sizeof(section), path, line, trim(s + 1));
            FATAL_ON(!strcmp(section, "link"), 1,
                     "%s:%d: [link]: MCU + RCP profiles need the split programs in virtual time (EM3 step 2)",
                     path, line);
            FATAL_ON(strcmp(section, "profile") && strcmp(section, "mcu") && strcmp(section, "cycles") &&
                     strcmp(section, "radio") && strcmp(section, "radio_transitions"), 1,
                     "%s:%d: unknown section [%s]", path, line, section);
            continue;
        }
        eq = strchr(s, '=');
        FATAL_ON(!eq || !*section, 1, "%s:%d: expected \"key = value\" in a section", path, line);
        *eq = '\0';
        val = trim(eq + 1);
        // "key@MODE": the value for nodes that run this PHY mode
        mode = 0;
        if ((at = strchr(s, '@'))) {
            *at = '\0';
            mode = mode_by_name(trim(at + 1));
            FATAL_ON(mode < 0, 1, "%s:%d: unknown PHY mode \"%s\" (1a 1b 2a 2b 3 4a 4b 5)",
                     path, line, trim(at + 1));
            FATAL_ON(!strcmp(section, "profile"), 1, "%s:%d: [profile] keys do not depend on the PHY mode",
                     path, line);
        }
        FATAL_ON(snprintf(key, sizeof(key), "%s.%s", section, trim(s)) >= sizeof(key), 1,
                 "%s:%d: key too long", path, line);
        if (mode) {
            l = calloc(1, sizeof(*l));
            FATAL_ON(!l, 1, "calloc: %m");
            l->mode = mode;
            l->line = line;
            strcpy(l->key, key);
            strcpy(l->val, val);
            *tail = l;
            tail = &l->next;
            continue;
        }
        if (!strcmp(key, "profile.name")) {
            parse_string(p->name, sizeof(p->name), path, line, val);
            continue;
        }
        if (!strcmp(key, "profile.kind")) {
            parse_string(p->kind, sizeof(p->kind), path, line, val);
            continue;
        }
        profile_set(p, seen, line, key, val);
    }
    fclose(f);
    FATAL_ON(!p->name[0], 1, "%s: missing profile.name", path);
    FATAL_ON(!p->tx_n, 1, "%s: missing radio.tx_ma", path);
    for (i = 0; i < PWR_NKEYS; i++)
        FATAL_ON(!seen[i] && !profile_keys[i].optional, 1, "%s: missing %s", path, profile_keys[i].key);
    if (!strcmp(p->kind, "mcu+rcp"))
        FATAL(1, "%s: MCU + RCP profiles need the split programs in virtual time (EM3 step 2)", path);
    FATAL_ON(strcmp(p->kind, "soc") && strcmp(p->kind, "mcu+transceiver"), 1,
             "%s: profile.kind must be soc or mcu+transceiver, got \"%s\"", path, p->kind);
    profile_check(p);
    profile_modes(p, lines);
    return p;
}

// TX current at a power level: linear between the profile's levels, the
// nearest level outside them (*clamped is set).
static double tx_current(const struct pwr_profile *p, double dbm, bool *clamped)
{
    int i;

    if (dbm <= p->tx_dbm[0]) {
        *clamped = dbm < p->tx_dbm[0];
        return p->tx_ma[0];
    }
    if (dbm >= p->tx_dbm[p->tx_n - 1]) {
        *clamped = dbm > p->tx_dbm[p->tx_n - 1];
        return p->tx_ma[p->tx_n - 1];
    }
    *clamped = false;
    for (i = 1; dbm > p->tx_dbm[i]; i++)
        ;
    return p->tx_ma[i - 1] + (p->tx_ma[i] - p->tx_ma[i - 1]) *
           (dbm - p->tx_dbm[i - 1]) / (p->tx_dbm[i] - p->tx_dbm[i - 1]);
}

/* ---- per-node ledger */

struct pwr_node {
    int id;
    const struct pwr_profile *base;     // the node's profile (common values)
    const struct pwr_profile *p;        // the values in use: base, or its copy for a PHY mode
    int mode_radio;                     // PHY mode the radio reported (0: none yet)
    int mode_user;                      // --power-mode: -1 auto (the radio's), 0 common values, or a mode

    // MCU: active from act_start until busy_until (the modelled work), then
    // asleep at depth until the next wake.
    bool started;
    double act_start, busy_until;
    int depth;                  // MCU_SLEEP or MCU_DEEP, chosen at "xi"
    double mcu_time[MCU_STATES];
    uint64_t mcu_count[MCU_STATES];
    double mt_time[MT_COUNT];
    uint64_t mt_count[MT_COUNT];
    uint64_t overlaps;          // wakes while the modelled work was not done
    double cycles;

    // The wake in progress (between "xa" and "xi")
    uint64_t w_t;
    unsigned int w_frames, w_rx, w_tx;
    bool w_reported;
    struct nsim_wake w;

    // Wakes
    uint64_t wakes, wakes_unseen, empty_ticks, events, frames_in, frames_unseen;
    uint64_t wakes_with[NSIM_CAUSE_COUNT + 1];  // the last one: a frame the radio took
    uint64_t timer_cbs[NSIM_CAUSE_COUNT];

    // Radio
    int r_state;
    double r_since;
    int8_t r_dbm;
    int r_carve_from, r_carve_to;   // transition to charge at the start of the interval
    double r_carve;
    double r_time[SIM_RADIO_STATES];
    uint64_t r_count[SIM_RADIO_STATES];
    struct {
        int8_t dbm;
        double time;
        uint64_t count;
    } tx[PWR_TX_LEVELS_MAX * 2];
    int tx_n;
    double cca_time;
    uint64_t cca_count;
    double rt_time[SIM_RADIO_STATES][SIM_RADIO_STATES];
    uint64_t rt_count[SIM_RADIO_STATES][SIM_RADIO_STATES];
};

static struct {
    const char *profile_path;
    struct {
        char *spec;
    } node_specs[64];
    int node_specs_n;
    char *mode_specs[64];               // --power-mode=[IDS:]MODE
    int mode_specs_n;
    const char *out_path;
    const char *log_path;
    bool has_cell;
    double cell_mah, cell_v, cell_eff;

    struct pwr_profile *profiles;
    struct pwr_node *nodes[SIM_MAX_NODES];
    int num_nodes;
    int ids[SIM_MAX_NODES];
    FILE *log;
} pwr = {
    .out_path = "power.json",
};

void pwr_set_profile(const char *path)
{
    pwr.profile_path = path;
}

void pwr_add_node_profile(const char *spec)
{
    FATAL_ON(pwr.node_specs_n >= 64, 1, "--power-node: at most 64");
    FATAL_ON(!strchr(spec, ':'), 1, "--power-node: expected IDS:PROFILE, got %s", spec);
    pwr.node_specs[pwr.node_specs_n++].spec = strdup(spec);
}

// "auto" -1, "common" 0, or a mode name
static int mode_choice(const char *name)
{
    int m;

    if (!strcmp(name, "auto"))
        return -1;
    if (!strcmp(name, "common"))
        return 0;
    m = mode_by_name(name);
    FATAL_ON(m < 0, 1, "--power-mode: expected auto, common or a PHY mode (1a 1b 2a 2b 3 4a 4b 5), got %s", name);
    return m;
}

void pwr_set_mode(const char *spec)
{
    const char *colon = strchr(spec, ':');

    FATAL_ON(pwr.mode_specs_n >= 64, 1, "--power-mode: at most 64");
    mode_choice(colon ? colon + 1 : spec);
    pwr.mode_specs[pwr.mode_specs_n++] = strdup(spec);
}

void pwr_set_out(const char *path)
{
    pwr.out_path = path;
}

void pwr_set_log(const char *path)
{
    pwr.log_path = path;
}

void pwr_set_cell(const char *spec)
{
    int n = sscanf(spec, "%lf:%lf:%lf", &pwr.cell_mah, &pwr.cell_v, &pwr.cell_eff);

    FATAL_ON(n < 1 || pwr.cell_mah <= 0 || (n >= 2 && pwr.cell_v <= 0) ||
             (n == 3 && (pwr.cell_eff <= 0 || pwr.cell_eff > 1)), 1,
             "--power-cell: expected MAH[:VOLT[:EFF]], got %s", spec);
    if (n < 2)
        pwr.cell_v = 0;         // no converter: the cell gives the profile's current
    if (n < 3)
        pwr.cell_eff = 1;
    pwr.has_cell = true;
}

bool pwr_enabled(void)
{
    return pwr.profile_path || pwr.node_specs_n;
}

static struct pwr_profile *profile_get(const char *path)
{
    struct pwr_profile *p;

    for (p = pwr.profiles; p; p = p->next)
        if (!strcmp(p->path, path))
            return p;
    p = profile_load(path);
    p->next = pwr.profiles;
    pwr.profiles = p;
    INFO("power: profile %s (%s, %s): deep sleep from %llu us of idle", p->name, p->kind, path,
         (unsigned long long)p->rule.deep_min_us);
    for (int m = 1; m < SIM_PHY_MODES; m++)
        if (p->by_mode[m])
            INFO("power: profile %s: values for PHY mode %s", p->name, mode_names[m]);
    return p;
}

// "3", "2-5", "1,4,7-9"
static bool spec_has_node(const char *ids, size_t len, int node)
{
    char buf[256], *tok, *save, *end;
    long a, b;

    FATAL_ON(len >= sizeof(buf), 1, "--power-node: node list too long");
    memcpy(buf, ids, len);
    buf[len] = '\0';
    for (tok = strtok_r(buf, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
        a = strtol(tok, &end, 0);
        b = a;
        if (*end == '-')
            b = strtol(end + 1, &end, 0);
        FATAL_ON(*end || a < 0 || b < a, 1, "--power-node: bad node list %s", ids);
        if (node >= a && node <= b)
            return true;
    }
    return false;
}

static const struct pwr_profile *profile_of(int node)
{
    const char *path = pwr.profile_path, *spec, *colon;
    int i;

    // The last --power-node that names the node wins
    for (i = 0; i < pwr.node_specs_n; i++) {
        spec = pwr.node_specs[i].spec;
        colon = strchr(spec, ':');
        if (spec_has_node(spec, colon - spec, node))
            path = colon + 1;
    }
    FATAL_ON(!path, 1, "power: no profile for node %d (--power or --power-node)", node);
    return profile_get(path);
}

// The node's --power-mode: the last one that names it (or names no node) wins
static int mode_user_of(int node)
{
    const char *spec, *colon;
    int i, m = -1;

    for (i = 0; i < pwr.mode_specs_n; i++) {
        spec = pwr.mode_specs[i];
        colon = strchr(spec, ':');
        if (!colon)
            m = mode_choice(spec);
        else if (spec_has_node(spec, colon - spec, node))
            m = mode_choice(colon + 1);
    }
    return m;
}

static const struct pwr_profile *profile_for_mode(const struct pwr_profile *base, int mode)
{
    return mode > 0 && base->by_mode[mode] ? base->by_mode[mode] : base;
}

void pwr_start(int num_nodes, const int *nodes)
{
    struct pwr_node *n;
    int i;

    for (i = 0; i < num_nodes; i++) {
        n = calloc(1, sizeof(*n));
        FATAL_ON(!n, 1, "calloc: %m");
        n->id = nodes[i];
        n->base = profile_of(nodes[i]);
        n->mode_user = mode_user_of(nodes[i]);
        n->p = profile_for_mode(n->base, n->mode_user);
        if (n->mode_user > 0 && n->p == n->base)
            INFO("power: node %d: --power-mode=%s, but profile %s has no values for it: common values",
                 n->id, mode_names[n->mode_user], n->base->name);
        n->depth = MCU_SLEEP;
        n->r_state = SIM_RADIO_OFF;
        n->r_carve_from = -1;
        pwr.nodes[nodes[i]] = n;
        pwr.ids[i] = nodes[i];
    }
    pwr.num_nodes = num_nodes;
    if (pwr.log_path) {
        pwr.log = fopen(pwr.log_path, "w");
        FATAL_ON(!pwr.log, 1, "%s: %m", pwr.log_path);
        fprintf(pwr.log, "# start_us end_us node part state [dBm]\n");
    }
}

static void log_interval(const struct pwr_node *n, const char *part, const char *state,
                         double start, double len, int dbm, bool has_dbm)
{
    if (!pwr.log || len <= 0)
        return;
    if (has_dbm)
        fprintf(pwr.log, "%.3f %.3f %d %s %s %d\n", start, start + len, n->id, part, state, dbm);
    else
        fprintf(pwr.log, "%.3f %.3f %d %s %s\n", start, start + len, n->id, part, state);
}

/* ---- MCU */

static void mcu_add(struct pwr_node *n, int state, double start, double len)
{
    if (len <= 0)
        return;
    n->mcu_time[state] += len;
    n->mcu_count[state]++;
    log_interval(n, "mcu", mcu_names[state], start, len, 0, false);
}

static void mt_add(struct pwr_node *n, int kind, double start, double len)
{
    n->mt_count[kind]++;
    n->mt_time[kind] += len;
    log_interval(n, "mcu", mt_names[kind], start, len, 0, false);
}

// A sleep from start to end at the node's depth: its entry, then the sleep,
// then (if woken) the wake. Transitions longer than the sleep are cut.
static void mcu_sleep(struct pwr_node *n, double start, double end, bool woken)
{
    const struct pwr_profile *p = n->p;
    int enter = n->depth == MCU_DEEP ? MT_DEEP_ENTER : MT_SLEEP_ENTER;
    int wake = n->depth == MCU_DEEP ? MT_DEEP_WAKE : MT_SLEEP_WAKE;
    double len = end - start, t_enter, t_wake = 0;

    if (len <= 0)
        return;
    t_enter = fmin(p->t_mt[enter], len);
    if (woken)
        t_wake = fmin(p->t_mt[wake], len - t_enter);
    mt_add(n, enter, start, t_enter);
    mcu_add(n, n->depth, start + t_enter, len - t_enter - t_wake);
    if (woken)
        mt_add(n, wake, end - t_wake, t_wake);
}

/* ---- radio */

// Close the radio's current interval at t. A transition into the state is
// charged at its start, a CCA (cca_us) at its end; both are cut to fit.
static void radio_close(struct pwr_node *n, double t, double cca_us)
{
    double len = t - n->r_since, start = n->r_since, c;
    int i;

    if (n->r_carve_from >= 0) {
        c = fmin(n->r_carve, fmax(len, 0));
        n->rt_time[n->r_carve_from][n->r_carve_to] += c;
        if (c > 0 && pwr.log)
            fprintf(pwr.log, "%.3f %.3f %d radio %s>%s\n", start, start + c, n->id,
                    radio_names[n->r_carve_from], radio_names[n->r_carve_to]);
        start += c;
        len -= c;
        n->r_carve_from = -1;
    }
    c = cca_us > 0 ? fmin(cca_us, fmax(len, 0)) : 0;
    len -= c;
    if (len > 0) {
        if (n->r_state == SIM_RADIO_TX) {
            for (i = 0; i < n->tx_n && n->tx[i].dbm != n->r_dbm; i++)
                ;
            n->tx[i].time += len;
            log_interval(n, "radio", "tx", start, len, n->r_dbm, true);
        } else {
            n->r_time[n->r_state] += len;
            log_interval(n, "radio", radio_names[n->r_state], start, len, 0, false);
        }
    }
    if (cca_us > 0) {
        n->cca_time += c;
        n->cca_count++;
        log_interval(n, "radio", "cca", start + fmax(len, 0), c, 0, false);
    }
    n->r_since = t;
}

void pwr_radio(int node, uint64_t t, const struct sim_radio_report *r)
{
    struct pwr_node *n = pwr.nodes[node];
    int i;

    if (!n)
        return;
    if (r->state >= SIM_RADIO_STATES) {
        WARN("node %d: radio report with state %u, ignored", node, r->state);
        return;
    }
    // The PHY mode the MAC configured: the profile's values for it. Every
    // time and count is charged at the end with the values in use then
    // (the MAC configures its PHY once, at its start).
    if (r->phy_mode && r->phy_mode != n->mode_radio) {
        if (r->phy_mode >= SIM_PHY_MODES) {
            WARN("node %d: radio report with PHY mode %u, ignored", node, r->phy_mode);
        } else {
            if (n->mode_radio)
                WARN("node %d: PHY mode %s, then %s: the whole run is charged with the values of the last",
                     node, mode_names[n->mode_radio], mode_names[r->phy_mode]);
            n->mode_radio = r->phy_mode;
            if (n->mode_user < 0)
                n->p = profile_for_mode(n->base, n->mode_radio);
        }
    }
    if (r->events & SIM_RADIO_EV_CCA)
        radio_close(n, t, n->p->t_cca);
    if (r->state == n->r_state && (r->state != SIM_RADIO_TX || r->tx_dbm == n->r_dbm))
        return;
    radio_close(n, t, 0);
    n->rt_count[n->r_state][r->state]++;
    n->r_carve_from = n->r_state;
    n->r_carve_to = r->state;
    n->r_carve = n->p->t_rt[n->r_state][r->state];
    n->r_state = r->state;
    n->r_count[r->state]++;
    if (r->state == SIM_RADIO_TX) {
        n->r_dbm = r->tx_dbm;
        for (i = 0; i < n->tx_n && n->tx[i].dbm != r->tx_dbm; i++)
            ;
        if (i == n->tx_n) {
            FATAL_ON(n->tx_n >= (int)(sizeof(n->tx) / sizeof(n->tx[0])), 1, "node %d: too many TX powers", node);
            n->tx[n->tx_n++].dbm = r->tx_dbm;
        }
        n->tx[i].count++;
        n->w_tx++;
    } else if (r->state == SIM_RADIO_RECEIVE) {
        n->w_rx++;
    }
}

/* ---- wakes */

void pwr_wake(int node, uint64_t t, unsigned int frames)
{
    struct pwr_node *n = pwr.nodes[node];

    if (!n)
        return;
    n->w_t = t;
    n->w_frames = frames;
    n->w_rx = n->w_tx = 0;
    n->w_reported = false;
    memset(&n->w, 0, sizeof(n->w));
}

void pwr_wake_report(int node, const struct nsim_wake *w)
{
    struct pwr_node *n = pwr.nodes[node];

    if (!n)
        return;
    n->w = *w;
    n->w_reported = true;
}

static bool radio_holds_clock(const struct pwr_node *n)
{
    if (n->r_state == SIM_RADIO_OFF)
        return false;
    if (n->r_state == SIM_RADIO_IDLE)
        return n->p->idle_holds != 0;
    return true;
}

// The whole wake is known at its "xi": account for it.
void pwr_idle(int node, uint64_t t, uint64_t next_us)
{
    struct pwr_node *n = pwr.nodes[node];
    const struct pwr_profile *p;
    bool timers = false, empty;
    double cycles, idle_us;
    int i;

    if (!n)
        return;
    p = n->p;
    for (i = 0; i < NSIM_CAUSE_COUNT; i++)
        timers |= n->w.timers[i] > 0;
    n->frames_in += n->w_frames;
    if (n->started && n->w_frames && !n->w_rx && !n->w_tx && !timers && !n->w.events) {
        // Only frames that the radio did not take (another channel, lost,
        // or busy): a board would not have woken.
        n->wakes_unseen++;
        n->frames_unseen += n->w_frames;
        return;
    }
    if (n->w_frames && !n->w_rx)
        n->frames_unseen += n->w_frames;
    n->wakes++;
    if (!n->started) {
        n->started = true;
        n->act_start = n->busy_until = t;
    } else if (t < n->busy_until) {
        n->overlaps++;
    } else {
        mcu_add(n, MCU_ACTIVE, n->act_start, n->busy_until - n->act_start);
        mcu_sleep(n, n->busy_until, t, true);
        n->act_start = n->busy_until = t;
    }
    // Modelled work of this wake
    cycles = p->cyc_wake + p->cyc_event * n->w.events + p->cyc_frame_in * n->w_frames +
             p->cyc_rx_frame * n->w_rx + p->cyc_tx_frame * n->w_tx;
    for (i = 0; i < NSIM_CAUSE_COUNT; i++) {
        cycles += p->cyc_cause[i] * n->w.timers[i];
        n->timer_cbs[i] += n->w.timers[i];
        if (n->w.timers[i])
            n->wakes_with[i]++;
    }
    if (n->w_rx)
        n->wakes_with[NSIM_CAUSE_COUNT]++;
    n->events += n->w.events;
    n->cycles += cycles;
    n->busy_until += cycles / p->clock_mhz;
    empty = n->w.timers[NSIM_CAUSE_TICK] && !n->w.events && !n->w_rx && !n->w_tx;
    for (i = 0; i < NSIM_CAUSE_COUNT; i++)
        if (i != NSIM_CAUSE_TICK && n->w.timers[i])
            empty = false;
    if (empty)
        n->empty_ticks++;
    // Sleep depth for the idle that follows (include/ns_port/power.h)
    if (next_us == NSIM_NEVER)
        idle_us = INFINITY;
    else
        idle_us = fmax((double)next_us - n->busy_until, 0);
    n->depth = ns_port_sleep_depth(radio_holds_clock(n) ? NS_PORT_HOLD_RADIO : 0,
                                   idle_us >= (double)UINT64_MAX ? UINT64_MAX : (uint64_t)idle_us,
                                   &p->rule) == NS_PORT_SLEEP_DEEP ? MCU_DEEP : MCU_SLEEP;
}

/* ---- end of the run */

struct pwr_totals {
    double mcu_q[MCU_STATES], mt_q[MT_COUNT], r_q[SIM_RADIO_STATES], cca_q, tx_q, rt_q, wake_q;
    double charge, mcu_t, radio_t, tx_t;
    bool tx_clamped;
};

static void node_close(struct pwr_node *n, double t_end)
{
    if (!n->started) {
        n->started = true;      // never ran: asleep from 0
        n->act_start = n->busy_until = 0;
    }
    if (n->busy_until >= t_end) {
        mcu_add(n, MCU_ACTIVE, n->act_start, t_end - n->act_start);
    } else {
        mcu_add(n, MCU_ACTIVE, n->act_start, n->busy_until - n->act_start);
        mcu_sleep(n, n->busy_until, t_end, false);
    }
    radio_close(n, t_end, 0);
}

static void node_totals(const struct pwr_node *n, struct pwr_totals *tt)
{
    const struct pwr_profile *p = n->p;
    bool clamped;
    int i, j;

    memset(tt, 0, sizeof(*tt));
    for (i = 0; i < MCU_STATES; i++) {
        tt->mcu_q[i] = n->mcu_time[i] * p->i_mcu[i] / 1000;
        tt->mcu_t += n->mcu_time[i];
        tt->charge += tt->mcu_q[i];
    }
    for (i = 0; i < MT_COUNT; i++) {
        tt->mt_q[i] = n->mt_time[i] * p->i_mt[i] / 1000;
        tt->mcu_t += n->mt_time[i];
        tt->charge += tt->mt_q[i];
    }
    // A charge per wake, without time of its own (mcu.wake_charge_uc)
    tt->wake_q = n->wakes * p->wake_q;
    tt->charge += tt->wake_q;
    for (i = 0; i < SIM_RADIO_STATES; i++) {
        if (i == SIM_RADIO_TX)
            continue;
        tt->r_q[i] = n->r_time[i] * p->i_radio[i] / 1000;
        tt->radio_t += n->r_time[i];
        tt->charge += tt->r_q[i];
    }
    for (i = 0; i < n->tx_n; i++) {
        tt->tx_q += n->tx[i].time * tx_current(p, n->tx[i].dbm, &clamped) / 1000;
        tt->tx_clamped |= clamped;
        tt->radio_t += n->tx[i].time;
        tt->tx_t += n->tx[i].time;
    }
    tt->r_q[SIM_RADIO_TX] = tt->tx_q;
    tt->charge += tt->tx_q;
    tt->cca_q = n->cca_time * p->i_cca / 1000;
    tt->radio_t += n->cca_time;
    tt->charge += tt->cca_q;
    for (i = 0; i < SIM_RADIO_STATES; i++) {
        for (j = 0; j < SIM_RADIO_STATES; j++) {
            tt->rt_q += n->rt_time[i][j] * p->i_rt[i][j] / 1000;
            tt->radio_t += n->rt_time[i][j];
        }
    }
    tt->charge += tt->rt_q;
}

static void write_node(FILE *f, const struct pwr_node *n, double t_end, bool last)
{
    const struct pwr_profile *p = n->p;
    struct pwr_totals tt;
    double avg_ma, secs = t_end / 1e6, i_batt, life_h;
    bool clamped;
    int i, j, first;

    node_totals(n, &tt);
    avg_ma = t_end > 0 ? tt.charge * 1000 / t_end : 0;
    fprintf(f, "    {\n      \"node\": %d,\n      \"profile\": \"%s\",\n", n->id, p->name);
    // PHY mode: the radio's; the profile values used (a mode, or "common"), and why
    if (n->mode_radio)
        fprintf(f, "      \"phy_mode\": \"%s\",\n", mode_names[n->mode_radio]);
    else
        fprintf(f, "      \"phy_mode\": null,\n");
    fprintf(f, "      \"profile_values\": \"%s\",\n      \"profile_values_from\": \"%s\",\n",
            p->mode ? mode_names[p->mode] : "common", n->mode_user < 0 ? "radio" : "--power-mode");
    fprintf(f, "      \"avg_current_ma\": %.6f,\n      \"charge_uc\": %.6f,\n      \"energy_mj\": %.6f,\n",
            avg_ma, tt.charge, tt.charge * p->supply_v / 1000);
    fprintf(f, "      \"check\": {\"mcu_time_us\": %.3f, \"radio_time_us\": %.3f, \"run_time_us\": %.3f},\n",
            tt.mcu_t, tt.radio_t, t_end);
    if (pwr.has_cell) {
        i_batt = avg_ma * (pwr.cell_v > 0 ? p->supply_v / (pwr.cell_v * pwr.cell_eff) : 1);
        life_h = i_batt > 0 ? pwr.cell_mah / i_batt : INFINITY;
        fprintf(f, "      \"battery\": {\"capacity_mah\": %g, \"cell_v\": %g, \"efficiency\": %g, "
                "\"cell_current_ma\": %.6f, \"life_h\": %.3f, \"life_years\": %.4f},\n",
                pwr.cell_mah, pwr.cell_v, pwr.cell_eff, i_batt, life_h, life_h / (24 * 365.25));
    }
    fprintf(f, "      \"mcu\": {\n        \"clock_mhz\": %g,\n        \"cycles\": %.0f,\n"
            "        \"wakes_while_active\": %llu,\n",
            p->clock_mhz, n->cycles, (unsigned long long)n->overlaps);
    fprintf(f, "        \"states\": {\n");
    for (i = 0; i < MCU_STATES; i++)
        fprintf(f, "          \"%s\": {\"time_us\": %.3f, \"charge_uc\": %.6f, \"count\": %llu, \"ma\": %g},\n",
                mcu_names[i], n->mcu_time[i], tt.mcu_q[i], (unsigned long long)n->mcu_count[i], p->i_mcu[i]);
    for (i = 0; i < MT_COUNT; i++)
        fprintf(f, "          \"%s\": {\"time_us\": %.3f, \"charge_uc\": %.6f, \"count\": %llu, \"ma\": %g}%s\n",
                mt_names[i], n->mt_time[i], tt.mt_q[i], (unsigned long long)n->mt_count[i], p->i_mt[i],
                i == MT_COUNT - 1 ? "" : ",");
    fprintf(f, "        },\n        \"wake_charge\": {\"count\": %llu, \"uc_each\": %g, \"charge_uc\": %.6f}\n      },\n",
            (unsigned long long)n->wakes, p->wake_q, tt.wake_q);
    fprintf(f, "      \"radio\": {\n        \"states\": {\n");
    for (i = 0; i < SIM_RADIO_STATES; i++) {
        if (i == SIM_RADIO_TX)
            continue;
        fprintf(f, "          \"%s\": {\"time_us\": %.3f, \"charge_uc\": %.6f, \"count\": %llu, \"ma\": %g},\n",
                radio_names[i], n->r_time[i], tt.r_q[i], (unsigned long long)n->r_count[i], p->i_radio[i]);
    }
    fprintf(f, "          \"cca\": {\"time_us\": %.3f, \"charge_uc\": %.6f, \"count\": %llu, \"ma\": %g},\n",
            n->cca_time, tt.cca_q, (unsigned long long)n->cca_count, p->i_cca);
    fprintf(f, "          \"tx\": {\"time_us\": %.3f, \"charge_uc\": %.6f, \"count\": %llu, \"levels\": [",
            tt.tx_t, tt.tx_q, (unsigned long long)n->r_count[SIM_RADIO_TX]);
    for (i = 0; i < n->tx_n; i++) {
        double ma = tx_current(p, n->tx[i].dbm, &clamped);

        fprintf(f, "%s{\"dbm\": %d, \"time_us\": %.3f, \"charge_uc\": %.6f, \"count\": %llu, \"ma\": %g, "
                "\"clamped\": %s}", i ? ", " : "", n->tx[i].dbm, n->tx[i].time, n->tx[i].time * ma / 1000,
                (unsigned long long)n->tx[i].count, ma, clamped ? "true" : "false");
    }
    fprintf(f, "]}\n        },\n        \"transitions\": {");
    first = 1;
    for (i = 0; i < SIM_RADIO_STATES; i++) {
        for (j = 0; j < SIM_RADIO_STATES; j++) {
            if (!n->rt_count[i][j])
                continue;
            fprintf(f, "%s\n          \"%s>%s\": {\"time_us\": %.3f, \"charge_uc\": %.6f, \"count\": %llu}",
                    first ? "" : ",", radio_names[i], radio_names[j], n->rt_time[i][j],
                    n->rt_time[i][j] * p->i_rt[i][j] / 1000, (unsigned long long)n->rt_count[i][j]);
            first = 0;
        }
    }
    fprintf(f, "\n        }\n      },\n");
    fprintf(f, "      \"wakes\": {\n        \"total\": %llu,\n        \"per_s\": %.3f,\n"
            "        \"empty_ticks\": %llu,\n        \"empty_ticks_per_s\": %.3f,\n"
            "        \"unseen_frame_wakes\": %llu,\n        \"events\": %llu,\n"
            "        \"frames_delivered\": %llu,\n        \"frames_unseen\": %llu,\n",
            (unsigned long long)n->wakes, secs > 0 ? n->wakes / secs : 0,
            (unsigned long long)n->empty_ticks, secs > 0 ? n->empty_ticks / secs : 0,
            (unsigned long long)n->wakes_unseen, (unsigned long long)n->events,
            (unsigned long long)n->frames_in, (unsigned long long)n->frames_unseen);
    fprintf(f, "        \"wakes_with\": {");
    for (i = 0; i < NSIM_CAUSE_COUNT; i++)
        fprintf(f, "\"%s\": %llu, ", cause_names[i], (unsigned long long)n->wakes_with[i]);
    fprintf(f, "\"frame\": %llu},\n", (unsigned long long)n->wakes_with[NSIM_CAUSE_COUNT]);
    fprintf(f, "        \"timer_callbacks\": {");
    for (i = 0; i < NSIM_CAUSE_COUNT; i++)
        fprintf(f, "%s\"%s\": %llu", i ? ", " : "", cause_names[i], (unsigned long long)n->timer_cbs[i]);
    fprintf(f, "}\n      }\n    }%s\n", last ? "" : ",");
}

void pwr_finish(uint64_t t_end_us, uint64_t seed)
{
    double t_end = (double)t_end_us;
    struct pwr_totals tt;
    struct pwr_node *n;
    FILE *f;
    int i;

    for (i = 0; i < pwr.num_nodes; i++)
        node_close(pwr.nodes[pwr.ids[i]], t_end);
    if (pwr.log)
        fclose(pwr.log);
    f = fopen(pwr.out_path, "w");
    FATAL_ON(!f, 1, "%s: %m", pwr.out_path);
    fprintf(f, "{\n  \"format\": \"wssim-power 1\",\n  \"run_time_us\": %llu,\n  \"seed\": %llu,\n",
            (unsigned long long)t_end_us, (unsigned long long)seed);
    fprintf(f, "  \"profiles\": [");
    for (struct pwr_profile *p = pwr.profiles; p; p = p->next)
    {
        fprintf(f, "%s{\"name\": \"%s\", \"kind\": \"%s\", \"path\": \"%s\", \"supply_v\": %g, "
                "\"deep_min_us\": %llu, \"modes\": [", p == pwr.profiles ? "" : ", ", p->name, p->kind, p->path,
                p->supply_v, (unsigned long long)p->rule.deep_min_us);
        for (int m = 1, first = 1; m < SIM_PHY_MODES; m++) {
            if (!p->by_mode[m])
                continue;
            fprintf(f, "%s\"%s\"", first ? "" : ", ", mode_names[m]);
            first = 0;
        }
        fprintf(f, "]}");
    }
    fprintf(f, "],\n  \"nodes\": [\n");
    for (i = 0; i < pwr.num_nodes; i++)
        write_node(f, pwr.nodes[pwr.ids[i]], t_end, i == pwr.num_nodes - 1);
    fprintf(f, "  ]\n}\n");
    FATAL_ON(fclose(f), 1, "%s: %m", pwr.out_path);
    for (i = 0; i < pwr.num_nodes; i++) {
        n = pwr.nodes[pwr.ids[i]];
        node_totals(n, &tt);
        INFO("power: node %d (values %s): %.4f mA average, MCU active %.3f%%, radio RX %.2f%% TX %.3f%%, %.1f wakes/s, %.1f empty ticks/s",
             n->id, n->p->mode ? mode_names[n->p->mode] : "common", t_end > 0 ? tt.charge * 1000 / t_end : 0,
             t_end > 0 ? 100 * n->mcu_time[MCU_ACTIVE] / t_end : 0,
             t_end > 0 ? 100 * (n->r_time[SIM_RADIO_LISTEN] + n->r_time[SIM_RADIO_RECEIVE]) / t_end : 0,
             t_end > 0 ? 100 * tt.tx_t / t_end : 0,
             t_end > 0 ? n->wakes / (t_end / 1e6) : 0, t_end > 0 ? n->empty_ticks / (t_end / 1e6) : 0);
    }
    INFO("power: report in %s", pwr.out_path);
}
