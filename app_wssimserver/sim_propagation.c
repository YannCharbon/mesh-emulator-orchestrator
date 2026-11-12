/* SPDX-License-Identifier: Apache-2.0
 *
 * Propagation from node positions: docs/MEDIUM.md §11.
 *
 * At the start (prop_build()), for every pair of positioned nodes: the path
 * loss of the chosen model, the static shadowing of each direction (normal
 * draws from a hash of the run's seed and the pair, so the result does not
 * depend on the order of the computation), and whether the pair hears each
 * other. The mean received power at the nominal TX power goes into the
 * server's RSSI map (the shared memory the radios and the medium read), and
 * the pairs that hear each other into the hearing graph. At run time, the
 * central medium asks prop_link_dbm() for the power at the frame's own TX
 * power: one addition, no per-frame search.
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "common/log.h"
#include "sim_propagation.h"

#define MAX_NODES SIM_MAX_NODES
#define PROP_MAX_FLOORS_TABLE 16

enum prop_model { PROP_INDOOR, PROP_LOGDIST, PROP_FREESPACE };
static const char *const prop_model_names[] = { "indoor", "logdist", "freespace" };

enum {
    OPT_POSITIONS = PROP_OPT_BASE,
    OPT_PROP_MODEL,
    OPT_PROP_FREQ,
    OPT_PROP_PL0,
    OPT_PROP_EXP,
    OPT_PROP_MIN_DIST,
    OPT_PROP_WALL_PER_M,
    OPT_PROP_WALLS,
    OPT_PROP_FLOOR_DB,
    OPT_PROP_FLOOR_HEIGHT,
    OPT_PROP_SHADOW,
    OPT_PROP_SHADOW_DIR,
    OPT_PROP_SHADOW_NODE,
    OPT_PROP_GAIN,
    OPT_PROP_TX_DBM,
    OPT_PROP_CUTOFF,
    OPT_PROP_MARGIN,
    OPT_PROP_DUMP,
    OPT_PROP_DUMP_NODE,
    OPT_RSSI_OFFSET,
    OPT_RSSI_NOISE,
};

const struct option prop_long_options[] = {
    { "positions", required_argument, 0, OPT_POSITIONS },
    { "prop-model", required_argument, 0, OPT_PROP_MODEL },
    { "prop-freq", required_argument, 0, OPT_PROP_FREQ },
    { "prop-pl0", required_argument, 0, OPT_PROP_PL0 },
    { "prop-exp", required_argument, 0, OPT_PROP_EXP },
    { "prop-min-dist", required_argument, 0, OPT_PROP_MIN_DIST },
    { "prop-wall-db-per-m", required_argument, 0, OPT_PROP_WALL_PER_M },
    { "prop-walls", required_argument, 0, OPT_PROP_WALLS },
    { "prop-floor-db", required_argument, 0, OPT_PROP_FLOOR_DB },
    { "prop-floor-height", required_argument, 0, OPT_PROP_FLOOR_HEIGHT },
    { "prop-shadow", required_argument, 0, OPT_PROP_SHADOW },
    { "prop-shadow-dir", required_argument, 0, OPT_PROP_SHADOW_DIR },
    { "prop-shadow-node", required_argument, 0, OPT_PROP_SHADOW_NODE },
    { "prop-gain", required_argument, 0, OPT_PROP_GAIN },
    { "prop-tx-dbm", required_argument, 0, OPT_PROP_TX_DBM },
    { "prop-cutoff", required_argument, 0, OPT_PROP_CUTOFF },
    { "prop-margin", required_argument, 0, OPT_PROP_MARGIN },
    { "prop-dump", required_argument, 0, OPT_PROP_DUMP },
    { "prop-dump-node", required_argument, 0, OPT_PROP_DUMP_NODE },
    { "rssi-offset", required_argument, 0, OPT_RSSI_OFFSET },
    { "rssi-noise", required_argument, 0, OPT_RSSI_NOISE },
    { 0, 0, 0, 0 }
};

// Keys of the hash draws: each kind of draw has its own
#define K_LINK   0x6c696e6b5f736864ULL  // shadowing shared by both directions
#define K_DIR    0x6469725f73686164ULL  // shadowing of one direction
#define K_NODE   0x6e6f64655f736864ULL  // shadowing term of a node
#define K_ROFF   0x727373695f6f6666ULL  // RSSI offset of a receiver
#define K_RNOISE 0x727373695f6e6f69ULL  // RSSI noise of a frame at a receiver
#define K_INTF   0x696e74665f736864ULL  // shadowing of an external interferer's path to a node

static struct {
    bool tuning;                // any --prop-* option
    const char *path;           // --positions
    enum prop_model model;
    bool has_pl0, has_exp, has_floor;
    double freq_mhz, pl0, exp, min_dist;
    double wall_per_m;          // dB per horizontal metre
    double walls_x, walls_y, wall_db;   // grid: a wall every walls_x (walls_y) m, wall_db each; 0: none
    double floor_db[PROP_MAX_FLOORS_TABLE];     // cumulative loss for 1, 2, ... floors apart
    int n_floor_db;
    double floor_height;
    double sh_link, sh_dir, sh_node;
    double gain;
    double tx_dbm, cutoff, margin;
    const char *dump_path;
    int dump_node;              // --prop-dump-node, -1: every pair
    double rssi_offset, rssi_noise;
    uint64_t seed;
    bool seed_set;

    // The run
    bool built;
    struct node_infos *links;
    int n_pos;
    int ids[MAX_NODES];         // positioned nodes, ascending
    bool has[MAX_NODES];
    double x[MAX_NODES], y[MAX_NODES], z[MAX_NODES], g[MAX_NODES];
    int floor[MAX_NODES];
    double node_sh[MAX_NODES];  // per-node shadowing term (dB)
} prop = {
    .model = PROP_INDOOR,
    .freq_mhz = 915,
    .min_dist = 1,
    .floor_height = 3.5,
    .sh_link = 4,
    .tx_dbm = 20,
    .cutoff = -125,
    .margin = 10,
    .dump_node = -1,
};

// Directed pairs whose value is the model's: modelled[rx] bit tx
static uint64_t modelled[MAX_NODES][MAX_NODES / 64];

/* ---- options */

static double prop_number(const char *opt, const char *arg, double min, double max)
{
    char *end;
    double v = strtod(arg, &end);

    FATAL_ON(end == arg || *end || !(v >= min && v <= max), 1, "--%s: expected a number from %g to %g: %s",
             opt, min, max, arg);
    return v;
}

static void prop_parse_floor_db(const char *arg)
{
    char *copy = strdup(arg), *tok, *save = NULL;
    int n = 0;

    FATAL_ON(!copy, 1, "strdup: %m");
    for (tok = strtok_r(copy, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
        FATAL_ON(n >= PROP_MAX_FLOORS_TABLE, 1, "--prop-floor-db: at most %d values", PROP_MAX_FLOORS_TABLE);
        prop.floor_db[n] = prop_number("prop-floor-db", tok, 0, 200);
        FATAL_ON(n && prop.floor_db[n] < prop.floor_db[n - 1], 1,
                 "--prop-floor-db: the losses for 1, 2, ... floors apart must not decrease: %s", arg);
        n++;
    }
    FATAL_ON(!n, 1, "--prop-floor-db: expected DB[,DB...]: %s", arg);
    prop.n_floor_db = n;
    prop.has_floor = true;
    free(copy);
}

// --prop-walls=SX[,SY]:DB
static void prop_parse_walls(const char *arg)
{
    char *copy = strdup(arg), *colon, *comma;

    FATAL_ON(!copy, 1, "strdup: %m");
    colon = strchr(copy, ':');
    FATAL_ON(!colon, 1, "--prop-walls: expected SPACING[,SPACING_Y]:DB (e.g. 8:2.4): %s", arg);
    *colon = '\0';
    prop.wall_db = prop_number("prop-walls", colon + 1, 0, 100);
    comma = strchr(copy, ',');
    if (comma)
        *comma = '\0';
    prop.walls_x = prop_number("prop-walls", copy, 0.1, 1e4);
    prop.walls_y = comma ? prop_number("prop-walls", comma + 1, 0.1, 1e4) : prop.walls_x;
    free(copy);
}

bool prop_option(int opt, const char *arg)
{
    if (opt < PROP_OPT_BASE || opt > OPT_RSSI_NOISE)
        return false;
    if (opt != OPT_POSITIONS && opt != OPT_RSSI_OFFSET && opt != OPT_RSSI_NOISE)
        prop.tuning = true;
    switch (opt) {
    case OPT_POSITIONS:
        prop.path = arg;
        break;
    case OPT_PROP_MODEL:
        for (int i = 0; ; i++) {
            FATAL_ON(i == 3, 1, "--prop-model: expected indoor, logdist or freespace: %s", arg);
            if (!strcmp(arg, prop_model_names[i])) {
                prop.model = i;
                break;
            }
        }
        break;
    case OPT_PROP_FREQ:
        prop.freq_mhz = prop_number("prop-freq", arg, 1, 1e5);
        break;
    case OPT_PROP_PL0:
        prop.pl0 = prop_number("prop-pl0", arg, -50, 200);
        prop.has_pl0 = true;
        break;
    case OPT_PROP_EXP:
        prop.exp = prop_number("prop-exp", arg, 0, 10);
        prop.has_exp = true;
        break;
    case OPT_PROP_MIN_DIST:
        prop.min_dist = prop_number("prop-min-dist", arg, 0.01, 1e4);
        break;
    case OPT_PROP_WALL_PER_M:
        prop.wall_per_m = prop_number("prop-wall-db-per-m", arg, 0, 100);
        break;
    case OPT_PROP_WALLS:
        prop_parse_walls(arg);
        break;
    case OPT_PROP_FLOOR_DB:
        prop_parse_floor_db(arg);
        break;
    case OPT_PROP_FLOOR_HEIGHT:
        prop.floor_height = prop_number("prop-floor-height", arg, 0.1, 1e3);
        break;
    case OPT_PROP_SHADOW:
        prop.sh_link = prop_number("prop-shadow", arg, 0, 30);
        break;
    case OPT_PROP_SHADOW_DIR:
        prop.sh_dir = prop_number("prop-shadow-dir", arg, 0, 30);
        break;
    case OPT_PROP_SHADOW_NODE:
        prop.sh_node = prop_number("prop-shadow-node", arg, 0, 30);
        break;
    case OPT_PROP_GAIN:
        prop.gain = prop_number("prop-gain", arg, -50, 50);
        break;
    case OPT_PROP_TX_DBM:
        prop.tx_dbm = prop_number("prop-tx-dbm", arg, -100, 100);
        break;
    case OPT_PROP_CUTOFF:
        prop.cutoff = prop_number("prop-cutoff", arg, -250, 0);
        break;
    case OPT_PROP_MARGIN:
        prop.margin = prop_number("prop-margin", arg, 0, 100);
        break;
    case OPT_PROP_DUMP:
        prop.dump_path = arg;
        break;
    case OPT_PROP_DUMP_NODE:
    {
        char *end;
        long id = strtol(arg, &end, 10);

        FATAL_ON(end == arg || *end || id < 0 || id >= MAX_NODES, 1, "--prop-dump-node: expected a node ID: %s", arg);
        prop.dump_node = (int)id;
        break;
    }
    case OPT_RSSI_OFFSET:
        prop.rssi_offset = prop_number("rssi-offset", arg, 0, 30);
        break;
    case OPT_RSSI_NOISE:
        prop.rssi_noise = prop_number("rssi-noise", arg, 0, 30);
        break;
    }
    return true;
}

void prop_print_help(FILE *stream)
{
    fprintf(stream, "  Propagation from node positions (docs/MEDIUM.md §11):\n");
    fprintf(stream, "  --positions=FILE          one node per line: ID X Y Z [FLOOR [GAIN_DBI]] (metres); the\n");
    fprintf(stream, "                            links of these nodes come from the model below, and who hears\n");
    fprintf(stream, "                            whom from their mean level; -g values override the model\n");
    fprintf(stream, "  --prop-model=indoor|logdist|freespace  path-loss model (indoor)\n");
    fprintf(stream, "  --prop-freq=MHZ           carrier frequency (915)\n");
    fprintf(stream, "  --prop-pl0=DB             path loss at 1 m (indoor: 20log10(f)-28; others: free space)\n");
    fprintf(stream, "  --prop-exp=N              distance exponent (indoor 3.3, logdist 3.0, freespace 2)\n");
    fprintf(stream, "  --prop-min-dist=M         shortest distance in the formula (1)\n");
    fprintf(stream, "  --prop-wall-db-per-m=DB   wall loss per horizontal metre (0)\n");
    fprintf(stream, "  --prop-walls=SX[,SY]:DB   walls every SX (SY) metres across x (y), DB each (none)\n");
    fprintf(stream, "  --prop-floor-db=L1[,L2..] loss for 1, 2, ... floors apart, the last step repeated\n");
    fprintf(stream, "                            beyond (indoor: 9,19,24; others: 0); one value: per floor\n");
    fprintf(stream, "  --prop-floor-height=M     floor of a node without a FLOOR column: Z / M rounded (3.5)\n");
    fprintf(stream, "  --prop-shadow=DB          shadowing sigma, same both ways (4)\n");
    fprintf(stream, "  --prop-shadow-dir=DB      shadowing sigma of each direction on its own (0)\n");
    fprintf(stream, "  --prop-shadow-node=DB     shadowing sigma of a term per node, in all its links (0)\n");
    fprintf(stream, "  --prop-gain=DBI           antenna gain of a node without a GAIN column (0)\n");
    fprintf(stream, "  --prop-tx-dbm=DBM         TX power of the hearing graph and of the RSSI map (20)\n");
    fprintf(stream, "  --prop-cutoff=DBM, --prop-margin=DB  a pair hears each other when its mean level at\n");
    fprintf(stream, "                            --prop-tx-dbm plus the margin reaches the cutoff (-125, 10)\n");
    fprintf(stream, "  --prop-dump=FILE          the computed links at start-up, one line per direction\n");
    fprintf(stream, "  --prop-dump-node=ID       with --prop-dump, only the links of this node\n");
    fprintf(stream, "  --rssi-offset=DB, --rssi-noise=DB  central medium: error of the RSSI reported to the\n");
    fprintf(stream, "                            receivers, sigma per receiver (fixed) and per frame (0, 0)\n");
}

void prop_check_options(bool central)
{
    FATAL_ON((prop.tuning || prop.dump_path) && !prop.path, 1, "--prop-* options need --positions");
    FATAL_ON(prop.dump_node >= 0 && !prop.dump_path, 1, "--prop-dump-node needs --prop-dump");
    FATAL_ON((prop.rssi_offset > 0 || prop.rssi_noise > 0) && !central, 1,
             "--rssi-offset and --rssi-noise need the central medium (--vtime, not --medium=legacy)");
}

bool prop_enabled(void)
{
    return prop.path != NULL;
}

/* ---- draws */

static uint64_t mix64(uint64_t z)
{
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

// A standard normal draw from a hash of (seed, key, a, b): Box-Muller on two
// uniforms. Any order of computation gives the same value.
static double prop_normal(uint64_t key, uint64_t a, uint64_t b)
{
    uint64_t h = mix64(prop.seed + mix64(key + mix64(a + 1) + mix64(mix64(b + 1))));
    uint64_t h2 = mix64(h + 0x9e3779b97f4a7c15ULL);
    double u1 = ((h >> 11) + 1) * 0x1.0p-53;    // (0, 1]
    double u2 = (h2 >> 11) * 0x1.0p-53;          // [0, 1)

    return sqrt(-2 * log(u1)) * cos(2 * M_PI * u2);
}

/* ---- positions */

#define PROP_MAX_COORD 1e6         // m
#define PROP_MAX_FLOOR 10000

// One number token, whole and finite, within [min, max]
static bool prop_token(const char *tok, double min, double max, double *v)
{
    char *end;

    *v = strtod(tok, &end);
    return end != tok && !*end && isfinite(*v) && *v >= min && *v <= max;
}

static void prop_read_positions(void)
{
    FILE *f = fopen(prop.path, "r");
    char *line = NULL;
    size_t cap = 0;
    int lineno = 0;

    FATAL_ON(!f, 1, "--positions: %s: %m", prop.path);
    // getline: lines of any length (a long comment included)
    while (getline(&line, &cap, f) >= 0) {
        char *tok[7], *save = NULL, *hash, *t, *end;
        double v[5];
        int n = 0;
        long id;

        lineno++;
        hash = strchr(line, '#');
        if (hash)
            *hash = '\0';
        for (t = strtok_r(line, " \t\r\n", &save); t && n < 7; t = strtok_r(NULL, " \t\r\n", &save))
            tok[n++] = t;
        if (!n)
            continue;
        FATAL_ON(n < 4 || n > 6, 1, "--positions: %s:%d: expected ID X Y Z [FLOOR [GAIN_DBI]]", prop.path, lineno);
        id = strtol(tok[0], &end, 10);
        FATAL_ON(end == tok[0] || *end || id < 0 || id >= MAX_NODES, 1,
                 "--positions: %s:%d: bad node ID \"%s\" (an integer from 0 to %d)", prop.path, lineno, tok[0],
                 MAX_NODES - 1);
        for (int k = 1; k < n; k++) {
            double lim = k <= 3 ? PROP_MAX_COORD : k == 4 ? PROP_MAX_FLOOR : 50;
            FATAL_ON(!prop_token(tok[k], -lim, lim, &v[k - 1]), 1,
                     "--positions: %s:%d: bad %s \"%s\" (a finite number from %g to %g)", prop.path, lineno,
                     k <= 3 ? "coordinate" : k == 4 ? "FLOOR" : "GAIN_DBI", tok[k], -lim, lim);
        }
        FATAL_ON(prop.has[id], 1, "--positions: %s:%d: node %ld given twice", prop.path, lineno, id);
        prop.has[id] = true;
        prop.x[id] = v[0];
        prop.y[id] = v[1];
        prop.z[id] = v[2];
        if (n >= 5) {
            FATAL_ON(v[3] != floor(v[3]), 1, "--positions: %s:%d: FLOOR must be an integer", prop.path, lineno);
            prop.floor[id] = (int)v[3];
        } else {
            prop.floor[id] = (int)floor(v[2] / prop.floor_height + 0.5);
        }
        prop.g[id] = n >= 6 ? v[4] : prop.gain;
    }
    free(line);
    fclose(f);
    for (int i = 0; i < MAX_NODES; i++)
        if (prop.has[i])
            prop.ids[prop.n_pos++] = i;
    FATAL_ON(!prop.n_pos, 1, "--positions: %s: no node", prop.path);
}

/* ---- model */

static double prop_floor_loss(int k)
{
    int m = prop.n_floor_db;

    if (k <= 0 || !m)
        return 0;
    if (k <= m)
        return prop.floor_db[k - 1];
    // Beyond the table: its last step repeated (one value: that value per floor)
    return prop.floor_db[m - 1] + (k - m) * (m >= 2 ? prop.floor_db[m - 1] - prop.floor_db[m - 2] : prop.floor_db[0]);
}

static int prop_walls_between(double a, double b, double spacing)
{
    // Walls at (k + 1/2) * spacing: nodes on a grid of that spacing are in
    // the middle of their rooms
    return abs((int)floor(a / spacing + 0.5) - (int)floor(b / spacing + 0.5));
}

struct prop_path {
    double d, pl;
    int floors, walls;
};

static struct prop_path prop_path_loss(int a, int b)
{
    struct prop_path r = { };
    double dx = prop.x[a] - prop.x[b], dy = prop.y[a] - prop.y[b], dz = prop.z[a] - prop.z[b];
    double dh = sqrt(dx * dx + dy * dy);

    r.d = sqrt(dh * dh + dz * dz);
    r.pl = prop.pl0 + 10 * prop.exp * log10(fmax(r.d, prop.min_dist)) + prop.wall_per_m * dh;
    if (prop.wall_db > 0) {
        r.walls = prop_walls_between(prop.x[a], prop.x[b], prop.walls_x) +
                  prop_walls_between(prop.y[a], prop.y[b], prop.walls_y);
        r.pl += r.walls * prop.wall_db;
    }
    r.floors = abs(prop.floor[a] - prop.floor[b]);
    r.pl += prop_floor_loss(r.floors);
    return r;
}

// Shadowing of rx's reception of tx (dB, added to the loss)
static double prop_shadow(int rx, int tx)
{
    int lo = rx < tx ? rx : tx, hi = rx < tx ? tx : rx;
    double s = 0;

    if (prop.sh_link > 0)
        s += prop.sh_link * prop_normal(K_LINK, lo, hi);
    if (prop.sh_dir > 0)
        s += prop.sh_dir * prop_normal(K_DIR, rx, tx);
    if (prop.sh_node > 0)
        s += prop.node_sh[rx] + prop.node_sh[tx];
    return s;
}

static void prop_defaults(void)
{
    double fspl1 = 20 * log10(prop.freq_mhz) - 27.55;       // free space at 1 m, f in MHz

    if (!prop.has_pl0)
        prop.pl0 = prop.model == PROP_INDOOR ? 20 * log10(prop.freq_mhz) - 28 : fspl1;
    if (!prop.has_exp)
        prop.exp = prop.model == PROP_INDOOR ? 3.3 : prop.model == PROP_LOGDIST ? 3.0 : 2.0;
    if (!prop.has_floor && prop.model == PROP_INDOOR) {
        // ITU-R P.1238, 900 MHz, office: 9, 19, 24 dB for 1, 2, 3 floors
        prop.floor_db[0] = 9;
        prop.floor_db[1] = 19;
        prop.floor_db[2] = 24;
        prop.n_floor_db = 3;
    }
}

void prop_build(uint64_t (*graph)[MAX_NODES / 64], struct node_infos *links, const uint8_t *given)
{
    const char *seed_env = getenv("WSSIM_SEED");
    struct timespec t0, t1;
    FILE *dump = NULL;
    long heard = 0, by_model = 0, usable = 0, asym = 0, overridden = 0;
    int deg_min = MAX_NODES, deg_max = 0;
    static int deg[MAX_NODES];
    char floors[128] = "none";

    if (!prop.path)
        return;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    prop.seed = (seed_env && *seed_env) ? strtoull(seed_env, NULL, 0) : 1;
    prop.seed_set = true;
    prop.links = links;
    prop_defaults();
    prop_read_positions();
    if (prop.sh_node > 0)
        for (int k = 0; k < prop.n_pos; k++)
            prop.node_sh[prop.ids[k]] = prop.sh_node * prop_normal(K_NODE, prop.ids[k], 0);
    if (prop.dump_path) {
        dump = fopen(prop.dump_path, "w");
        FATAL_ON(!dump, 1, "--prop-dump: %s: %m", prop.dump_path);
        fprintf(dump, "# rx tx dist_m floors walls pl_db shadow_db gains_db mean_dbm heard given\n"
                      "# at start-up; mean_dbm at --prop-tx-dbm %.1f dBm; heard: 1 if the model puts the pair in the graph "
                      "(0: added by a -g option only); given: 1 if a -g value sets that direction\n",
                prop.tx_dbm);
    }
    for (int ka = 0; ka < prop.n_pos; ka++) {
        int a = prop.ids[ka];

        for (int kb = ka + 1; kb < prop.n_pos; kb++) {
            int b = prop.ids[kb];
            struct prop_path pp = prop_path_loss(a, b);
            double gains = prop.g[a] + prop.g[b];
            double s_ab = prop_shadow(b, a), s_ba = prop_shadow(a, b);   // at b of a, at a of b
            double p_ab = prop.tx_dbm + gains - pp.pl - s_ab;            // power at b of a's frames
            double p_ba = prop.tx_dbm + gains - pp.pl - s_ba;
            bool hears = fmax(p_ab, p_ba) + prop.margin >= prop.cutoff;
            bool in_graph = graph[a][b / 64] & (1ULL << (b % 64));
            bool g_ab = given[(long)b * MAX_NODES + a], g_ba = given[(long)a * MAX_NODES + b];

            if (!hears && !in_graph)
                continue;
            if (hears && !in_graph) {
                graph[a][b / 64] |= 1ULL << (b % 64);
                graph[b][a / 64] |= 1ULL << (a % 64);
            }
            heard++;
            by_model += hears;
            deg[a]++;
            deg[b]++;
            if (fmax(p_ab, p_ba) >= -100)
                usable++;
            if (!g_ab) {
                sim_link(links, b, a)->rssi = (float)p_ab;
                modelled[b][a / 64] |= 1ULL << (a % 64);
            }
            if (!g_ba) {
                sim_link(links, a, b)->rssi = (float)p_ba;
                modelled[a][b / 64] |= 1ULL << (b % 64);
            }
            overridden += g_ab + g_ba;
            if (!g_ab && !g_ba && (float)p_ab != (float)p_ba)
                asym++;
            if (dump && (prop.dump_node < 0 || prop.dump_node == a || prop.dump_node == b)) {
                fprintf(dump, "%d %d %.2f %d %d %.2f %.2f %.2f %.2f %d %d\n", b, a, pp.d, pp.floors, pp.walls,
                        pp.pl, s_ab, gains, p_ab, hears, g_ab);
                fprintf(dump, "%d %d %.2f %d %d %.2f %.2f %.2f %.2f %d %d\n", a, b, pp.d, pp.floors, pp.walls,
                        pp.pl, s_ba, gains, p_ba, hears, g_ba);
            }
        }
    }
    if (dump)
        fclose(dump);
    for (int k = 0; k < prop.n_pos; k++) {
        int d = deg[prop.ids[k]];
        deg_min = d < deg_min ? d : deg_min;
        deg_max = d > deg_max ? d : deg_max;
    }
    if (prop.n_floor_db) {
        int n = 0;
        for (int i = 0; i < prop.n_floor_db && n < (int)sizeof(floors) - 8; i++)
            n += snprintf(floors + n, sizeof(floors) - n, "%s%g", i ? "/" : "", prop.floor_db[i]);
        snprintf(floors + n, sizeof(floors) - n, " dB");
    }
    prop.built = true;
    clock_gettime(CLOCK_MONOTONIC, &t1);
    INFO("propagation: %d nodes from %s, model %s: PL(1 m) %.1f dB, exponent %.2f, walls %.2f dB/m%s, floors %s",
         prop.n_pos, prop.path, prop_model_names[prop.model], prop.pl0, prop.exp, prop.wall_per_m,
         prop.wall_db > 0 ? " and a grid" : "", floors);
    if (prop.wall_db > 0)
        INFO("propagation: walls every %g m (x) and %g m (y), %.1f dB each", prop.walls_x, prop.walls_y, prop.wall_db);
    INFO("propagation: shadowing %.1f dB both ways, %.1f dB per direction, %.1f dB per node, seed %llu",
         prop.sh_link, prop.sh_dir, prop.sh_node, (unsigned long long)prop.seed);
    INFO("propagation: %ld pairs in the graph among the positioned nodes, %ld of them from the model (mean level at "
         "%.1f dBm TX + %.1f dB >= %.1f dBm) and %ld from -g only; degree %.1f "
         "(%d-%d), %ld pairs at -100 dBm or more, %ld asymmetric, %ld directions set by -g; %.2f s",
         heard, by_model, prop.tx_dbm, prop.margin, prop.cutoff, heard - by_model, prop.n_pos ? 2.0 * heard / prop.n_pos : 0.0,
         deg_min, deg_max, usable, asym, overridden,
         (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) * 1e-9);
}

// An external interferer at pos (docs/MEDIUM.md §13.1): the mean level at
// node of a transmitter there with eirp_dbm (its antenna gain included), by
// the same path loss as between nodes, the node's antenna gain, and static
// shadowing: the link term (sigma_link) drawn for the pair (position, node),
// both directions alike, and the node's own term (sigma_node); no direction
// term (an interferer only transmits). The interferer's floor comes from z,
// as for a node without FLOOR. Registered with intf_set_position_model().
bool prop_point_dbm(int node, const double pos[3], float eirp_dbm, float *dbm)
{
    double dx, dy, dz, dh, d, pl, s = 0;
    uint64_t key;
    int fl;

    if (!prop.built || !prop.has[node])
        return false;
    dx = pos[0] - prop.x[node];
    dy = pos[1] - prop.y[node];
    dz = pos[2] - prop.z[node];
    dh = sqrt(dx * dx + dy * dy);
    d = sqrt(dh * dh + dz * dz);
    pl = prop.pl0 + 10 * prop.exp * log10(fmax(d, prop.min_dist)) + prop.wall_per_m * dh;
    if (prop.wall_db > 0)
        pl += (prop_walls_between(pos[0], prop.x[node], prop.walls_x) +
               prop_walls_between(pos[1], prop.y[node], prop.walls_y)) * prop.wall_db;
    fl = (int)floor(pos[2] / prop.floor_height + 0.5);
    pl += prop_floor_loss(abs(fl - prop.floor[node]));
    // The position's key: its coordinates rounded to the millimetre
    key = mix64(K_INTF + mix64((uint64_t)llround(pos[0] * 1000)) + mix64(mix64((uint64_t)llround(pos[1] * 1000))) +
                mix64(mix64(mix64((uint64_t)llround(pos[2] * 1000)))));
    if (prop.sh_link > 0)
        s += prop.sh_link * prop_normal(key, node, 0);
    if (prop.sh_node > 0)
        s += prop.node_sh[node];
    *dbm = eirp_dbm + prop.g[node] - pl - s;
    return true;
}

bool prop_modelled(int rx, int tx)
{
    return prop.built && (modelled[rx][tx / 64] & (1ULL << (tx % 64)));
}

float prop_link_dbm(int rx, int tx, int8_t tx_dbm)
{
    return sim_link(prop.links, rx, tx)->rssi + (float)(tx_dbm - prop.tx_dbm);
}

bool prop_positioned(int a, int b)
{
    return prop.built && prop.has[a] && prop.has[b];
}

void prop_override(int a, int b)
{
    if (!prop.built)
        return;
    modelled[a][b / 64] &= ~(1ULL << (b % 64));
    modelled[b][a / 64] &= ~(1ULL << (a % 64));
}

/* ---- reported RSSI */

bool prop_rssi_error(void)
{
    return prop.rssi_offset > 0 || prop.rssi_noise > 0;
}

float prop_reported_dbm(int rx, uint32_t frame_id, float dbm)
{
    double e = 0;

    if (!prop_rssi_error())
        return dbm;
    if (!prop.seed_set) {
        const char *seed_env = getenv("WSSIM_SEED");
        prop.seed = (seed_env && *seed_env) ? strtoull(seed_env, NULL, 0) : 1;
        prop.seed_set = true;
    }
    if (prop.rssi_offset > 0)
        e += prop.rssi_offset * prop_normal(K_ROFF, rx, 0);
    if (prop.rssi_noise > 0)
        e += prop.rssi_noise * prop_normal(K_RNOISE, rx, frame_id);
    return (float)(dbm + e);
}
