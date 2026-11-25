#include <stdint.h>
#include <stdbool.h>
#include <inttypes.h>
#include <poll.h>
#include <sys/un.h>
#include <unistd.h>
#include <getopt.h>
#include <sys/socket.h>
#include <sys/resource.h>
#include "common/log.h"
#include "common/utils.h"
#include <fcntl.h>
#include <sys/mman.h>
#include <signal.h>
#include <errno.h>
#include <sys/file.h>
#include <stdlib.h>
#include <time.h>
#include <stdarg.h>

#include "sim_proto.h"
#include "ns_port_sim.h"
#include "sim_power.h"
#include "sim_channel.h"
#include "sim_interf.h"
#include "sim_medium.h"
#include "sim_describe.h"
#include "sim_propagation.h"

#define MAX_NODES SIM_MAX_NODES

struct node_infos *nodes_infos_flat_map;
static bool verbose;            // -v: one line per connection, the fd limit

struct ctxt {
    struct sockaddr_un addr;
    uint64_t node_graph[MAX_NODES][MAX_NODES / 64];
    char **groups;              // the -g options (into argv), applied once the shared memory exists
    int num_groups;
    bool dump, dump2file;
    char dump_path[4096];
    int vtime_nodes;            // --vtime: number of nodes, 0 in wall-clock mode
    bool has_vtime_end;
    uint64_t vtime_end_us;
};

static int increase_limit_fd()
{
    struct rlimit rlimit;

    getrlimit(RLIMIT_NOFILE, &rlimit);
    if (verbose)
        DEBUG("Increase file descriptors limit from %ld to %ld",
              rlimit.rlim_cur, rlimit.rlim_max);
    rlimit.rlim_cur = rlimit.rlim_max;
    setrlimit(RLIMIT_NOFILE, &rlimit);
    return rlimit.rlim_cur;
}

static int bitmap_get(int shift, uint64_t *in, int size)
{
    int word_nr = shift / 64;
    int bit_nr = shift % 64;

    BUG_ON(word_nr >= size);
    return !!(in[word_nr] & (1ULL << bit_nr));
}

static int bitmap_set(int shift, uint64_t *out, int size)
{
    int word_nr = shift / 64;
    int bit_nr = shift % 64;

    if (word_nr >= size)
        return -1;
    out[word_nr] |= 1ULL << bit_nr;
    return 0;
}

static int bitmap_clr(int shift, uint64_t *out, int size)
{
    int word_nr = shift / 64;
    int bit_nr = shift % 64;

    if (word_nr >= size)
        return -1;
    out[word_nr] &= ~(1ULL << bit_nr);
    return 0;
}

static int bitmap_parse(char *str, uint64_t *out, int size)
{
    char *range;
    char *endptr;
    unsigned long cur, end;

    char *str_copy = strdup(str);
    if (str_copy == NULL) {
        return -1;
    }

    char *indexes_part = strtok(str_copy, ":");

    if (indexes_part == NULL) {
        // No RSSI has been specified
        strcpy(str_copy, str);
    }

    memset(out, 0, size * sizeof(uint64_t));
    range = strtok(str_copy, ",");
    if (!range) {
        free(str_copy);
        return -1;
    }
    do {
        cur = strtoul(range, &endptr, 0);
        if (*endptr == '-') {
            range = endptr + 1;
            end = strtol(range, &endptr, 0);
        } else {
            end = cur;
        }
        if (*endptr != '\0') {
            free(str_copy);
            return -1;
        }
        if (cur > end) {
            free(str_copy);
            return -1;
        }
        for (; cur <= end; cur++)
            if (bitmap_set(cur, out, size) < 0) {
                free(str_copy);
                return -1;
            }
    } while ((range = strtok(NULL, ",")));

    free(str_copy);
    return 0;
}

// RSSI of the links from the -g options (docs/MEDIUM.md §2, docs/RADIO_INTERFACE.md §8).
// The map is directional: sim_link(map, rx, tx) is the power at rx of tx's
// frames. "A,B:RSSI" (exactly two IDs) is the link A -> B; it also sets B -> A
// unless a "B,A:RSSI" option gives that direction itself, before or after it:
// so a link given once is symmetric, and a reverse link makes it asymmetric.
// Any other list ("0-3:RSSI", "1,2,5:RSSI", "0-2,7:RSSI") sets every pair of
// its nodes, both directions. Options apply in order, the later one wins; a
// change of a value that an earlier option set itself (not only as the
// reverse of a link) is a warning. A group without ":RSSI" sets no value.
enum link_origin {
    LINK_UNSET,
    LINK_REVERSE,               // set as the reverse of an "A,B:RSSI" link
    LINK_GROUP,                 // set by a group of other than two IDs
    LINK_GIVEN,                 // set by an "A,B:RSSI" link in this direction
};

struct link_parse {
    uint8_t *origin;            // MAX_NODES x MAX_NODES, [rx][tx]
    int overrides;              // values an earlier option set, changed
    int asymmetric;             // reverse links of a different value
};

static void link_set(struct link_parse *lp, const char *opt, int tx, int rx, float rssi, enum link_origin origin)
{
    uint8_t *o = &lp->origin[(long)rx * MAX_NODES + tx];
    float *v = &sim_link(nodes_infos_flat_map, rx, tx)->rssi;

    if ((*o == LINK_GROUP || *o == LINK_GIVEN) && *v != rssi) {
        if (lp->overrides++ < 10)
            WARN("-g %s: the link %d -> %d was %.1f dBm (an earlier -g), now %.1f dBm", opt, tx, rx, *v, rssi);
        else if (lp->overrides == 11)
            WARN("-g: more links set again with another value (count at the end)");
    }
    *v = rssi;
    *o = origin;
}

// "-g" option: a list of IDs and ranges, then optionally ":RSSI"
static int rssi_map_parse_for_one_group(struct link_parse *lp, const char *str)
{
    static int ids[MAX_NODES];
    char buf[65536], *colon, *tok, *save, *end;
    unsigned long a, b, k;
    int n = 0, i, j;
    float rssi;
    bool pair;

    if (strlen(str) >= sizeof(buf))
        return -1;
    strcpy(buf, str);
    colon = strchr(buf, ':');
    if (!colon)
        return 0;               // no RSSI: a perfect link (0), the value left as it is
    *colon = '\0';
    rssi = strtof(colon + 1, &end);
    if (end == colon + 1 || *end) {
        WARN("-g %s: bad RSSI \"%s\" (a number of dBm, e.g. -67.5)", str, colon + 1);
        return -1;
    }
    pair = true;
    for (tok = strtok_r(buf, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
        a = strtoul(tok, &end, 0);
        b = a;
        if (*end == '-') {
            pair = false;       // a range is a group, even of two nodes
            b = strtoul(end + 1, &end, 0);
        }
        if (*end || b < a || b >= MAX_NODES)
            return -1;
        for (k = a; k <= b; k++) {
            if (n >= MAX_NODES)
                return -1;
            ids[n++] = k;
        }
    }
    if (pair && n == 2 && ids[0] != ids[1]) {
        uint8_t rev = lp->origin[(long)ids[0] * MAX_NODES + ids[1]];  // ids[1] -> ids[0]

        link_set(lp, str, ids[0], ids[1], rssi, LINK_GIVEN);
        if (rev != LINK_GIVEN)
            link_set(lp, str, ids[1], ids[0], rssi, LINK_REVERSE);
        else if (sim_link(nodes_infos_flat_map, ids[0], ids[1])->rssi != rssi && lp->asymmetric++ < 10)
            INFO("-g %s: asymmetric link: %d -> %d %.1f dBm, %d -> %d %.1f dBm", str, ids[0], ids[1], rssi,
                 ids[1], ids[0], sim_link(nodes_infos_flat_map, ids[0], ids[1])->rssi);
        return 0;
    }
    for (i = 0; i < n; i++)
        for (j = 0; j < n; j++)
            if (ids[i] != ids[j])
                link_set(lp, str, ids[i], ids[j], rssi, LINK_GROUP);
    return 0;
}

static void graph_apply_mask(uint64_t node_graph[MAX_NODES][MAX_NODES / 64], uint64_t mask[MAX_NODES / 64])
{
    int i, j;

    for (i = 0; i < MAX_NODES; i++)
        if (bitmap_get(i, mask, MAX_NODES / 64))
            for (j = 0; j < MAX_NODES; j++)
                if (bitmap_get(j, mask, MAX_NODES / 64))
                    bitmap_set(j, node_graph[i], MAX_NODES / 64);
}

static int graph_get_num_nodes(struct ctxt *ctxt)
{
    int max = 0;
    int i, j;

    for (i = 0; i < MAX_NODES; i++)
        for (j = 0; j < MAX_NODES; j++)
            if (bitmap_get(j, ctxt->node_graph[i], MAX_NODES / 64))
                max = i;
    return max + 1;
}

static void graph_dump(struct ctxt *ctxt)
{
    int max = graph_get_num_nodes(ctxt);
    int i, j;

    printf("Topology:\n");
    for (i = 0; i < max; i++) {
        printf("%02x ", i);
        for (j = 0; j < max; j++) {
            char bf[10];
            sprintf(bf, "%02x|", j);
            printf("%s", bitmap_get(j, ctxt->node_graph[i], MAX_NODES / 64) ? bf : "--|");
        }
        printf("\n");
    }

    printf("\nRSSI map (dBm; row: receiver, column: sender):\n");
    for (i = 0; i < max; i++) {
        if (i == 0) {
            printf("%*d ", 11, i);
        } else {
            printf("%*d ", 5, i);
        }

    }
    printf("\n");
    for (i = 0; i < max; i++) {
        printf("%*d ", 5, i);
        for (j = 0; j < max; j++) {
            printf("%*.*g ", 5, 6, sim_link(nodes_infos_flat_map, i, j)->rssi);
        }
        printf("\n");
    }
}

static void graph_dump_to_file(struct ctxt *ctxt, char * file_path)
{
    int max = graph_get_num_nodes(ctxt);
    int i, j;

    FILE *f = fopen(file_path, "w");
    if (f == NULL) {
        perror("open");
        exit(-1);
    }
    fprintf(f, "%04d\n", max);
    for (i = 0; i < max; i++)
        for (j = 0; j < max; j++)
            if (bitmap_get(j, ctxt->node_graph[i], MAX_NODES / 64))
                fprintf(f, "%04d,%04d\n", i, j);
    fclose(f);
}

void print_help(FILE *stream, int exit_code) {
    fprintf(stream, "broadcast server to create networks of wshwsim\n");
    fprintf(stream, "\n");
    fprintf(stream, "Usage: wssimserver [OPTIONS] SOCKET_PATH\n");
    fprintf(stream, "  -g, --group=NODES[:RSSI]  link these nodes (e.g. 0-2, or 2,5:-67.0). RSSI in dBm at the\n");
    fprintf(stream, "                            receiver. A,B:RSSI is the link A->B, and also B->A unless an\n");
    fprintf(stream, "                            option B,A:RSSI gives it (asymmetric link); a range or a list\n");
    fprintf(stream, "                            of other than two IDs sets every pair, both directions; the\n");
    fprintf(stream, "                            later option wins (warning). docs/MEDIUM.md section 2\n");
    fprintf(stream, "  --group-file=FILE         -g options from FILE, one per line, with or without \"-g\"\n");
    fprintf(stream, "                            (blank lines and # comments skipped): for topologies too\n");
    fprintf(stream, "                            large for the command line\n");
    fprintf(stream, "  --describe=json           print every option (type, default, preset values, help) as\n");
    fprintf(stream, "                            JSON and exit (docs/MEDIUM.md §11.12)\n");
    fprintf(stream, "  -v, --verbose             also log each connection's fd and the fd limit\n");
    fprintf(stream, "  -l, --dump                print the topology and the RSSI map\n");
    fprintf(stream, "  -f, --fpath[=FILE]        with -l, also write the edges to FILE (/tmp/graph.txt)\n");
    fprintf(stream, "  --vtime=N                 virtual time: conduct N nodes (docs/VIRTUAL_TIME.md)\n");
    fprintf(stream, "  --vtime-end=SECONDS       virtual time: end the run at this time\n");
    fprintf(stream, "  --drift=[IDS:]PPM         virtual time: clock error of these nodes (all if no IDS), in ppm;\n");
    fprintf(stream, "                            repeatable, the last one naming a node wins\n");
    fprintf(stream, "  --drift-random=PPM        virtual time: each node draws its clock error in [-PPM, +PPM]\n");
    fprintf(stream, "                            from WSSIM_SEED (a --drift naming the node takes precedence)\n");
    fprintf(stream, "  --vtime-kill=ID:SECONDS   virtual time: stop node ID at that time, outright: the conductor\n");
    fprintf(stream, "                            closes its connection and delivers nothing more to it; repeatable\n");
    fprintf(stream, "  --vtime-start=ID:SECONDS  virtual time: one more instance of node ID connects at the start\n");
    fprintf(stream, "                            (after the first one) and is welcomed at that time, once the\n");
    fprintf(stream, "                            previous one is stopped (stopped then if still running); repeatable\n");
    fprintf(stream, "  --vtime-power-up=ID:SECONDS virtual time: node ID (its first instance) is welcomed at that\n");
    fprintf(stream, "                            time instead of at 0: until then it neither runs nor hears; repeatable\n");
    fprintf(stream, "  --vtime-cut=SRC:DST:SECONDS virtual time: from that time, frames from SRC no longer reach\n");
    fprintf(stream, "                            DST (one direction); repeatable\n");
    fprintf(stream, "  --vtime-link=A:B:SECONDS:RSSI virtual time: from that time, the link between A and B has\n");
    fprintf(stream, "                            this RSSI (dBm), both directions (it must exist); repeatable\n");
    fprintf(stream, "  --vtime-drop=SRC:DST:SEQ:FROM:TO virtual time: frames from SRC with this MAC sequence\n");
    fprintf(stream, "                            number are not delivered to DST between FROM and TO s; repeatable\n");
    fprintf(stream, "  --vtime-control=SOCKET    virtual time: a scenario driver connects to this UNIX stream socket\n");
    fprintf(stream, "                            and steps the run (pause, run until, commands to the nodes and the\n");
    fprintf(stream, "                            medium; docs/SCENARIOS.md)\n");
    fprintf(stream, "  --vtime-frames=FILE       virtual time: write every frame sent (time us, sender, channel,\n");
    fprintf(stream, "                            PSDU in hex) to FILE\n");
    fprintf(stream, "  --power=PROFILE           virtual time: record power, every node with this profile\n");
    fprintf(stream, "                            (docs/POWER_MODEL.md)\n");
    fprintf(stream, "  --power-node=IDS:PROFILE  another profile for these nodes (3, 2-5, 1,4); repeatable\n");
    fprintf(stream, "  --power-mode=[IDS:]MODE   profile values of this PHY mode (1a ... 5), common, or auto (the\n");
    fprintf(stream, "                            default: the mode the node's MAC configured); repeatable\n");
    fprintf(stream, "  --power-out=FILE          the power report (default power.json)\n");
    fprintf(stream, "  --power-log=FILE          also log every state interval of every node\n");
    fprintf(stream, "  --power-cell=MAH[:V[:EFF]] battery life for this cell (capacity, voltage, converter efficiency)\n");
    fprintf(stream, "  --medium=legacy|central   virtual time: who decides reception: the server (central, the\n");
    fprintf(stream, "                            default; docs/MEDIUM.md) or each radio (legacy: the behaviour\n");
    fprintf(stream, "                            of the permanent reference runs)\n");
    fprintf(stream, "  --medium-preset=calibrated|realistic|basic|em4  central: a set of medium options, given\n");
    fprintf(stream, "                            before the command line's own (which override it): calibrated\n");
    fprintf(stream, "                            (the default: layered receiver curve with fading, adjacent-channel\n");
    fprintf(stream, "                            table 10/40, smooth capture, PA maximum 16.9 dBm), realistic (calibrated + slow\n");
    fprintf(stream, "                            shadowing 2 dB/60 s and RSSI offset 0.9 dB, illustrative), basic\n");
    fprintf(stream, "                            (the defaults before the presets), em4 (the MVP; also implied\n");
    fprintf(stream, "                            by --medium-model=em4 without a preset); docs/MEDIUM.md §14\n");
    fprintf(stream, "  --medium-tx-ref=DBM       central: link values are the received power when the sender\n");
    fprintf(stream, "                            transmits at DBM (default: whatever its TX power)\n");
    fprintf(stream, "  --medium-tx-max=DBM|profile|off  central: the most a sender radiates, for the links\n");
    fprintf(stream, "                            that depend on its TX power (--medium-tx-ref, positions):\n");
    fprintf(stream, "                            a value (calibrated, realistic: 16.9), profile: the top of its\n");
    fprintf(stream, "                            power profile's TX table (needs --power), off (basic, em4)\n");
    fprintf(stream, "  --medium-lock-snr=DB      central: lowest SINR at which a receiver locks onto a frame (0)\n");
    fprintf(stream, "  --medium-model=fg25|em4   central: reception model: the FG25 kit measurements (fg25, the\n");
    fprintf(stream, "                            default) or the MVP's (em4: interference as noise, capture by\n");
    fprintf(stream, "                            6 dB over the SHR, no adjacent channel); docs/MEDIUM.md §2.1\n");
    fprintf(stream, "  Per PHY (MODE: 1a, 1b, 3 or other; without it, every PHY), over the model's values:\n");
    fprintf(stream, "  --medium-int-curve=[MODE:]X0,B|noise  interference bit error rate 0.5 exp(-(SIR-X0)/B),\n");
    fprintf(stream, "                            or noise: the noise curve of the SINR (fg25 3: -2.85,0.9)\n");
    fprintf(stream, "  --medium-capture-db=[MODE:]DB  a frame this much stronger captures the receiver in the\n");
    fprintf(stream, "                            capture windows (fg25: 3; em4: 6)\n");
    fprintf(stream, "  --medium-capture-early-bits=[MODE:]N|shr  early capture window, bits from the SHR's\n");
    fprintf(stream, "                            start, or the whole SHR (fg25: 1a 20, 3 37; em4: shr)\n");
    fprintf(stream, "  --medium-capture-sfd=[MODE:]on|off  capture also during the SFD (fg25: 3 on, 1a off)\n");
    fprintf(stream, "  --medium-break-db=[MODE:]DB|off  between the windows, a frame this much stronger\n");
    fprintf(stream, "                            makes both lost (fg25: 2.5; em4: off)\n");
    fprintf(stream, "  --medium-adjacent-db=[MODE:]DB|off  a frame on the next channel interferes this much\n");
    fprintf(stream, "                            weaker (fg25: 10; em4: off)\n");
    fprintf(stream, "  --medium-loss=P           every frame a receiver would decode is destroyed with probability P\n"
                    "                            (independent per receiver and frame, as ns-3's --loss; 0)\n");
    fprintf(stream, "  --medium-log=FILE         central: one line per frame and receiver (t_end_us src dst\n");
    fprintf(stream, "                            frame_id outcome rx_dbm sinr_min_db)\n");
    fprintf(stream, "  Preamble sniffing (central medium, docs/MEDIUM.md §10):\n");
    fprintf(stream, "  --sniff-detect=preamble|frame  what part of a frame a sniff can detect (preamble)\n");
    fprintf(stream, "  --sniff-dwell-bits=N      bits of a frame a sniff must observe for one detection try (7)\n");
    fprintf(stream, "  --sniff-threshold=DBM|off lowest received power a sniff detects (off)\n");
    fprintf(stream, "  --sniff-curve=ber:N|SINR:P,...  detection probability of one try against its lowest\n");
    fprintf(stream, "                            SINR: the medium's bit error rate over N bits, or a table\n");
    fprintf(stream, "                            (-2.8:0,0.6:0.002,3.9:0.65,5.4:1)\n");
    fprintf(stream, "  --sniff-sfd-bits=N        bits of SFD at the end of each SHR (16)\n");
    fprintf(stream, "  --sniff-margin-bits=N     preamble bits a detection must leave before the SFD for the\n");
    fprintf(stream, "                            frame to be received (22)\n");
    fprintf(stream, "  --sniff-settle-us=N       receiver settling time: a sniff detects from N us after its\n");
    fprintf(stream, "                            receiver is on (225)\n");
    fprintf(stream, "  --sniff-log=FILE          one line per sniff (t_req node id chan t0 t1 outcome t frame_id\n");
    fprintf(stream, "                            src rssi_dbm sinr_db locked)\n");
    prop_print_help(stream);
    chan_help(stream);
    intf_usage(stream);
    exit(exit_code);
}

enum {
    OPT_VTIME = 256,
    OPT_VTIME_END,
    OPT_POWER,
    OPT_POWER_NODE,
    OPT_POWER_MODE,
    OPT_POWER_OUT,
    OPT_POWER_LOG,
    OPT_POWER_CELL,
    OPT_DRIFT,
    OPT_DRIFT_RANDOM,
    OPT_VTIME_KILL,
    OPT_VTIME_START,
    OPT_VTIME_POWER_UP,
    OPT_VTIME_CUT,
    OPT_VTIME_FRAMES,
    OPT_VTIME_CONTROL,
    OPT_VTIME_LINK,
    OPT_VTIME_DROP,
    OPT_MEDIUM,
    OPT_MEDIUM_TX_REF,
    OPT_MEDIUM_TX_MAX,
    OPT_MEDIUM_LOCK_SNR,
    OPT_MEDIUM_CAPTURE_DB,
    OPT_MEDIUM_MODEL,
    OPT_MEDIUM_INT_CURVE,
    OPT_MEDIUM_CAPTURE_EARLY,
    OPT_MEDIUM_CAPTURE_SFD,
    OPT_MEDIUM_BREAK_DB,
    OPT_MEDIUM_ADJACENT_DB,
    OPT_MEDIUM_LOSS,
    OPT_MEDIUM_LOG,
    OPT_MEDIUM_PRESET,
    OPT_DESCRIBE,
    OPT_GROUP_FILE,
    OPT_SNIFF_DETECT,
    OPT_SNIFF_DWELL,
    OPT_SNIFF_THRESHOLD,
    OPT_SNIFF_CURVE,
    OPT_SNIFF_SFD,
    OPT_SNIFF_SETTLE,
    OPT_SNIFF_MARGIN,
    OPT_SNIFF_LOG,
    OPT_CHAN,                   // the options of sim_channel.c, by name
    OPT_INTF,                   // the options of sim_interf.c (intf_option_names)
};

// --vtime-drop=SRC:DST:SEQ:FROM:TO: frames from SRC with this MAC sequence
// number are not delivered to DST between FROM and TO s (a lost frame and its
// MAC retries)
struct vt_drop {
    int src, dst, seq;
    uint64_t from, to;
};
#define VT_DROP_MAX 64
static struct vt_drop vt_drops[VT_DROP_MAX];
static int vt_drops_n;
static uint64_t vt_dropped;

// Node events in virtual time (--vtime-kill, --vtime-start and
// --vtime-power-up, --vtime-cut, --vtime-link),
// applied at their time before any wake at that time, in time order (option
// order on a tie).
enum { VT_CTL_KILL, VT_CTL_START, VT_CTL_CUT, VT_CTL_LINK };
struct vt_ctl {
    uint64_t t;
    int kind;
    int node;                   // CUT: the receiver
    int src;                    // CUT: the sender
    int slot;                   // START: the connection of the instance, 0 until its hello
    bool first;                 // START: --vtime-power-up, the node's first instance
    float rssi;                 // LINK: the new RSSI of the link, both directions
};
#define VT_CTL_MAX 256
static struct vt_ctl vt_ctls[VT_CTL_MAX];
static int vt_ctls_n, vt_ctls_next;
static int vt_starts_n;
static const char *vt_frames_path;
// --vtime-control (docs/SCENARIOS.md): a scenario driver steps the run
static const char *ctl_path;
static int ctl_listen_fd = -1, ctl_fd = -1;
static uint64_t ctl_until;                      // events up to this time may run
static FILE *ctl_in;
static uint64_t vt_airtime_us;                  // central medium: airtime of every frame sent
static bool vt_uncharged;                       // this wake only answers measurement requests ("ask")
static pid_t ctl_owner;                         // the server process (not a child of it)
#define SIM_CTL_TEXT_MAX 65000                  // longest "xL" text (app_wshwsim/sl_rf_driver.c reads up to 64 KiB)
static FILE *vt_frames_file;

static uint64_t seconds_to_us(const char *opt, const char *str)
{
    char *end;
    double d = strtod(str, &end);

    FATAL_ON(*end || !(d >= 0) || d > 1e12, 1, "%s: bad number of seconds: %s", opt, str);
    return (uint64_t)(d * 1e6 + 0.5);
}

static int node_id(const char *opt, const char *str)
{
    char *end;
    long id = strtol(str, &end, 0);

    FATAL_ON(*end || id < 0 || id >= MAX_NODES, 1, "%s: bad node ID: %s", opt, str);
    return id;
}

// "ID:SECONDS" (kill, start, power-up), "SRC:DST:SECONDS" (cut) or "A:B:SECONDS:RSSI" (link)
static void vt_ctl_add(int kind, const char *opt, char *arg, bool first)
{
    struct vt_ctl *c = &vt_ctls[vt_ctls_n];
    char *f[4];
    int n = 0, want = kind == VT_CTL_LINK ? 4 : kind == VT_CTL_CUT ? 3 : 2;
    char *p, *end;

    FATAL_ON(vt_ctls_n >= VT_CTL_MAX, 1, "%s: at most %d node events", opt, VT_CTL_MAX);
    for (p = strtok(arg, ":"); p && n < 4; p = strtok(NULL, ":"))
        f[n++] = p;
    FATAL_ON(n != want || p, 1, "%s: expected %s", opt,
             want == 4 ? "A:B:SECONDS:RSSI" : want == 3 ? "SRC:DST:SECONDS" : "ID:SECONDS");
    memset(c, 0, sizeof(*c));
    c->kind = kind;
    c->node = node_id(opt, f[want >= 3 ? 1 : 0]);
    c->src = want >= 3 ? node_id(opt, f[0]) : -1;
    c->t = seconds_to_us(opt, f[want == 4 ? 2 : want - 1]);
    if (want == 4) {
        c->rssi = strtof(f[3], &end);
        FATAL_ON(*end || c->rssi > 0 || c->rssi < -200, 1, "%s: bad RSSI: %s", opt, f[3]);
    }
    c->first = first;
    if (kind == VT_CTL_START && !first)
        vt_starts_n++;
    vt_ctls_n++;
}


// Clock drift per node (EM3 step 5), sent in the welcome: --drift=[IDS:]PPM,
// the last one that names the node wins; else --drift-random.
static struct {
    uint64_t mask[MAX_NODES / 64];
    int32_t ppb;
} drift_specs[64];
static int drift_specs_n;
static int32_t drift_random_ppb;

static int32_t ppm_to_ppb(const char *opt, const char *str)
{
    char *end;
    double ppm = strtod(str, &end);

    FATAL_ON(*end || !(ppm > -1e5 && ppm < 1e5), 1, "%s: expected ppm between -100000 and 100000, got %s", opt, str);
    return (int32_t)(ppm * 1000 + (ppm < 0 ? -0.5 : 0.5));
}

static void drift_add(char *spec)
{
    char *colon = strchr(spec, ':');

    FATAL_ON(drift_specs_n >= 64, 1, "--drift: at most 64");
    if (colon) {
        *colon = '\0';
        FATAL_ON(bitmap_parse(spec, drift_specs[drift_specs_n].mask, MAX_NODES / 64), 1, "--drift: bad node list %s", spec);
        *colon = ':';
    } else {
        memset(drift_specs[drift_specs_n].mask, 0xff, sizeof(drift_specs[drift_specs_n].mask));
    }
    drift_specs[drift_specs_n].ppb = ppm_to_ppb("--drift", colon ? colon + 1 : spec);
    drift_specs_n++;
}

static uint64_t drift_mix64(uint64_t z)
{
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

static int32_t drift_of(int node, uint64_t seed)
{
    int32_t ppb = 0;
    int i;

    if (drift_random_ppb) {
        // Uniform in [-max, +max], from the seed and the node only (its own
        // stream: not the radio's loss draws, not the port's random source)
        uint64_t r = drift_mix64(seed * 0x9e3779b97f4a7c15ULL + drift_mix64(0x64726966ULL + node));
        ppb = (int32_t)((int64_t)(r % (2 * (uint64_t)drift_random_ppb + 1)) - drift_random_ppb);
    }
    for (i = 0; i < drift_specs_n; i++)
        if (bitmap_get(node, drift_specs[i].mask, MAX_NODES / 64))
            ppb = drift_specs[i].ppb;
    return ppb;
}

// Presets of the central medium (docs/MEDIUM.md §14): their options go before
// the command line's own, so that every option given overrides them.
static const struct med_preset {
    const char *name;
    const char *opts[10];
} med_presets[] = {
    // The behaviour of the defaults before the presets (emulator e446b87)
    { "basic", { NULL } },
    // Calibrated on the FG25 kits, or fitted to reproduce the empirical curve
    { "calibrated", { "--medium-rx-model=layered", "--medium-adjacent-table=10/40",
                      "--medium-capture-model=smooth", "--medium-tx-max=16.9", NULL } },
    // calibrated + illustrative indoor values (slow shadowing, RSSI offset)
    { "realistic", { "--medium-rx-model=layered", "--medium-adjacent-table=10/40",
                     "--medium-capture-model=smooth", "--medium-tx-max=16.9", "--medium-shadowing=2,60",
                     "--rssi-offset=0.9", NULL } },
    // The MVP's reception model (MEDIUM.md §2.1)
    { "em4", { "--medium-model=em4", NULL } },
};
#define MED_PRESET_DEFAULT 1    // calibrated

static const struct med_preset *med_preset_find(const char *name)
{
    for (size_t i = 0; i < ARRAY_SIZE(med_presets); i++)
        if (!strcmp(med_presets[i].name, name))
            return &med_presets[i];
    FATAL(1, "--medium-preset: expected basic, calibrated, realistic or em4: %s", name);
}

void parse_commandline(struct ctxt *ctxt, int argc, char *argv[])
{
    const char *opts_short = "hlvf::g:";
    static const struct option opts_long[] = {
        { "group", required_argument, 0,  'g' },
        { "fpath", optional_argument, 0, 'f'},
        { "dump",  no_argument,       0,  'l' },
        { "help",  no_argument,       0,  'h' },
        { "verbose", no_argument,     0,  'v' },
        { "vtime", required_argument, 0, OPT_VTIME },
        { "vtime-end", required_argument, 0, OPT_VTIME_END },
        { "power", required_argument, 0, OPT_POWER },
        { "power-node", required_argument, 0, OPT_POWER_NODE },
        { "power-mode", required_argument, 0, OPT_POWER_MODE },
        { "power-out", required_argument, 0, OPT_POWER_OUT },
        { "power-log", required_argument, 0, OPT_POWER_LOG },
        { "power-cell", required_argument, 0, OPT_POWER_CELL },
        { "drift", required_argument, 0, OPT_DRIFT },
        { "drift-random", required_argument, 0, OPT_DRIFT_RANDOM },
        { "vtime-kill", required_argument, 0, OPT_VTIME_KILL },
        { "vtime-start", required_argument, 0, OPT_VTIME_START },
        { "vtime-power-up", required_argument, 0, OPT_VTIME_POWER_UP },
        { "vtime-cut", required_argument, 0, OPT_VTIME_CUT },
        { "vtime-frames", required_argument, 0, OPT_VTIME_FRAMES },
        { "vtime-control", required_argument, 0, OPT_VTIME_CONTROL },
        { "vtime-link", required_argument, 0, OPT_VTIME_LINK },
        { "vtime-drop", required_argument, 0, OPT_VTIME_DROP },
        { "medium", required_argument, 0, OPT_MEDIUM },
        { "medium-tx-ref", required_argument, 0, OPT_MEDIUM_TX_REF },
        { "medium-tx-max", required_argument, 0, OPT_MEDIUM_TX_MAX },
        { "medium-lock-snr", required_argument, 0, OPT_MEDIUM_LOCK_SNR },
        { "medium-capture-db", required_argument, 0, OPT_MEDIUM_CAPTURE_DB },
        { "medium-model", required_argument, 0, OPT_MEDIUM_MODEL },
        { "medium-int-curve", required_argument, 0, OPT_MEDIUM_INT_CURVE },
        { "medium-capture-early-bits", required_argument, 0, OPT_MEDIUM_CAPTURE_EARLY },
        { "medium-capture-sfd", required_argument, 0, OPT_MEDIUM_CAPTURE_SFD },
        { "medium-break-db", required_argument, 0, OPT_MEDIUM_BREAK_DB },
        { "medium-adjacent-db", required_argument, 0, OPT_MEDIUM_ADJACENT_DB },
        { "medium-loss", required_argument, 0, OPT_MEDIUM_LOSS },
        { "medium-log", required_argument, 0, OPT_MEDIUM_LOG },
        { "medium-preset", required_argument, 0, OPT_MEDIUM_PRESET },
        { "describe", required_argument, 0, OPT_DESCRIBE },
        { "group-file", required_argument, 0, OPT_GROUP_FILE },
        { "sniff-detect", required_argument, 0, OPT_SNIFF_DETECT },
        { "sniff-dwell-bits", required_argument, 0, OPT_SNIFF_DWELL },
        { "sniff-threshold", required_argument, 0, OPT_SNIFF_THRESHOLD },
        { "sniff-curve", required_argument, 0, OPT_SNIFF_CURVE },
        { "sniff-sfd-bits", required_argument, 0, OPT_SNIFF_SFD },
        { "sniff-settle-us", required_argument, 0, OPT_SNIFF_SETTLE },
        { "sniff-margin-bits", required_argument, 0, OPT_SNIFF_MARGIN },
        { "sniff-log", required_argument, 0, OPT_SNIFF_LOG },
        // Interference and spectrum (docs/MEDIUM.md §13): sim_interf.c
        { "interferer", required_argument, 0, OPT_INTF },
        { "interferer-file", required_argument, 0, OPT_INTF },
        { "interferer-log", required_argument, 0, OPT_INTF },
        { "medium-adjacent-table", required_argument, 0, OPT_INTF },
        { "medium-capture-model", required_argument, 0, OPT_INTF },
        { "medium-capture-smooth", required_argument, 0, OPT_INTF },
        { "medium-int-curve-presync", required_argument, 0, OPT_INTF },
        { "sniff-adjacent", required_argument, 0, OPT_INTF },
        { "sniff-energy", required_argument, 0, OPT_INTF },
        { "sniff-energy-us", required_argument, 0, OPT_INTF },
        { "sniff-false", required_argument, 0, OPT_INTF },
        { 0,       0,                 0,   0  }
    };
    // The propagation (sim_propagation.c) and channel (sim_channel.c)
    // options follow the server's own
    static struct option all_opts[ARRAY_SIZE(opts_long) + 48];
    uint64_t mask[MAX_NODES / 64];
    char *end;
    double d;
    int opt, n, longindex = 0;

    for (n = 0; opts_long[n].name; n++)
        all_opts[n] = opts_long[n];
    for (int k = 0; prop_long_options[k].name; k++)
        all_opts[n++] = prop_long_options[k];
    for (int k = 0; chan_option_names[k]; k++)
        all_opts[n++] = (struct option){ chan_option_names[k], required_argument, 0, OPT_CHAN };
    BUG_ON(n >= (int)ARRAY_SIZE(all_opts));

    // First pass, on a copy (getopt_long permutes its array): the preset, and
    // what decides whether one applies (--vtime, --medium, --medium-model)
    {
        char **scan = calloc(argc + 1, sizeof(*scan));
        const char *preset_arg = NULL, *how;
        bool vtime = false, legacy = false, model_em4 = false;
        const struct med_preset *mp = NULL;
        static char preset_opts[512];
        char **argv2;
        int argc2 = 0;

        FATAL_ON(!scan, 1, "calloc: %m");
        memcpy(scan, argv, argc * sizeof(*scan));
        opterr = 0;
        optind = 0;
        while ((opt = getopt_long(argc, scan, opts_short, all_opts, &longindex)) != -1) {
            if (opt == OPT_MEDIUM_PRESET)
                preset_arg = optarg;
            else if (opt == OPT_VTIME)
                vtime = true;
            else if (opt == OPT_MEDIUM)
                legacy = !strcmp(optarg, "legacy");
            else if (opt == OPT_MEDIUM_MODEL)
                model_em4 = !strcmp(optarg, "em4");
        }
        free(scan);
        opterr = 1;
        optind = 0;
        if (preset_arg) {
            mp = med_preset_find(preset_arg);
            FATAL_ON(!vtime, 1, "--medium-preset needs --vtime");
            FATAL_ON(legacy && mp != &med_presets[0], 1,
                     "--medium-preset=%s needs the central medium (not --medium=legacy)", mp->name);
            how = "--medium-preset";
        } else if (model_em4) {
            // --medium-model=em4 alone keeps meaning the MVP (MEDIUM.md §2.1)
            mp = med_preset_find("em4");
            how = "implied by --medium-model=em4";
        } else {
            mp = &med_presets[MED_PRESET_DEFAULT];
            how = "the default";
        }
        if (!vtime || legacy)
            mp = NULL;
        argv2 = calloc(argc + ARRAY_SIZE(mp->opts) + 1, sizeof(*argv2));
        FATAL_ON(!argv2, 1, "calloc: %m");
        argv2[argc2++] = argv[0];
        for (int k = 0; mp && mp->opts[k]; k++) {
            argv2[argc2++] = (char *)mp->opts[k];
            if (k)
                strcat(preset_opts, " ");
            strcat(preset_opts, mp->opts[k]);
        }
        for (int k = 1; k < argc; k++)
            argv2[argc2++] = argv[k];
        argc = argc2;
        argv = argv2;           // kept for the run (getopt's optarg points into it)
        if (mp)
            med_set_preset_note(mp->name, how, preset_opts);
    }

    while ((opt = getopt_long(argc, argv, opts_short, all_opts, &longindex)) != -1) {
        switch (opt) {
            case OPT_MEDIUM_PRESET:
                break;          // the first pass applied it
            case OPT_DESCRIBE: {
                struct describe_preset dp[ARRAY_SIZE(med_presets)];

                FATAL_ON(strcmp(optarg, "json"), 1, "--describe: expected json: %s", optarg);
                for (size_t k = 0; k < ARRAY_SIZE(med_presets); k++)
                    dp[k] = (struct describe_preset){ med_presets[k].name, med_presets[k].opts };
                describe_json(stdout, all_opts, dp, ARRAY_SIZE(med_presets), MED_PRESET_DEFAULT);
                exit(0);
            }
            case OPT_CHAN:
                chan_set_option(all_opts[longindex].name, optarg);
                break;
            case 'g':
                FATAL_ON(bitmap_parse(optarg, mask, MAX_NODES / 64), 1,
                         "-g %s: bad node list (IDs 0 to %d, e.g. 2,5:-67.5 or 0-3)", optarg, MAX_NODES - 1);
                ctxt->groups = realloc(ctxt->groups, (ctxt->num_groups + 1) * sizeof(*ctxt->groups));
                FATAL_ON(!ctxt->groups, 1, "realloc: %m");
                ctxt->groups[ctxt->num_groups++] = optarg;
                break;
            case OPT_GROUP_FILE: {
                // One -g argument per line: large topologies exceed the
                // command line's size limit (docs/SCALE.md). A line is
                // "IDS[:RSSI]", optionally after "-g" or "--group=";
                // blank lines and '#' comments (whole or end of line) are
                // skipped.
                FILE *gf = fopen(optarg, "r");
                char *line = NULL, *l, *hash;
                size_t cap = 0;
                ssize_t n;
                int lineno = 0;

                FATAL_ON(!gf, 1, "--group-file: %s: %m", optarg);
                while ((n = getline(&line, &cap, gf)) >= 0) {
                    lineno++;
                    if ((hash = strchr(line, '#')))
                        *hash = '\0';
                    l = line;
                    while (*l == ' ' || *l == '\t')
                        l++;
                    n = strlen(l);
                    while (n && (l[n - 1] == '\n' || l[n - 1] == '\r' || l[n - 1] == ' ' || l[n - 1] == '\t'))
                        l[--n] = '\0';
                    if (!n)
                        continue;
                    if (!strncmp(l, "--group=", 8)) {
                        l += 8;
                    } else if (!strncmp(l, "-g", 2)) {
                        l += 2;
                        while (*l == ' ' || *l == '\t')
                            l++;
                    }
                    FATAL_ON(!*l || strpbrk(l, " \t"), 1,
                             "--group-file: %s:%d: one IDS[:RSSI] per line (e.g. 2,5:-67.5), got \"%s\"",
                             optarg, lineno, l);
                    FATAL_ON(bitmap_parse(l, mask, MAX_NODES / 64), 1,
                             "--group-file: %s:%d: bad node list in \"%s\" (IDs 0 to %d, e.g. 2,5:-67.5 or 0-3)",
                             optarg, lineno, l, MAX_NODES - 1);
                    ctxt->groups = realloc(ctxt->groups, (ctxt->num_groups + 1) * sizeof(*ctxt->groups));
                    FATAL_ON(!ctxt->groups, 1, "realloc: %m");
                    ctxt->groups[ctxt->num_groups++] = strdup(l);
                }
                free(line);
                fclose(gf);
                break;
            }
            case 'f':
               if (optarg == NULL && optind < argc
                    && argv[optind][0] != '-')
                {
                    optarg = argv[optind++];
                }
                if (optarg == NULL)
                {
                    sprintf(ctxt->dump_path, "%s", "/tmp/graph.txt");
                } else {
                    strncpy(ctxt->dump_path, optarg, sizeof(ctxt->dump_path)-2);
                    ctxt->dump_path[sizeof(ctxt->dump_path)-1] = '\n';
                }
                ctxt->dump2file = true;
                break;
            case 'l':
                ctxt->dump = true;
                break;
            case OPT_VTIME:
                ctxt->vtime_nodes = strtol(optarg, &end, 0);
                FATAL_ON(*end || ctxt->vtime_nodes < 1 || ctxt->vtime_nodes > MAX_NODES, 1,
                         "--vtime: expected a number of nodes from 1 to %d: %s", MAX_NODES, optarg);
                break;
            case OPT_VTIME_END:
                d = strtod(optarg, &end);
                FATAL_ON(*end || !(d >= 0) || d > 1e12, 1, "--vtime-end: bad number of seconds: %s", optarg);
                ctxt->vtime_end_us = (uint64_t)(d * 1e6 + 0.5);
                ctxt->has_vtime_end = true;
                break;
            case OPT_POWER:
                pwr_set_profile(optarg);
                break;
            case OPT_POWER_NODE:
                pwr_add_node_profile(optarg);
                break;
            case OPT_POWER_MODE:
                pwr_set_mode(optarg);
                break;
            case OPT_POWER_OUT:
                pwr_set_out(optarg);
                break;
            case OPT_POWER_LOG:
                pwr_set_log(optarg);
                break;
            case OPT_POWER_CELL:
                pwr_set_cell(optarg);
                break;
            case OPT_DRIFT:
                drift_add(optarg);
                break;
            case OPT_DRIFT_RANDOM:
                drift_random_ppb = ppm_to_ppb("--drift-random", optarg);
                FATAL_ON(drift_random_ppb < 0, 1, "--drift-random: expected a positive bound");
                break;
            case OPT_VTIME_KILL:
                vt_ctl_add(VT_CTL_KILL, "--vtime-kill", optarg, false);
                break;
            case OPT_VTIME_START:
                vt_ctl_add(VT_CTL_START, "--vtime-start", optarg, false);
                break;
            case OPT_VTIME_POWER_UP:
                vt_ctl_add(VT_CTL_START, "--vtime-power-up", optarg, true);
                break;
            case OPT_VTIME_CUT:
                vt_ctl_add(VT_CTL_CUT, "--vtime-cut", optarg, false);
                break;
            case OPT_VTIME_DROP: {
                char *f[5], *q;
                int n = 0;
                FATAL_ON(vt_drops_n >= VT_DROP_MAX, 1, "--vtime-drop: at most %d", VT_DROP_MAX);
                for (q = strtok(optarg, ":"); q && n < 5; q = strtok(NULL, ":"))
                    f[n++] = q;
                FATAL_ON(n != 5 || q, 1, "--vtime-drop: expected SRC:DST:SEQ:FROM:TO");
                vt_drops[vt_drops_n].src = node_id("--vtime-drop", f[0]);
                vt_drops[vt_drops_n].dst = node_id("--vtime-drop", f[1]);
                vt_drops[vt_drops_n].seq = strtol(f[2], &end, 0);
                FATAL_ON(*end || vt_drops[vt_drops_n].seq < 0 || vt_drops[vt_drops_n].seq > 255, 1, "--vtime-drop: bad sequence number %s", f[2]);
                vt_drops[vt_drops_n].from = seconds_to_us("--vtime-drop", f[3]);
                vt_drops[vt_drops_n].to = seconds_to_us("--vtime-drop", f[4]);
                vt_drops_n++;
                break;
            }
            case OPT_VTIME_LINK:
                vt_ctl_add(VT_CTL_LINK, "--vtime-link", optarg, false);
                break;
            case OPT_VTIME_CONTROL:
                FATAL_ON(strlen(optarg) >= sizeof(((struct sockaddr_un *)0)->sun_path), 1,
                         "--vtime-control: path too long: %s", optarg);
                ctl_path = optarg;
                break;
            case OPT_VTIME_FRAMES:
                vt_frames_path = optarg;
                break;
            case OPT_MEDIUM:
                med_set_mode(optarg);
                break;
            case OPT_MEDIUM_TX_REF:
                med_set_tx_ref(optarg);
                break;
            case OPT_MEDIUM_TX_MAX:
                med_set_tx_max(optarg);
                break;
            case OPT_MEDIUM_LOCK_SNR:
                med_set_lock_snr(optarg);
                break;
            case OPT_MEDIUM_CAPTURE_DB:
                med_set_capture_db(optarg);
                break;
            case OPT_MEDIUM_MODEL:
                med_set_model(optarg);
                break;
            case OPT_MEDIUM_INT_CURVE:
                med_set_int_curve(optarg);
                break;
            case OPT_MEDIUM_CAPTURE_EARLY:
                med_set_capture_early(optarg);
                break;
            case OPT_MEDIUM_CAPTURE_SFD:
                med_set_capture_sfd(optarg);
                break;
            case OPT_MEDIUM_BREAK_DB:
                med_set_break_db(optarg);
                break;
            case OPT_MEDIUM_ADJACENT_DB:
                med_set_adjacent_db(optarg);
                break;
            case OPT_MEDIUM_LOSS:
                med_set_loss(optarg);
                break;
            case OPT_MEDIUM_LOG:
                med_set_log(optarg);
                break;
            case OPT_SNIFF_DETECT:
                med_set_sniff_detect(optarg);
                break;
            case OPT_SNIFF_DWELL:
                med_set_sniff_dwell(optarg);
                break;
            case OPT_SNIFF_THRESHOLD:
                med_set_sniff_threshold(optarg);
                break;
            case OPT_SNIFF_CURVE:
                med_set_sniff_curve(optarg);
                break;
            case OPT_SNIFF_SFD:
                med_set_sniff_sfd(optarg);
                break;
            case OPT_SNIFF_SETTLE:
                med_set_sniff_settle(optarg);
                break;
            case OPT_INTF:
                intf_option(all_opts[longindex].name, optarg);
                break;
            case OPT_SNIFF_MARGIN:
                med_set_sniff_margin(optarg);
                break;
            case OPT_SNIFF_LOG:
                med_set_sniff_log(optarg);
                break;
            case 'h':
                print_help(stdout, 0);
                break;
            case 'v':
                verbose = true;
                break;
            case '?':
                print_help(stderr, 1);
                break;
            default:
                prop_option(opt, optarg);
                break;
        }
    }
    FATAL_ON(ctxt->has_vtime_end && !ctxt->vtime_nodes, 1, "--vtime-end needs --vtime");
    FATAL_ON(pwr_enabled() && !ctxt->vtime_nodes, 1, "--power needs --vtime");
    FATAL_ON((drift_specs_n || drift_random_ppb) && !ctxt->vtime_nodes, 1, "--drift needs --vtime");
    FATAL_ON((vt_ctls_n || vt_frames_path || vt_drops_n) && !ctxt->vtime_nodes, 1,
             "--vtime-kill, --vtime-start, --vtime-power-up, --vtime-cut, --vtime-link and --vtime-frames need --vtime");
    FATAL_ON(med_options_given() && !ctxt->vtime_nodes, 1, "--medium needs --vtime");
    if (ctxt->vtime_nodes)
        med_default_central();
    FATAL_ON(med_tuning_given() && !med_central(), 1,
             "--medium-tx-ref, --medium-tx-max, --medium-lock-snr, --medium-model, --medium-int-curve, --medium-capture-*, "
             "--medium-break-db, --medium-adjacent-*, --medium-log, --sniff-* and --interferer* need the central "
             "medium (not --medium=legacy)");
    chan_options_done();
    FATAL_ON(chan_options_given() && !med_central(), 1,
             "--medium-rx-model, --medium-rx-curve, --medium-combine, --medium-fading, --medium-shadowing, "
             "--medium-node-share, --medium-reciprocal and --medium-burst need the central medium (--vtime, not "
             "--medium=legacy)");
    // The ledger follows each node's radio and MCU without gaps (a node
    // powered up late: its ledger starts at its first wake)
    for (int i = 0; i < vt_ctls_n; i++)
        FATAL_ON(pwr_enabled() && (vt_ctls[i].kind == VT_CTL_KILL || (vt_ctls[i].kind == VT_CTL_START && !vt_ctls[i].first)), 1,
                 "--power cannot be combined with --vtime-kill or --vtime-start");
    // Stable sort: ties keep the option order (qsort is not stable)
    for (int i = 1; i < vt_ctls_n; i++)
        for (int j = i; j > 0 && vt_ctls[j].t < vt_ctls[j - 1].t; j--) {
            struct vt_ctl tmp = vt_ctls[j];
            vt_ctls[j] = vt_ctls[j - 1];
            vt_ctls[j - 1] = tmp;
        }
    prop_check_options(med_central());
    if (med_central()) {
        float tx_max;
        int mode = med_tx_max(&tx_max);

        FATAL_ON(mode == 0 && !pwr_enabled(), 1, "--medium-tx-max=profile needs --power (the profiles' TX tables)");
        if (mode == 1 && prop_enabled())
            prop_limit_tx(tx_max);
    }
    FATAL_ON(ctxt->dump && !ctxt->num_groups && !prop_enabled(), 1, "No graph to dump");
    if (optind >= argc)
        FATAL(1, "Expected argument: socket path");
    if (optind + 1 < argc)
        FATAL(1, "Too many arguments argument: %s", argv[optind + 1]);
    // A UNIX socket path is at most 107 bytes (sun_path, with its NUL)
    FATAL_ON(strlen(argv[optind]) >= sizeof(ctxt->addr.sun_path), 1,
             "socket path too long: %zu bytes, the limit of a UNIX socket path is %zu: %s\n"
             "Give a shorter path, e.g. in /tmp (run.sh: SOCK=/tmp/<name>.sock; its default is short)",
             strlen(argv[optind]), sizeof(ctxt->addr.sun_path) - 1, argv[optind]);
    strcpy(ctxt->addr.sun_path, argv[optind]);
}

// Build the graph and the RSSI map from the -g options. The RSSI map lives in
// shared memory, whose names depend on the socket path: this runs after both.
static void apply_groups(struct ctxt *ctxt)
{
    struct link_parse lp = { };
    uint64_t mask[MAX_NODES / 64];
    int i, j, max;

    lp.origin = calloc((long)MAX_NODES * MAX_NODES, 1);
    FATAL_ON(!lp.origin, 1, "calloc: %m");
    for (i = 0; i < ctxt->num_groups; i++) {
        FATAL_ON(bitmap_parse(ctxt->groups[i], mask, MAX_NODES / 64), 1,
                 "-g %s: bad node list (IDs 0 to %d, e.g. 2,5 or 0-3)", ctxt->groups[i], MAX_NODES - 1);
        FATAL_ON(rssi_map_parse_for_one_group(&lp, ctxt->groups[i]), 1,
                 "-g %s: bad option (IDS[:RSSI], IDs 0 to %d, e.g. 2,5:-67.5 or 0-3)", ctxt->groups[i], MAX_NODES - 1);
        graph_apply_mask(ctxt->node_graph, mask);
    }
    if (lp.overrides)
        WARN("-g: %d link values set again with another value by a later -g (the later one applies)", lp.overrides);
    max = ctxt->num_groups ? graph_get_num_nodes(ctxt) : 0;
    lp.asymmetric = 0;
    for (i = 0; i < max; i++)
        for (j = i + 1; j < max; j++)
            if (sim_link(nodes_infos_flat_map, i, j)->rssi != sim_link(nodes_infos_flat_map, j, i)->rssi)
                lp.asymmetric++;
    if (lp.asymmetric)
        INFO("-g: %d asymmetric links (a different value in each direction)", lp.asymmetric);
    // With --positions, who hears whom comes from the model (plus the -g pairs)
    if (!ctxt->num_groups && !prop_enabled())
        memset(ctxt->node_graph, 0xFF, sizeof(ctxt->node_graph));
    prop_build(ctxt->node_graph, nodes_infos_flat_map, lp.origin);
    // External interferers placed with pos= get their levels from the positions (MEDIUM.md §13.1)
    if (prop_enabled())
        intf_set_position_model(prop_point_dbm);
    free(lp.origin);
    for (i = 0; i < MAX_NODES; i++)
        bitmap_clr(i, ctxt->node_graph[i], MAX_NODES / 64);
    if (ctxt->dump) {
        graph_dump(ctxt);
        if (ctxt->dump2file)
            graph_dump_to_file(ctxt, ctxt->dump_path);
    }
}

// A connection, and the node it carries once its hello arrived.
struct conn {
    int node;                   // -1 until the hello
    uint16_t radio_if;          // client of the radio interface (SIM_HELLO_RADIO_IF): its version, else 0
};

static struct pollfd fds[MAX_NODES + 1];
static struct conn conns[MAX_NODES + 1];
static int slot_of_node[MAX_NODES];   // index in fds, or 0 if not connected
static int fd_limit;
static int num_connected;

static void conn_close(int slot)
{
    if (conns[slot].node >= 0) {
        INFO("node %d disconnected", conns[slot].node);
        if (slot_of_node[conns[slot].node] == slot)
            slot_of_node[conns[slot].node] = 0;
        for (int i = 0; i < vt_ctls_n; i++)
            if (vt_ctls[i].kind == VT_CTL_START && vt_ctls[i].slot == slot)
                vt_ctls[i].slot = -1;   // the instance is gone: its start fails
        num_connected--;
    } else {
        DEBUG("Disconnect fd %d", fds[slot].fd);
    }
    close(fds[slot].fd);
    fds[slot].fd = -1;
    fds[slot].events = 0;
    conns[slot].node = -1;
    conns[slot].radio_if = 0;
}

// The first --vtime-start of node that has no connection yet takes slot.
static bool vt_start_take(int node, int slot)
{
    int i;

    for (i = 0; i < vt_ctls_n; i++)
        if (vt_ctls[i].kind == VT_CTL_START && !vt_ctls[i].first && vt_ctls[i].node == node && !vt_ctls[i].slot) {
            vt_ctls[i].slot = slot;
            return true;
        }
    return false;
}

// A hello must say whether its node runs on virtual time, and that must match
// the server's mode. In virtual time (vtime_nodes > 0), hellos beyond the
// expected number are refused: several can arrive in one poll() pass.
static void conn_hello(int slot, const void *buf, int len, int vtime_nodes)
{
    const struct sim_hello *hello = buf;
    bool vtime = vtime_nodes > 0;

    if (len != sizeof(*hello) || memcmp(hello->magic, "xh", 2)) {
        WARN("fd %d: expected a hello, dropping the connection", fds[slot].fd);
        conn_close(slot);
    } else if (hello->flags & ~SIM_HELLO_KNOWN) {
        WARN("fd %d: node %d: unknown hello flags 0x%04x: refused", fds[slot].fd, hello->node_id,
             hello->flags & ~SIM_HELLO_KNOWN);
        conn_close(slot);
    } else if ((hello->flags & SIM_HELLO_RADIO_IF) &&
               (hello->version < SIM_RADIO_IF_VERSION_MIN || hello->version > SIM_RADIO_IF_VERSION_MAX)) {
        WARN("fd %d: node %d: radio interface version %u, this server speaks %d to %d: refused", fds[slot].fd,
             hello->node_id, hello->version, SIM_RADIO_IF_VERSION_MIN, SIM_RADIO_IF_VERSION_MAX);
        conn_close(slot);
    } else if ((hello->flags & SIM_HELLO_RADIO_IF) &&
               (!vtime || !med_central() || !(hello->flags & SIM_HELLO_VTIME))) {
        WARN("fd %d: node %d: the radio interface needs virtual time and the central medium "
             "(--vtime, not --medium=legacy): refused", fds[slot].fd, hello->node_id);
        conn_close(slot);
    } else if (hello->node_id >= MAX_NODES) {
        WARN("fd %d: node ID %d out of range (max %d)", fds[slot].fd, hello->node_id, MAX_NODES - 1);
        conn_close(slot);
    } else if (slot_of_node[hello->node_id] && vtime && vt_start_take(hello->node_id, slot)) {
        // Another instance of a connected node, for --vtime-start: welcomed
        // at its start time
        conns[slot].node = hello->node_id;
        conns[slot].radio_if = (hello->flags & SIM_HELLO_RADIO_IF) ? hello->version : 0;
        num_connected++;
        fds[slot].events = 0;
        INFO("node %d connected (instance for a later start%s)", hello->node_id,
             conns[slot].radio_if ? ", radio interface" : "");
    } else if (slot_of_node[hello->node_id]) {
        WARN("fd %d: node %d is already connected", fds[slot].fd, hello->node_id);
        conn_close(slot);
    } else if (!(hello->flags & SIM_HELLO_VTIME) != !vtime) {
        WARN("fd %d: node %d runs on %s time, this server on %s time: refused", fds[slot].fd,
             hello->node_id, (hello->flags & SIM_HELLO_VTIME) ? "virtual" : "wall-clock",
             vtime ? "virtual" : "wall-clock");
        conn_close(slot);
    } else if (vtime && num_connected >= vtime_nodes + vt_starts_n) {
        WARN("fd %d: node %d: the %d nodes of --vtime are already connected: refused",
             fds[slot].fd, hello->node_id, vtime_nodes);
        conn_close(slot);
    } else {
        conns[slot].node = hello->node_id;
        conns[slot].radio_if = (hello->flags & SIM_HELLO_RADIO_IF) ? hello->version : 0;
        slot_of_node[hello->node_id] = slot;
        num_connected++;
        if (vtime)
            fds[slot].events = 0; // only hang-ups until the run starts
        if (conns[slot].radio_if)
            INFO("node %d connected (radio interface version %u)", hello->node_id, conns[slot].radio_if);
        else
            INFO("node %d connected", hello->node_id);
    }
}

static bool frame_check(int src, const void *buf, int len)
{
    const struct sim_frame_hdr *hdr = buf;

    if (len < (int)sizeof(*hdr) || memcmp(hdr->magic, "xx", 2) ||
        hdr->len != len - sizeof(*hdr)) {
        WARN("node %d: malformed frame (%d bytes), dropped", src, len);
        return false;
    }
    return true;
}

// Relay a frame to the sender's neighbours, stamped with the sender's ID.
static void relay(uint64_t node_graph[MAX_NODES][MAX_NODES / 64], int slot, void *buf, int len)
{
    struct sim_frame_hdr *hdr = buf;
    int src = conns[slot].node;
    int j, dst, ret;

    if (!frame_check(src, buf, len))
        return;
    hdr->src = src;
    for (dst = 0; dst < MAX_NODES; dst++) {
        j = slot_of_node[dst];
        if (!j || !bitmap_get(dst, node_graph[src], MAX_NODES / 64))
            continue;
        ret = send(fds[j].fd, buf, len, MSG_NOSIGNAL);
        if (ret != len) {
            WARN("node %d: send: %m", dst);
            conn_close(j);
        }
    }
}

static volatile sig_atomic_t stop = false;
static void signal_handler(int signal)
{
    stop = true;
}

static char buf[sizeof(struct sim_frame_hdr) + 4096];

static void listen_socket(struct ctxt *ctxt)
{
    int on = 1;
    int ret;

    fds[0].events = POLLIN;
    fds[0].fd = socket(AF_UNIX, SOCK_SEQPACKET, 0); // use SOCK_SEQPACKET or SOCK_STREAM
    FATAL_ON(fds[0].fd < 0, 1, "socket: %s: %m", ctxt->addr.sun_path);
    ret = setsockopt(fds[0].fd, SOL_SOCKET, SO_REUSEADDR, (char *)&on, sizeof(on));
    FATAL_ON(ret < 0, 1, "setsockopt: %s: %m", ctxt->addr.sun_path);
    unlink(ctxt->addr.sun_path);
    ret = bind(fds[0].fd, (struct sockaddr *)&ctxt->addr, sizeof(ctxt->addr));
    FATAL_ON(ret < 0, 1, "bind: %s: %m", ctxt->addr.sun_path);
    ret = listen(fds[0].fd, 4096);
    FATAL_ON(ret < 0, 1, "listen: %s: %m", ctxt->addr.sun_path);
}

// Accept connections and read messages: hellos, then frames (relayed in
// wall-clock mode). In virtual-time mode, returns once ctxt->vtime_nodes
// nodes said hello, without reading anything else.
static void serve(struct ctxt *ctxt)
{
    bool vtime = ctxt->vtime_nodes > 0;
    int i, ret, len;

    while (!stop) {
        if (vtime && num_connected == ctxt->vtime_nodes + vt_starts_n)
            return;
        ret = poll(fds, fd_limit, -1);
        if (ret < 0) {
            if (errno != EINTR)
                WARN("poll: %m");
            break;
        }
        if (fds[0].revents) {
            for (i = 1; i < fd_limit; i++) {
                if (fds[i].fd == -1) {
                    fds[i].events = POLLIN;
                    fds[i].fd = accept(fds[0].fd, NULL, NULL);
                    if (verbose)
                        DEBUG("Connect fd %d", fds[i].fd);
                    FATAL_ON(fds[i].fd < 0, 1, "accept: %m");
                    break;
                }
            }
            if (i == fd_limit)
                FATAL(1, "can't accept new node %d %d", i, fd_limit);
        }
        for (i = 1; i < fd_limit; i++) {
            if (fds[i].fd < 0 || !fds[i].revents)
                continue;
            if (vtime && conns[i].node >= 0) {
                // The conductor reads the nodes' messages once the run starts
                WARN("node %d disconnected before the start", conns[i].node);
                conn_close(i);
                continue;
            }
            len = read(fds[i].fd, buf, sizeof(buf));
            if (len < 1)
                conn_close(i);
            else if (conns[i].node < 0)
                conn_hello(i, buf, len, ctxt->vtime_nodes);
            else
                relay(ctxt->node_graph, i, buf, len);
        }
    }
}

/*
 * Virtual time: the conductor (docs/VIRTUAL_TIME.md §3).
 *
 * Events are kept in a binary min-heap ordered by (time, node ID, sequence).
 * An event is a frame delivery (one event per receiver, all sharing one
 * reference-counted copy of the frame), a node's deadline, or, with the
 * central medium (docs/MEDIUM.md), the verdict of a frame a receiver locked
 * onto, computed when the event is popped. A node has at most one live
 * deadline: its sequence number is in vt_deadline_seq[node], and any other
 * deadline entry of that node in the heap is stale and skipped when it
 * reaches the head.
 */
#define VT_SAME_TIME_CAP 100000

struct vt_frame {
    int refs;
    int len;
    uint8_t data[];
};

enum { VT_EV_DEADLINE, VT_EV_FRAME, VT_EV_VERDICT, VT_EV_SNIFF, VT_EV_CTL };

struct vt_event {
    uint64_t t;
    uint64_t seq;
    int node;
    int kind;                   // VT_EV_*
    struct vt_frame *frame;     // FRAME
    float rx_dbm;               // FRAME, central medium: the power at this receiver
    struct med_lock *lock;      // VERDICT
    uint32_t sniff;             // SNIFF: the serial of the sniff to check
};

static struct vt_event *vt_heap;
static size_t vt_heap_len, vt_heap_cap;
static uint64_t vt_seq = 1;                     // 0 means "no deadline"
static uint64_t vt_deadline_seq[MAX_NODES];
static int vt_nodes[MAX_NODES];                 // connected node IDs, ascending
static int vt_num_nodes;
static uint64_t vt_now;                         // time of the last wake
static uint64_t vt_wakes, vt_frames_in, vt_frames_out;
static uint64_t vt_cut[MAX_NODES][MAX_NODES / 64]; // vt_cut[dst]: senders it no longer hears
static uint64_t vt_verdicts_out, vt_senses, vt_sniffs_in, vt_sniff_results_out;
static uint8_t *vt_cca_shm;                     // the CCA shared memory (SIM_SHM_CCA_STRIDE bytes per node)

/*
 * Clients of the stack-neutral radio interface (docs/RADIO_INTERFACE.md).
 * They write no shared memory: the server keeps their radio's state from
 * their "xr" reports and writes it to the node's shared-memory entry itself,
 * where the medium reads every radio's state. It also checks their frames:
 * a frame is sent only while the reported state is tx, and not before the end
 * of the node's previous frame. Frames that break these rules are not sent.
 */
enum { RI_ERR_TX_STATE, RI_ERR_TX_OVERLAP, RI_ERR_TX_EARLY, RI_ERR_REPORT, RI_ERR_VERSION, RI_ERR_COUNT };
static const char *const ri_err_names[RI_ERR_COUNT] = {
    "frames sent while the reported radio state was not tx",
    "frames sent before the end of the node's previous frame",
    "radio reports leaving tx before the end of the node's frame",
    "malformed radio reports",
    "sniff messages (\"xn\", \"xl\") refused: they need interface version 2",
};
static struct {
    uint8_t state;              // enum sim_radio_state, from its last "xr"
    uint64_t tx_end;            // end of its last frame on air
    uint64_t errors[RI_ERR_COUNT];
} vt_ri[MAX_NODES];

static bool vt_radio_if(int node)
{
    return slot_of_node[node] && conns[slot_of_node[node]].radio_if;
}

// The driver states of app_wshwsim/sl_rf_driver.c (byte 0 of a node's
// shared-memory entry, read only by the legacy medium's CCA): kept consistent
static const uint8_t ri_drvstate[SIM_RADIO_STATES] = { 7, 2, 3, 5, 4 };

static void vt_shm_radio(int node, uint8_t state, uint16_t chan)
{
    uint8_t *e = vt_cca_shm + node * SIM_SHM_CCA_STRIDE;

    e[0] = ri_drvstate[state];
    memcpy(e + 1, &chan, 2);
    e[3] = state;
}

// A radio state the conductor applies itself, for a sniff (docs/MEDIUM.md
// §10): the power ledger records it, and for a client of the radio interface,
// which writes no shared memory, its stored state and shared-memory entry
// follow, so that the medium sees its receiver as the ledger does.
// chan < 0 keeps the channel of the node's entry.
static void vt_sniff_radio(int node, uint64_t t, uint8_t state, int chan, bool ledger)
{
    uint16_t c;

    if (ledger && pwr_enabled()) {
        struct sim_radio_report r = { .magic = "xr", .state = state, .channel = chan < 0 ? 0 : chan };

        pwr_radio(node, t, &r);
    }
    if (!vt_radio_if(node))
        return;
    if (chan < 0)
        memcpy(&c, vt_cca_shm + node * SIM_SHM_CCA_STRIDE + 1, 2);
    else
        c = chan;
    vt_ri[node].state = state;
    vt_shm_radio(node, state, c);
}

static void vt_ri_error(int node, int kind, const char *what)
{
    if (!vt_ri[node].errors[kind]++ || !(vt_ri[node].errors[kind] & (vt_ri[node].errors[kind] - 1)))
        WARN("node %d: radio interface: %s at %llu us (%llu so far)", node, what,
             (unsigned long long)vt_now, (unsigned long long)vt_ri[node].errors[kind]);
}

// A new instance or a stopped node: its radio is off, nothing on air
static void vt_ri_reset(int node)
{
    vt_ri[node].state = SIM_RADIO_OFF;
    vt_ri[node].tx_end = 0;
}

static bool vt_before(const struct vt_event *a, const struct vt_event *b)
{
    if (a->t != b->t)
        return a->t < b->t;
    if (a->node != b->node)
        return a->node < b->node;
    return a->seq < b->seq;
}

static void vt_push_ev(struct vt_event ev)
{
    size_t i, parent;

    if (vt_heap_len == vt_heap_cap) {
        vt_heap_cap = vt_heap_cap ? vt_heap_cap * 2 : 1024;
        vt_heap = realloc(vt_heap, vt_heap_cap * sizeof(*vt_heap));
        FATAL_ON(!vt_heap, 1, "realloc: %m");
    }
    ev.seq = vt_seq++;
    if (ev.kind == VT_EV_DEADLINE)
        vt_deadline_seq[ev.node] = ev.seq;
    else if (ev.kind == VT_EV_FRAME || ev.kind == VT_EV_CTL)
        ev.frame->refs++;
    for (i = vt_heap_len++; i > 0; i = parent) {
        parent = (i - 1) / 2;
        if (!vt_before(&ev, &vt_heap[parent]))
            break;
        vt_heap[i] = vt_heap[parent];
    }
    vt_heap[i] = ev;
}

static void vt_push(uint64_t t, int node, struct vt_frame *frame)
{
    vt_push_ev((struct vt_event){ .t = t, .node = node, .kind = frame ? VT_EV_FRAME : VT_EV_DEADLINE, .frame = frame });
}

static struct vt_event vt_pop(void)
{
    struct vt_event top = vt_heap[0];
    struct vt_event last = vt_heap[--vt_heap_len];
    size_t i = 0, child;

    for (;;) {
        child = 2 * i + 1;
        if (child >= vt_heap_len)
            break;
        if (child + 1 < vt_heap_len && vt_before(&vt_heap[child + 1], &vt_heap[child]))
            child++;
        if (!vt_before(&vt_heap[child], &last))
            break;
        vt_heap[i] = vt_heap[child];
        i = child;
    }
    if (vt_heap_len)
        vt_heap[i] = last;
    return top;
}

static void vt_unref(struct vt_frame *frame)
{
    if (!--frame->refs)
        free(frame);
}

static bool vt_stale(const struct vt_event *ev)
{
    return ev->kind == VT_EV_DEADLINE && vt_deadline_seq[ev->node] != ev->seq;
}

static void vt_drop_stale(void)
{
    while (vt_heap_len && vt_stale(&vt_heap[0]))
        vt_pop();
}

static int vt_fd(int node)
{
    return fds[slot_of_node[node]].fd;
}

static int vt_send(int node, const void *msg, size_t len)
{
    ssize_t ret = send(vt_fd(node), msg, len, MSG_NOSIGNAL);

    if (ret != (ssize_t)len) {
        WARN("node %d: send: %m", node);
        return -1;
    }
    return 0;
}

static void vt_set_deadline(int node, uint64_t next)
{
    vt_deadline_seq[node] = 0;
    if (next == NSIM_NEVER)
        return;
    if (next < vt_now) {
        WARN("node %d: deadline %llu us is before now (%llu us), moved to now",
             node, (unsigned long long)next, (unsigned long long)vt_now);
        next = vt_now;
    }
    vt_push(next, node, NULL);
}

static bool vt_drop(int src, int dst, const struct sim_frame_hdr *hdr)
{
    const uint8_t *psdu = (const uint8_t *)(hdr + 1);
    int i;

    // the sequence number follows the frame control, unless suppressed
    if (!vt_drops_n || hdr->len < 3 || (psdu[1] & 0x01))
        return false;
    for (i = 0; i < vt_drops_n; i++)
        if (vt_drops[i].src == src && vt_drops[i].dst == dst && vt_drops[i].seq == psdu[2] &&
            vt_now >= vt_drops[i].from && vt_now <= vt_drops[i].to) {
            INFO("virtual time: frame from node %d (sequence number %d) not delivered to node %d at %llu us",
                 src, psdu[2], dst, (unsigned long long)vt_now);
            vt_dropped++;
            return true;
        }
    return false;
}

// Whether dst hears frames from src: a connected graph neighbour, not cut,
// and this frame not dropped for it
static bool vt_hears(struct ctxt *ctxt, int src, int dst, const struct sim_frame_hdr *hdr)
{
    return bitmap_get(dst, ctxt->node_graph[src], MAX_NODES / 64) && slot_of_node[dst] &&
           !bitmap_get(src, vt_cut[dst], MAX_NODES / 64) && !vt_drop(src, dst, hdr);
}

// The graph neighbours of src after node `after` (-1: the first), in
// ascending order, or -1: the set bits of src's row. Every connected node is
// in vt_nodes, so walking these instead of vt_nodes finds the same receivers
// in the same order, without testing the nodes that cannot hear src.
static int vt_next_neighbour(struct ctxt *ctxt, int src, int after)
{
    int i = after + 1;
    uint64_t w;

    while (i < MAX_NODES) {
        w = ctxt->node_graph[src][i / 64] >> (i % 64);
        if (w)
            return i + __builtin_ctzll(w);
        i = (i / 64 + 1) * 64;
    }
    return -1;
}

// Central medium (docs/MEDIUM.md §3): the frame goes on air; each receiver
// that locks onto it gets it now ("xx" with SIM_FRAME_MEDIUM and its power)
// and a verdict at its end.
static void vt_queue_frame_medium(struct ctxt *ctxt, int src, void *msg, int len, uint32_t preamble_us)
{
    struct sim_frame_hdr *hdr = msg;
    struct vt_frame *frame = NULL;
    struct med_frame *f;
    struct med_lock *lock, *broken;
    float rx_dbm;
    int i;

    uint32_t serial;
    uint64_t check;

    med_tx_start(src, vt_now);
    f = med_frame_start_lp(src, hdr, vt_now, preamble_us);
    if (!f)
        return;
    vt_airtime_us += med_frame_end(f) - vt_now;
    for (i = vt_next_neighbour(ctxt, src, -1); i >= 0; i = vt_next_neighbour(ctxt, src, i))
        if (vt_hears(ctxt, src, i, hdr))
            med_add_rx(f, i);
    hdr->flags = SIM_FRAME_MEDIUM | (preamble_us ? SIM_FRAME_LONG_PREAMBLE : 0);
    hdr->frame_id = med_frame_id(f);
    for (i = 0; i < med_num_rx(f); i++) {
        lock = med_decide(f, i, vt_now, &rx_dbm);
        broken = med_take_broken();
        if (broken)             // its receiver gets the failed verdict now (§3)
            vt_push_ev((struct vt_event){ .t = vt_now, .node = med_rx_node(f, i), .kind = VT_EV_VERDICT,
                                          .lock = broken });
        // A receiver that sniffs on this channel: a dwell to check (§10)
        check = med_sniff_frame(f, i, &serial);
        if (check != MED_SNIFF_NONE)
            vt_push_ev((struct vt_event){ .t = check, .node = med_rx_node(f, i), .kind = VT_EV_SNIFF,
                                          .sniff = serial });
        if (!lock)
            continue;
        if (!frame) {
            frame = malloc(sizeof(*frame) + len);
            FATAL_ON(!frame, 1, "malloc: %m");
            frame->refs = 1;    // held until the loop ends
            frame->len = len;
            memcpy(frame->data, msg, len);
        }
        vt_push_ev((struct vt_event){ .t = vt_now, .node = med_rx_node(f, i), .kind = VT_EV_FRAME,
                                      .frame = frame, .rx_dbm = rx_dbm });
        vt_push_ev((struct vt_event){ .t = med_frame_end(f), .node = med_rx_node(f, i), .kind = VT_EV_VERDICT,
                                      .lock = lock });
    }
    if (frame)
        vt_unref(frame);
}

// Queue a frame from src for its neighbours at the current time.
// preamble_us: the long preamble of an "xl" (central medium only).
static void vt_queue_frame(struct ctxt *ctxt, int src, void *msg, int len, uint32_t preamble_us)
{
    struct sim_frame_hdr *hdr = msg;
    struct vt_frame *frame;
    int i;

    if (!frame_check(src, msg, len))
        return;
    if (vt_radio_if(src)) {
        uint64_t airtime = hdr->rate_bps ?
            (uint64_t)(hdr->shr_len + hdr->phr_len + hdr->len + hdr->fcs_len) * 8 * 1000000 / hdr->rate_bps : 0;

        if (vt_ri[src].state != SIM_RADIO_TX) {
            vt_ri_error(src, RI_ERR_TX_STATE, "frame sent while the radio is not in tx: not sent");
            return;
        }
        if (vt_now < vt_ri[src].tx_end) {
            vt_ri_error(src, RI_ERR_TX_OVERLAP, "frame sent before the end of the previous one: not sent");
            return;
        }
        // An "xl" is on air for its long preamble, then the frame
        vt_ri[src].tx_end = vt_now + preamble_us + airtime;
    }
    hdr->src = src;
    vt_frames_in++;
    if (vt_frames_file) {
        const uint8_t *psdu = (const uint8_t *)msg + sizeof(*hdr);
        fprintf(vt_frames_file, "%llu %d %d ", (unsigned long long)vt_now, src, hdr->chan);
        for (i = 0; i < hdr->len; i++)
            fprintf(vt_frames_file, "%02x", psdu[i]);
        fputc('\n', vt_frames_file);
    }
    hdr->flags = 0;
    hdr->frame_id = 0;
    hdr->rx_dbm = 0;
    if (med_central()) {
        vt_queue_frame_medium(ctxt, src, msg, len, preamble_us);
        return;
    }
    frame = malloc(sizeof(*frame) + len);
    FATAL_ON(!frame, 1, "malloc: %m");
    frame->refs = 1;            // held until the loop below ends
    frame->len = len;
    memcpy(frame->data, msg, len);
    for (i = vt_next_neighbour(ctxt, src, -1); i >= 0; i = vt_next_neighbour(ctxt, src, i))
        if (vt_hears(ctxt, src, i, hdr))
            vt_push(vt_now, i, frame);
    vt_unref(frame);
}

// Read what a running node sends until its "xi". Returns 0, or -1 if the node
// is gone or the run was interrupted.
static int vt_read_until_idle(struct ctxt *ctxt, int node)
{
    const struct nsim_idle *idle = (const struct nsim_idle *)buf;
    ssize_t len;

    for (;;) {
        len = recv(vt_fd(node), buf, sizeof(buf), 0);
        if (len < 0 && errno == EINTR) {
            if (stop)
                return -1;
            continue;
        }
        if (len <= 0) {
            WARN("node %d disconnected while running at %llu us: run aborted", node,
                 (unsigned long long)vt_now);
            return -1;
        }
        if (len >= 2 && !memcmp(buf, "xi", 2)) {
            if (len != sizeof(*idle)) {
                WARN("node %d: bad idle message (%zd bytes): run aborted", node, len);
                return -1;
            }
            if (pwr_enabled() && !vt_uncharged)
                pwr_idle(node, vt_now, idle->next_us);
            vt_uncharged = false;
            vt_set_deadline(node, idle->next_us);
            return 0;
        }
        if (len >= 2 && !memcmp(buf, "xx", 2)) {
            vt_queue_frame(ctxt, node, buf, len, 0);
        } else if (len >= 2 && (!memcmp(buf, "xl", 2) || !memcmp(buf, "xn", 2)) &&
                   (!med_central() || (vt_radio_if(node) && conns[slot_of_node[node]].radio_if < 2))) {
            // Sniffing needs the central medium, and for a client of the
            // radio interface, version 2 (docs/RADIO_INTERFACE.md §6.7)
            if (vt_radio_if(node))
                vt_ri_error(node, RI_ERR_VERSION, "\"xn\" or \"xl\" from a version-1 client: dropped (needs version 2)");
            else
                WARN("node %d: \"%.2s\" needs the central medium (preamble sniffing, docs/MEDIUM.md §10): dropped",
                     node, buf);
        } else if (!memcmp(buf, "xl", 2)) {
            // A frame with a long preamble (§10): the "xx" that follows
            const struct sim_lp_frame *lp = (const struct sim_lp_frame *)buf;

            if (len < (int)sizeof(*lp) + 2)
                WARN("node %d: \"xl\" of %zd bytes, dropped", node, len);
            else
                vt_queue_frame(ctxt, node, buf + sizeof(*lp), len - sizeof(*lp), lp->preamble_us);
        } else if (len == sizeof(struct sim_sniff) && !memcmp(buf, "xn", 2)) {
            // A sniff (§10): its receiver is on from now; checks queued
            const struct sim_sniff *req = (const struct sim_sniff *)buf;
            uint64_t check;
            uint32_t serial;
            uint16_t chan;

            // A client of the radio interface that holds a frame leaves it:
            // the sniff puts its receiver in LISTEN (RADIO_INTERFACE.md §6.7)
            if (vt_radio_if(node))
                med_rx_leave(node, vt_now);
            serial = med_sniff_start(node, req, vt_now, &check);

            vt_sniffs_in++;
            vt_push_ev((struct vt_event){ .t = check, .node = node, .kind = VT_EV_SNIFF, .sniff = serial });
            // Listening from now, or once its own frame has ended (then at
            // a check event): the ledger, and a client's radio state
            if (med_sniff_rx_on(node, serial, vt_now, &chan))
                vt_sniff_radio(node, vt_now, SIM_RADIO_LISTEN, chan, true);
        } else if (med_central() && len == sizeof(struct sim_sense) && !memcmp(buf, "xs", 2)) {
            // CCA: answered at once, the node waits for it
            struct sim_sense *sense = (struct sim_sense *)buf;

            sense->dbm = med_sense(node, sense->channel, vt_now);
            vt_senses++;
            if (vt_send(node, sense, sizeof(*sense)))
                return -1;
        } else if (len == sizeof(struct sim_radio_report) && !memcmp(buf, "xr", 2)) {
            const struct sim_radio_report *r = (const struct sim_radio_report *)buf;
            int sniff_chan;

            if (vt_radio_if(node)) {
                if (r->state >= SIM_RADIO_STATES) {
                    vt_ri_error(node, RI_ERR_REPORT, "radio report with an unknown state: ignored");
                    continue;
                }
                // The frame stays on air to its end whatever the node
                // reports: a radio that leaves TX earlier would hear while
                // it sends. Applied, but warned about and counted.
                if (vt_ri[node].state == SIM_RADIO_TX && r->state != SIM_RADIO_TX && vt_now < vt_ri[node].tx_end)
                    vt_ri_error(node, RI_ERR_TX_EARLY, "radio left tx before the end of its frame");
                // A report that turns the receiver off, starts a TX or moves
                // the receiver to another channel ends the sniff in progress,
                // with no "xd" (RADIO_INTERFACE.md §6.7)
                sniff_chan = med_central() ? med_sniff_channel(node) : -1;
                if (sniff_chan >= 0 &&
                    (r->state == SIM_RADIO_OFF || r->state == SIM_RADIO_IDLE ||
                     (r->state == SIM_RADIO_TX && vt_ri[node].state != SIM_RADIO_TX) ||
                     ((r->state == SIM_RADIO_LISTEN || r->state == SIM_RADIO_RECEIVE) && r->channel != sniff_chan)))
                    med_sniff_cancel(node, vt_now);
                vt_ri[node].state = r->state;
                vt_shm_radio(node, r->state, r->channel);
            }
            if (pwr_enabled())
                pwr_radio(node, vt_now, r);
        } else if (len == sizeof(struct nsim_wake) && !memcmp(buf, "xc", 2)) {
            if (pwr_enabled() && !vt_uncharged)
                pwr_wake_report(node, (const struct nsim_wake *)buf);
        } else {
            WARN("node %d: unexpected message (%zd bytes, \"%.2s\"), dropped", node, len, buf);
        }
    }
}

// Wake node at time t: "xa", then its messages due at t in queue order (they
// are at the head of the heap): frames, and with the central medium, the
// verdicts, computed now. The advance says how many messages follow: the node
// may read the "xa" before they are in its socket, as they are separate
// messages, so it waits for exactly that many.
static int vt_wake(struct ctxt *ctxt, int node, uint64_t t)
{
    static struct vt_msg {
        struct vt_frame *frame;     // NULL: verdict or sniff result
        float rx_dbm;
        bool sniff;                 // sniff result, not verdict
        bool ctl;                   // a control message ("xL"), sent as it is
        struct sim_verdict verdict;
        struct sim_sniff_result result;
    } *msgs;
    static size_t msgs_cap;
    static uint8_t out[sizeof(buf)];
    struct nsim_advance adv = { .magic = "xa", .now_us = t };
    struct sim_frame_hdr *hdr;
    struct vt_event ev;
    size_t num = 0, num_frames = 0, num_ask = 0, i;
    uint32_t delivered = 0;     // the last medium frame delivered
    bool deadline = false;      // the node's own deadline is due
    int ret = 0;

    vt_now = t;
    while (vt_heap_len && vt_heap[0].t == t && vt_heap[0].node == node) {
        ev = vt_pop();
        if (ev.kind == VT_EV_DEADLINE) {
            if (ev.seq == vt_deadline_seq[node]) {
                vt_deadline_seq[node] = 0; // consumed: the node reports its next one
                deadline = true;
            }
            continue;
        }
        if (num == msgs_cap) {
            msgs_cap = msgs_cap ? msgs_cap * 2 : 64;
            msgs = realloc(msgs, msgs_cap * sizeof(*msgs));
            FATAL_ON(!msgs, 1, "realloc: %m");
        }
        if (ev.kind == VT_EV_VERDICT) {
            if (!med_verdict(ev.lock, t, &msgs[num].verdict))
                continue;
            msgs[num].sniff = false;
            msgs[num].ctl = false;
            msgs[num++].frame = NULL;
            continue;
        }
        if (ev.kind == VT_EV_SNIFF) {
            struct med_lock *lock;
            const void *fmsg;
            uint64_t next;
            float rx_dbm;
            int flen;
            uint16_t chan;

            if (med_sniff_rx_on(node, ev.sniff, t, &chan))
                vt_sniff_radio(node, t, SIM_RADIO_LISTEN, chan, true);
            if (!med_sniff_check(node, ev.sniff, t, &msgs[num].result, &lock, &fmsg, &flen, &rx_dbm, &next)) {
                if (next != MED_SNIFF_NONE)
                    vt_push_ev((struct vt_event){ .t = next, .node = node, .kind = VT_EV_SNIFF, .sniff = ev.sniff });
                continue;
            }
            {
                // Nothing detected: the radio goes to the request's end
                // state; locked: it receives, on the frame's channel (§10)
                uint8_t state = !msgs[num].result.detected ? med_sniff_end_state(node, t) :
                                lock ? SIM_RADIO_RECEIVE : SIM_RADIO_LISTEN;

                if (state != SIM_RADIO_LISTEN && state < SIM_RADIO_STATES)
                    vt_sniff_radio(node, t, state, lock ? ((const struct sim_frame_hdr *)fmsg)->chan : -1, true);
            }
            msgs[num].sniff = true;
            msgs[num].ctl = false;
            msgs[num++].frame = NULL;
            if (!lock)
                continue;
            if (num == msgs_cap) {
                msgs_cap *= 2;
                msgs = realloc(msgs, msgs_cap * sizeof(*msgs));
                FATAL_ON(!msgs, 1, "realloc: %m");
            }
            msgs[num].frame = malloc(sizeof(struct vt_frame) + flen);
            FATAL_ON(!msgs[num].frame, 1, "malloc: %m");
            msgs[num].frame->refs = 1;
            msgs[num].frame->len = flen;
            memcpy(msgs[num].frame->data, fmsg, flen);
            msgs[num].sniff = false;
            msgs[num].ctl = false;
            msgs[num++].rx_dbm = rx_dbm;
            num_frames++;
            vt_push_ev((struct vt_event){ .t = med_lock_end(lock), .node = node, .kind = VT_EV_VERDICT,
                                          .lock = lock });
            continue;
        }
        msgs[num].sniff = false;
        msgs[num].ctl = ev.kind == VT_EV_CTL;
        msgs[num].frame = ev.frame;
        msgs[num++].rx_dbm = ev.rx_dbm;
        if (ev.kind != VT_EV_CTL)
            num_frames++;
        if (ev.kind == VT_EV_CTL && ev.sniff)   // a measurement request ("ask") has sniff = 1
            num_ask++;
    }
    // Only verdicts of locks that ended before their frame's end (captured,
    // tx_abort, left): nothing to tell the node, so no wake (it would count
    // as an MCU wake in the power report). Never the case in legacy mode,
    // whose head event is always a live deadline or a frame.
    if (!num && !deadline)
        return 0;
    FATAL_ON(num > UINT16_MAX, 1, "node %d: %zu messages due at %" PRIu64 " us", node, num, t);
    adv.frames = num;
    vt_wakes++;
    // A wake for measurement requests only (the scenario driver's "ask"):
    // not in the power ledger, which keeps the node asleep (docs/SCENARIOS.md)
    vt_uncharged = !deadline && num > 0 && num_ask == num;
    if (pwr_enabled() && !vt_uncharged)
        pwr_wake(node, t, num_frames);
    if (vt_send(node, &adv, sizeof(adv)))
        ret = -1;
    for (i = 0; i < num; i++) {
        if (!msgs[i].frame && msgs[i].sniff) {
            if (msgs[i].result.detected)
                msgs[i].result.rssi_dbm = prop_reported_dbm(node, msgs[i].result.frame_id, msgs[i].result.t_us, msgs[i].result.rssi_dbm);
            if (!ret && vt_send(node, &msgs[i].result, sizeof(msgs[i].result)))
                ret = -1;
            vt_sniff_results_out++;
            continue;
        }
        if (!msgs[i].frame) {
            msgs[i].verdict.rx_dbm = prop_reported_dbm(node, msgs[i].verdict.frame_id, 0, msgs[i].verdict.rx_dbm);
            if (!ret && vt_send(node, &msgs[i].verdict, sizeof(msgs[i].verdict)))
                ret = -1;
            vt_verdicts_out++;
            continue;
        }
        if (msgs[i].ctl) {
            if (!ret && vt_send(node, msgs[i].frame->data, msgs[i].frame->len))
                ret = -1;
            vt_unref(msgs[i].frame);
            continue;
        }
        hdr = (struct sim_frame_hdr *)msgs[i].frame->data;
        if (!ret && hdr->flags & SIM_FRAME_MEDIUM) {
            // The power at this receiver, in a copy: the frame is shared
            memcpy(out, msgs[i].frame->data, msgs[i].frame->len);
            ((struct sim_frame_hdr *)out)->rx_dbm = prop_reported_dbm(node, hdr->frame_id, 0, msgs[i].rx_dbm);
            // A version-1 client of the radio interface knows only MEDIUM
            if (vt_radio_if(node) && conns[slot_of_node[node]].radio_if < 2)
                ((struct sim_frame_hdr *)out)->flags &= ~SIM_FRAME_LONG_PREAMBLE;
            delivered = hdr->frame_id;
            if (vt_send(node, out, msgs[i].frame->len))
                ret = -1;
        } else if (!ret && vt_send(node, msgs[i].frame->data, msgs[i].frame->len)) {
            ret = -1;
        }
        vt_frames_out++;
        vt_unref(msgs[i].frame);
    }
    if (ret)
        return ret;
    ret = vt_read_until_idle(ctxt, node);
    if (delivered)
        med_delivered(node, delivered);
    return ret;
}

static void vt_log_head(void)
{
    size_t i, n = vt_heap_len < 10 ? vt_heap_len : 10;

    WARN("queue head (%zu events):", vt_heap_len);
    // The heap array is not sorted; its first entries are a good sample.
    for (i = 0; i < n; i++)
        WARN("  t=%llu us node=%d seq=%llu %s%s", (unsigned long long)vt_heap[i].t, vt_heap[i].node,
             (unsigned long long)vt_heap[i].seq,
             vt_heap[i].kind == VT_EV_FRAME ? "frame" : vt_heap[i].kind == VT_EV_VERDICT ? "verdict" : "deadline",
             vt_stale(&vt_heap[i]) ? " (stale)" : "");
}

// Send "xq" to every node and wait (at most 10 s) for them to close.
static void vt_quit_all(void)
{
    struct nsim_quit quit = { .magic = "xq" };
    struct pollfd pfd;
    int i, open = 0;
    ssize_t len;

    for (i = 0; i < vt_num_nodes; i++)
        if (slot_of_node[vt_nodes[i]])
            send(vt_fd(vt_nodes[i]), &quit, sizeof(quit), MSG_NOSIGNAL);
    // Instances for a later start that never started: "xq" instead of a welcome
    for (i = vt_ctls_next; i < vt_ctls_n; i++)
        if (vt_ctls[i].kind == VT_CTL_START && vt_ctls[i].slot > 0 && fds[vt_ctls[i].slot].fd >= 0) {
            send(fds[vt_ctls[i].slot].fd, &quit, sizeof(quit), MSG_NOSIGNAL);
            conn_close(vt_ctls[i].slot);
        }
    for (i = 0; i < vt_num_nodes; i++) {
        if (!slot_of_node[vt_nodes[i]])
            continue;
        pfd.fd = vt_fd(vt_nodes[i]);
        pfd.events = POLLIN;
        for (;;) {
            if (poll(&pfd, 1, 10000) <= 0) {
                open++;
                break;
            }
            len = recv(pfd.fd, buf, sizeof(buf), MSG_DONTWAIT);
            if (len == 0 || (len < 0 && errno != EAGAIN && errno != EINTR))
                break;
        }
    }
    if (open)
        WARN("%d nodes did not close after \"xq\"", open);
}

static double timespec_diff(const struct timespec *a, const struct timespec *b)
{
    return (b->tv_sec - a->tv_sec) + (b->tv_nsec - a->tv_nsec) / 1e9;
}

static const char *vt_time_str(uint64_t t)
{
    static char str[32];

    snprintf(str, sizeof(str), "%llu.%06llu s", (unsigned long long)(t / 1000000), (unsigned long long)(t % 1000000));
    return str;
}

// Stop a node outright: no more wakes or frames for it, its connection
// closed (the program exits on its own: "conductor closed the connection").
static void vt_kill(int node)
{
    if (!slot_of_node[node]) {
        WARN("virtual time: node %d is not running at %s: nothing to stop", node, vt_time_str(vt_now));
        return;
    }
    INFO("virtual time: node %d stopped at %s", node, vt_time_str(vt_now));
    conn_close(slot_of_node[node]);
    vt_deadline_seq[node] = 0;  // its deadline is stale; its frames are skipped
    // Its radio, as its neighbours' CCA sees it: off, not in the state it had
    if (vt_cca_shm) {
        vt_cca_shm[node * SIM_SHM_CCA_STRIDE] = 7;   // DRVSTATE_OFF (app_wshwsim/sl_rf_driver.c)
        vt_cca_shm[node * SIM_SHM_CCA_STRIDE + 3] = SIM_RADIO_OFF;
    }
    vt_ri_reset(node);
    if (med_central())
        med_node_stopped(node, vt_now);
}

// The welcome of a node. A client of the radio interface gets its interface
// version in the version field, not the conductor's (docs/RADIO_INTERFACE.md).
static int vt_welcome(int node, const struct nsim_welcome *welcome)
{
    struct nsim_welcome w = *welcome;

    if (vt_radio_if(node)) {
        w.version = conns[slot_of_node[node]].radio_if;
        if (w.version >= 2 && med_central())
            w.flags |= SIM_WELCOME_SNIFF;
        w.reserved = pwr_cca_us(node);     // cca_us (docs/RADIO_INTERFACE.md §4.3)
        vt_ri_reset(node);
    }
    return vt_send(node, &w, sizeof(w));
}

static int vt_ctl_run(struct ctxt *ctxt, struct vt_ctl *c, struct nsim_welcome *welcome)
{
    vt_now = c->t;
    switch (c->kind) {
    case VT_CTL_KILL:
        vt_kill(c->node);
        return 0;
    case VT_CTL_CUT:
        INFO("virtual time: frames from node %d no longer reach node %d from %s", c->src, c->node, vt_time_str(vt_now));
        bitmap_set(c->src, vt_cut[c->node], MAX_NODES / 64);
        return 0;
    case VT_CTL_LINK:
        // A positioned pair below the hearing cutoff joins the graph, both
        // ways (docs/MEDIUM.md §11.5)
        if (!bitmap_get(c->node, ctxt->node_graph[c->src], MAX_NODES / 64) && prop_positioned(c->src, c->node)) {
            bitmap_set(c->node, ctxt->node_graph[c->src], MAX_NODES / 64);
            bitmap_set(c->src, ctxt->node_graph[c->node], MAX_NODES / 64);
        }
        if (!bitmap_get(c->node, ctxt->node_graph[c->src], MAX_NODES / 64)) {
            WARN("virtual time: no link %d-%d to change", c->src, c->node);
            return -1;
        }
        INFO("virtual time: link %d-%d at %.1f dBm from %s", c->src, c->node, c->rssi, vt_time_str(vt_now));
        sim_link(nodes_infos_flat_map, c->node, c->src)->rssi = c->rssi;
        sim_link(nodes_infos_flat_map, c->src, c->node)->rssi = c->rssi;
        prop_override(c->src, c->node);
        return 0;
    case VT_CTL_START:
        if (slot_of_node[c->node])
            vt_kill(c->node);
        if (c->slot <= 0 || fds[c->slot].fd < 0) {
            WARN("virtual time: no instance of node %d to start at %s: run aborted", c->node, vt_time_str(vt_now));
            return -1;
        }
        INFO("virtual time: node %d started at %s (%s)", c->node, vt_time_str(vt_now),
             c->first ? "powered up" : "new instance");
        slot_of_node[c->node] = c->slot;
        c->slot = 0;
        welcome->start_us = vt_now;   // its clock starts now (protocol version 4)
        if (pwr_enabled())
            pwr_wake(c->node, vt_now, 0);
        welcome->drift_ppb = drift_of(c->node, welcome->seed);
        if (vt_welcome(c->node, welcome) || vt_read_until_idle(ctxt, c->node))
            return -1;
        return 0;
    }
    return 0;
}

/*
 * Scenario control (--vtime-control, docs/SCENARIOS.md). A driver connects to
 * a UNIX stream socket and sends one command per line; the server answers
 * each with one line. The run pauses whenever its next event is later than
 * the time the driver allowed ("run_until"): every node is then idle and its
 * log written up to that time, so the driver's decisions depend on virtual
 * time only and the run stays reproducible from the seed and the scenario.
 * Commands act at the pause time (vt_now).
 */
static void ctl_unlink(void)
{
    // Not from a child of the server (none should exit through atexit, but
    // a child must never remove the parent's socket)
    if (ctl_path && getpid() == ctl_owner)
        unlink(ctl_path);
}

static void ctl_listen(void)
{
    struct sockaddr_un a = { .sun_family = AF_UNIX };

    strcpy(a.sun_path, ctl_path);
    ctl_listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    FATAL_ON(ctl_listen_fd < 0, 1, "--vtime-control: socket: %m");
    unlink(ctl_path);
    FATAL_ON(bind(ctl_listen_fd, (struct sockaddr *)&a, sizeof(a)) < 0, 1, "--vtime-control: bind %s: %m", ctl_path);
    FATAL_ON(listen(ctl_listen_fd, 1) < 0, 1, "--vtime-control: listen: %m");
    ctl_owner = getpid();
    atexit(ctl_unlink);     // also when the server stops on an error
    INFO("scenario: control socket %s", ctl_path);
}

static void ctl_accept(void)
{
    struct pollfd p = { .fd = ctl_listen_fd, .events = POLLIN };

    INFO("scenario: waiting for the driver");
    FATAL_ON(poll(&p, 1, 120000) <= 0, 1, "--vtime-control: no driver connected within 120 s");
    ctl_fd = accept(ctl_listen_fd, NULL, NULL);
    FATAL_ON(ctl_fd < 0, 1, "--vtime-control: accept: %m");
    close(ctl_listen_fd);
    ctl_listen_fd = -1;
    ctl_in = fdopen(dup(ctl_fd), "r");
    FATAL_ON(!ctl_in, 1, "fdopen: %m");
    INFO("scenario: driver connected");
}

// A node list: "all" (every connected Nanostack node), or IDs and ranges
static int ctl_nodes(char *spec, uint64_t *mask)
{
    memset(mask, 0, MAX_NODES / 8);
    if (!strcmp(spec, "all")) {
        for (int n = 0; n < MAX_NODES; n++)
            if (slot_of_node[n] && !vt_radio_if(n))
                bitmap_set(n, mask, MAX_NODES / 64);
        return 0;
    }
    return bitmap_parse(spec, mask, MAX_NODES / 64);
}

// A new instance of node, started by the driver (it connects to the
// simulation socket): welcomed now, its clock at 0, as --vtime-start
static int ctl_start(struct ctxt *ctxt, int node, struct nsim_welcome *welcome, char *err, size_t errlen)
{
    struct pollfd p = { .fd = fds[0].fd, .events = POLLIN };
    struct sim_hello hello;
    int slot, fd;
    ssize_t len;

    if (fds[0].fd < 0) {
        snprintf(err, errlen, "no listening socket");
        return -1;
    }
    if (slot_of_node[node])
        vt_kill(node);
    if (poll(&p, 1, 60000) <= 0) {
        snprintf(err, errlen, "no new instance connected within 60 s");
        return -1;
    }
    fd = accept(fds[0].fd, NULL, NULL);
    if (fd < 0) {
        snprintf(err, errlen, "accept: %m");
        return -1;
    }
    for (slot = 1; slot < fd_limit && fds[slot].fd >= 0; slot++)
        ;
    if (slot >= fd_limit) {
        close(fd);
        snprintf(err, errlen, "no free connection slot");
        return -1;
    }
    p.fd = fd;
    if (poll(&p, 1, 60000) <= 0 || (len = recv(fd, &hello, sizeof(hello), 0)) != sizeof(hello) ||
        memcmp(hello.magic, "xh", 2) || hello.node_id != node || !(hello.flags & SIM_HELLO_VTIME)) {
        close(fd);
        snprintf(err, errlen, "the new connection is not node %d's virtual-time hello", node);
        return -1;
    }
    fds[slot].fd = fd;
    fds[slot].events = 0;
    conns[slot].node = node;
    conns[slot].radio_if = 0;
    slot_of_node[node] = slot;
    num_connected++;
    INFO("virtual time: node %d started at %s (scenario: new instance)", node, vt_time_str(vt_now));
    welcome->start_us = vt_now;
    if (pwr_enabled())
        pwr_wake(node, vt_now, 0);
    welcome->drift_ppb = drift_of(node, welcome->seed);
    if (vt_welcome(node, welcome) || vt_read_until_idle(ctxt, node)) {
        snprintf(err, errlen, "node %d failed at its start", node);
        return -2;
    }
    return 0;
}

static void ctl_push_text(int node, const char *text, bool ask)
{
    size_t n = strlen(text);
    struct vt_frame *f = malloc(sizeof(*f) + n + 2);

    FATAL_ON(!f, 1, "malloc: %m");
    f->refs = 0;
    f->len = n + 2;
    memcpy(f->data, "xL", 2);
    memcpy(f->data + 2, text, n);
    // sniff = 1 marks a measurement request: its wake is not charged
    vt_push_ev((struct vt_event){ .t = vt_now, .node = node, .kind = VT_EV_CTL, .frame = f, .sniff = ask });
}

// A node ID, the whole word: 0 to MAX_NODES - 1
static int ctl_id(const char *w)
{
    char *end;
    long v;

    if (!w || !*w)
        return -1;
    v = strtol(w, &end, 10);
    if (*end || v < 0 || v >= MAX_NODES)
        return -1;
    return v;
}

static char *ctl_word(char **rest)
{
    char *w;

    while (**rest == ' ')
        (*rest)++;
    if (!**rest)
        return NULL;
    w = *rest;
    while (**rest && **rest != ' ')
        (*rest)++;
    if (**rest)
        *(*rest)++ = '\0';
    while (**rest == ' ')
        (*rest)++;
    return w;
}

// The answer to the driver, of any length
static void ctl_answer(const char *fmt, ...)
{
    va_list ap;
    char *msg = NULL;
    int n;

    va_start(ap, fmt);
    n = vasprintf(&msg, fmt, ap);
    va_end(ap);
    if (n < 0)
        return;
    for (int off = 0; off < n; ) {
        ssize_t r = write(ctl_fd, msg + off, n - off);
        if (r <= 0 && errno != EINTR)
            break;
        if (r > 0)
            off += r;
    }
    if (write(ctl_fd, "\n", 1) < 0)
        WARN("scenario: write: %m");
    free(msg);
}

// One command (it answers itself). Returns 0, 1 to end the run, 2 to let
// time go on (run_until), -1 if the run must stop.
static int ctl_command(struct ctxt *ctxt, char *line, struct nsim_welcome *welcome)
{
    static uint64_t mask[MAX_NODES / 64];
    char *rest = line, *cmd, *a, *b, *c;
    int n, r;

    cmd = ctl_word(&rest);
    if (!cmd) {
        ctl_answer("err empty command");
        return 0;
    }
    if (!strcmp(cmd, "run_until")) {
        char *end;
        unsigned long long t = strtoull(rest, &end, 10);
        if (!*rest || *end) {
            ctl_answer("err usage: run_until MICROSECONDS");
            return 0;
        }
        if (t < vt_now) {
            ctl_answer("err time %llu is before now %llu", t, (unsigned long long)vt_now);
            return 0;
        }
        ctl_until = t;
        return 2;           // answered at the next pause
    }
    if (!strcmp(cmd, "now")) {
        ctl_answer("at %llu", (unsigned long long)vt_now);
        return 0;
    }
    if (!strcmp(cmd, "quit")) {
        ctl_answer("bye");
        return 1;
    }
    if (!strcmp(cmd, "log")) {
        INFO("scenario: %s", rest);
        ctl_answer("ok");
        return 0;
    }
    if (!strcmp(cmd, "node") || !strcmp(cmd, "ask")) {
        // ask: a measurement request (stats, tree), whose wake the power
        // ledger does not charge
        a = ctl_word(&rest);
        if (!a || !*rest || ctl_nodes(a, mask)) {
            ctl_answer("err usage: %s IDS|all TEXT", cmd);
            return 0;
        }
        if (strlen(rest) > SIM_CTL_TEXT_MAX) {
            ctl_answer("err text of %zu bytes, at most %d", strlen(rest), SIM_CTL_TEXT_MAX);
            return 0;
        }
        n = 0;
        for (int i = 0; i < MAX_NODES; i++)
            if (bitmap_get(i, mask, MAX_NODES / 64) && slot_of_node[i] && !vt_radio_if(i)) {
                ctl_push_text(i, rest, !strcmp(cmd, "ask"));
                n++;
            }
        ctl_answer("ok %d", n);
        return 0;
    }
    if (!strcmp(cmd, "kill") || !strcmp(cmd, "start")) {
        char err[160];
        n = ctl_id(ctl_word(&rest));
        if (n < 0 || *rest) {
            ctl_answer("err usage: %s ID (0 to %d)", cmd, MAX_NODES - 1);
            return 0;
        }
        if (!strcmp(cmd, "kill")) {
            if (!slot_of_node[n]) {
                ctl_answer("err node %d is not running", n);
                return 0;
            }
            vt_kill(n);
            if (pwr_enabled())
                pwr_node_off(n, vt_now);
            ctl_answer("ok");
            return 0;
        }
        r = ctl_start(ctxt, n, welcome, err, sizeof(err));
        if (r)
            ctl_answer("err %s", err);
        else
            ctl_answer("ok");
        return r == -2 ? -1 : 0;
    }
    if (!strcmp(cmd, "cut") || !strcmp(cmd, "uncut") || !strcmp(cmd, "link")) {
        int src, dst;
        a = ctl_word(&rest);
        b = ctl_word(&rest);
        c = ctl_word(&rest);
        src = ctl_id(a);
        dst = ctl_id(b);
        if (src < 0 || dst < 0 || *rest || (!strcmp(cmd, "link") ? !c : !!c)) {
            ctl_answer("err usage: cut|uncut SRC DST, link A B RSSI");
            return 0;
        }
        if (!strcmp(cmd, "cut")) {
            bitmap_set(src, vt_cut[dst], MAX_NODES / 64);
            INFO("virtual time: frames from node %d no longer reach node %d from %s (scenario)", src, dst, vt_time_str(vt_now));
        } else if (!strcmp(cmd, "uncut")) {
            vt_cut[dst][src / 64] &= ~(1ULL << (src % 64));
            INFO("virtual time: frames from node %d reach node %d again from %s (scenario)", src, dst, vt_time_str(vt_now));
        } else {
            char *end;
            struct vt_ctl lc = { .t = vt_now, .kind = VT_CTL_LINK, .node = dst, .src = src, .rssi = strtof(c, &end) };
            if (*end || lc.rssi > 0 || lc.rssi < -200) {
                ctl_answer("err bad RSSI %s", c);
                return 0;
            }
            if (vt_ctl_run(ctxt, &lc, welcome)) {
                ctl_answer("err no link %d-%d", src, dst);
                return 0;
            }
        }
        ctl_answer("ok");
        return 0;
    }
    if (!strcmp(cmd, "neighbors")) {
        // "ok ID:RSSI ...": the hearing graph, with the stored mean level of
        // each link (the weaker direction, dBm)
        char *buf = NULL;
        size_t sz = 0;
        FILE *f;
        n = ctl_id(ctl_word(&rest));
        if (n < 0) {
            ctl_answer("err usage: neighbors ID");
            return 0;
        }
        f = open_memstream(&buf, &sz);
        FATAL_ON(!f, 1, "open_memstream: %m");
        fputs("ok", f);
        for (int i = 0; i < MAX_NODES; i++)
            if (i != n && bitmap_get(i, ctxt->node_graph[n], MAX_NODES / 64)) {
                float x = sim_link(nodes_infos_flat_map, n, i)->rssi, y = sim_link(nodes_infos_flat_map, i, n)->rssi;
                fprintf(f, " %d:%.1f", i, x < y ? x : y);
            }
        fclose(f);
        ctl_answer("%s", buf);
        free(buf);
        return 0;
    }
    if (!strcmp(cmd, "intf")) {
        char err[300];
        a = ctl_word(&rest);
        if (!med_central()) {
            ctl_answer("err interferers need the central medium");
            return 0;
        }
        if (a && !strcmp(a, "add") && *rest) {
            const char *name = intf_add_runtime(rest, vt_now, err, sizeof(err));
            if (name)
                ctl_answer("ok %s", name);
            else
                ctl_answer("err %s", err);
            return 0;
        }
        if (a && !strcmp(a, "remove") && *rest) {
            n = intf_remove_runtime(rest, vt_now);
            if (n)
                ctl_answer("ok %d", n);
            else
                ctl_answer("err no active interferer %s", rest);
            return 0;
        }
        ctl_answer("err usage: intf add SPEC | intf remove NAME|all");
        return 0;
    }
    if (!strcmp(cmd, "counters")) {
        char outc[1024];
        int len2 = snprintf(outc, sizeof(outc), "ok t_us=%llu wakes=%llu frames_in=%llu frames_out=%llu airtime_us=%llu",
                            (unsigned long long)vt_now, (unsigned long long)vt_wakes, (unsigned long long)vt_frames_in,
                            (unsigned long long)vt_frames_out, (unsigned long long)vt_airtime_us);
        if (med_central())
            med_outcomes_str(outc + len2, sizeof(outc) - len2);
        ctl_answer("%s", outc);
        return 0;
    }
    if (!strcmp(cmd, "rxcounters")) {
        // "ok ID:HEARD:COLLISIONS:OK ...": per receiver, totals so far
        // (central medium; "ok" alone otherwise)
        char *buf = NULL;
        size_t sz = 0;
        FILE *f = open_memstream(&buf, &sz);
        FATAL_ON(!f, 1, "open_memstream: %m");
        fputs("ok", f);
        if (med_central())
            med_rx_counters(f);
        fclose(f);
        ctl_answer("%s", buf);
        free(buf);
        return 0;
    }
    if (!strcmp(cmd, "power")) {
        FILE *f;
        a = ctl_word(&rest);
        b = ctl_word(&rest);
        if (!pwr_enabled()) {
            ctl_answer("err no power recording (--power)");
            return 0;
        }
        if (!a || strcmp(a, "snap") || !b) {
            ctl_answer("err usage: power snap FILE TAG");
            return 0;
        }
        f = fopen(b, "a");
        if (!f) {
            ctl_answer("err %s: %s", b, strerror(errno));
            return 0;
        }
        pwr_snapshot(f, *rest ? rest : "-", vt_now);
        fclose(f);
        ctl_answer("ok");
        return 0;
    }
    ctl_answer("err unknown command %s", cmd);
    return 0;
}

// Paused: answer the driver until it allows time to go on. Returns 0 to go
// on, 1 when the driver ends the run ("quit"), -1 on an error, -3 when the
// driver is gone (its connection closed without "quit": the run fails).
static int ctl_pause(struct ctxt *ctxt, struct nsim_welcome *welcome)
{
    static char *line;
    static size_t cap;
    ssize_t len;
    int r;

    if (ctl_until > vt_now)
        vt_now = ctl_until;      // nothing happened in between: commands act at the pause time
    ctl_answer("at %llu", (unsigned long long)vt_now);
    for (;;) {
        len = getline(&line, &cap, ctl_in);
        if (len < 0) {
            WARN("scenario: the driver closed the control connection without \"quit\"");
            return -3;
        }
        line[strcspn(line, "\r\n")] = '\0';
        r = ctl_command(ctxt, line, welcome);
        if (r == 2)
            return 0;            // events up to ctl_until run, then the next pause answers
        if (r == 1)
            return 1;
        if (r < 0)
            return -1;
    }
}

static int vt_run(struct ctxt *ctxt)
{
    struct nsim_welcome welcome = { .magic = "xw", .version = NSIM_PROTO_VERSION, .seed = 1 };
    struct timespec start, end;
    const char *seed_env = getenv("WSSIM_SEED");
    uint64_t same_time = 0, t;
    const char *reason;
    int status = 0;
    double wall;
    int i, node;

    if (seed_env && *seed_env)
        welcome.seed = strtoull(seed_env, NULL, 0);
    serve(ctxt);
    if (stop) {
        INFO("interrupted while waiting for %d nodes (%d connected)", ctxt->vtime_nodes, num_connected);
        for (node = 0; node < MAX_NODES; node++)
            if (slot_of_node[node])
                vt_nodes[vt_num_nodes++] = node;
        vt_quit_all();
        return 0;
    }
    // No more connections: the run is complete (a scenario driver may start
    // new instances: the listening socket stays open for them).
    if (!ctl_path) {
        close(fds[0].fd);
        fds[0].fd = -1;
    } else {
        fds[0].events = 0;
    }
    for (i = 1; i < fd_limit; i++)
        if (fds[i].fd >= 0 && conns[i].node < 0)
            conn_close(i);
    for (node = 0; node < MAX_NODES; node++)
        if (slot_of_node[node])
            vt_nodes[vt_num_nodes++] = node;
    INFO("virtual time: %d nodes, seed %llu%s", vt_num_nodes, (unsigned long long)welcome.seed,
         ctxt->has_vtime_end ? "" : ", no end time");
    if (pwr_enabled()) {
        welcome.flags |= NSIM_WELCOME_POWER;
        pwr_start(vt_num_nodes, vt_nodes);
    }
    if (med_central()) {
        welcome.flags |= NSIM_WELCOME_MEDIUM;
        med_start(welcome.seed, vt_cca_shm, nodes_infos_flat_map);
    }

    if (vt_frames_path) {
        vt_frames_file = fopen(vt_frames_path, "w");
        FATAL_ON(!vt_frames_file, 1, "%s: %m", vt_frames_path);
    }
    // --vtime-power-up: the node's connection waits for its start time, as
    // an instance of --vtime-start does; its radio is off meanwhile
    for (i = 0; i < vt_ctls_n; i++) {
        if (vt_ctls[i].kind != VT_CTL_START || !vt_ctls[i].first)
            continue;
        node = vt_ctls[i].node;
        FATAL_ON(!slot_of_node[node], 1, "--vtime-power-up: node %d is not connected (or named twice)", node);
        vt_ctls[i].slot = slot_of_node[node];
        slot_of_node[node] = 0;
        if (pwr_enabled())
            pwr_power_up(node, vt_ctls[i].t);   // its ledger starts then, even after the end
        if (vt_cca_shm) {
            vt_cca_shm[node * SIM_SHM_CCA_STRIDE] = 7;   // DRVSTATE_OFF (app_wshwsim/sl_rf_driver.c)
            vt_cca_shm[node * SIM_SHM_CCA_STRIDE + 3] = SIM_RADIO_OFF;
        }
    }
    if (ctl_path)
        ctl_accept();
    clock_gettime(CLOCK_MONOTONIC, &start);
    // Welcome the nodes one at a time, in ID order, and read each one's first
    // run at time 0 until its "xi": no two nodes ever run at once.
    for (i = 0; i < vt_num_nodes; i++) {
        node = vt_nodes[i];
        if (!slot_of_node[node])
            continue;   // powered up later
        if (pwr_enabled())
            pwr_wake(node, 0, 0);
        welcome.drift_ppb = drift_of(node, welcome.seed);
        if (welcome.drift_ppb)
            INFO("virtual time: node %d clock drift %+.3f ppm", node, welcome.drift_ppb / 1000.0);
        if (vt_welcome(node, &welcome) || vt_read_until_idle(ctxt, node)) {
            status = stop ? 0 : 1;
            reason = stop ? "interrupted" : "a node failed";
            goto out;
        }
    }

    for (;;) {
        if (stop) {
            reason = "interrupted";
            break;
        }
        vt_drop_stale();
        // Events of a node that is stopped (frames, its old deadline)
        while (vt_heap_len && !slot_of_node[vt_heap[0].node]) {
            struct vt_event ev = vt_pop();
            if (ev.kind == VT_EV_FRAME || ev.kind == VT_EV_CTL)
                vt_unref(ev.frame);
            else if (ev.kind == VT_EV_VERDICT)
                med_lock_free(ev.lock);
            vt_drop_stale();
        }
        if (ctl_fd >= 0) {
            uint64_t next = UINT64_MAX;
            int r;

            if (vt_ctls_next < vt_ctls_n)
                next = vt_ctls[vt_ctls_next].t;
            if (vt_heap_len && vt_heap[0].t < next)
                next = vt_heap[0].t;
            if (next > ctl_until && !(ctxt->has_vtime_end && next > ctxt->vtime_end_us && ctl_until >= ctxt->vtime_end_us)) {
                r = ctl_pause(ctxt, &welcome);
                if (r == 1) {
                    reason = "scenario end";
                    break;
                }
                if (r == -3) {
                    reason = "scenario driver lost";
                    status = 1;
                    break;
                }
                if (r < 0) {
                    reason = stop ? "interrupted" : "a node failed";
                    status = stop ? 0 : 1;
                    break;
                }
                continue;
            }
        }
        if (vt_ctls_next < vt_ctls_n && (!vt_heap_len || vt_ctls[vt_ctls_next].t <= vt_heap[0].t) &&
            !(ctxt->has_vtime_end && vt_ctls[vt_ctls_next].t > ctxt->vtime_end_us)) {
            if (vt_ctl_run(ctxt, &vt_ctls[vt_ctls_next++], &welcome)) {
                reason = stop ? "interrupted" : "a node failed";
                status = stop ? 0 : 1;
                break;
            }
            continue;
        }
        if (!vt_heap_len) {
            reason = "no more events";
            break;
        }
        t = vt_heap[0].t;
        node = vt_heap[0].node;
        if (ctxt->has_vtime_end && t > ctxt->vtime_end_us) {
            reason = "end time reached";
            break;
        }
        if (t == vt_now && vt_wakes) {
            if (++same_time > VT_SAME_TIME_CAP) {
                WARN("more than %d wakes in a row at %llu us: stopped", VT_SAME_TIME_CAP,
                     (unsigned long long)t);
                vt_log_head();
                reason = "same-time loop";
                status = 1;
                break;
            }
        } else {
            same_time = 1;
        }
        if (vt_wake(ctxt, node, t)) {
            reason = stop ? "interrupted" : "a node failed";
            status = stop ? 0 : 1;
            break;
        }
    }
out:
    clock_gettime(CLOCK_MONOTONIC, &end);
    if (ctl_fd >= 0) {
        dprintf(ctl_fd, "ended %llu %s\n", (unsigned long long)vt_now, reason);
        if (ctl_in)
            fclose(ctl_in);     // its own descriptor (a dup)
        close(ctl_fd);
        ctl_fd = -1;
        ctl_in = NULL;
    }
    vt_quit_all();
    wall = timespec_diff(&start, &end);
    INFO("virtual time: end (%s)", reason);
    if (vt_frames_file)
        fclose(vt_frames_file);
    INFO("virtual time: reached %llu.%06llu s, %llu wakes, %llu frames sent, %llu frames delivered",
         (unsigned long long)(vt_now / 1000000), (unsigned long long)(vt_now % 1000000),
         (unsigned long long)vt_wakes, (unsigned long long)vt_frames_in,
         (unsigned long long)vt_frames_out);
    INFO("virtual time: wall %.3f s, %.6f wall s per simulated s", wall,
         vt_now ? wall / (vt_now / 1e6) : 0.0);
    if (med_central()) {
        INFO("medium: %llu verdicts delivered, %llu CCA queries answered", (unsigned long long)vt_verdicts_out,
             (unsigned long long)vt_senses);
        if (vt_sniffs_in)
            INFO("medium: %llu sniffs requested, %llu sniff results delivered", (unsigned long long)vt_sniffs_in,
                 (unsigned long long)vt_sniff_results_out);
        intf_advance(vt_now);   // the run's end, for the interferers' statistics
        med_finish();
    }
    for (node = 0; node < MAX_NODES; node++)
        for (i = 0; i < RI_ERR_COUNT; i++)
            if (vt_ri[node].errors[i])
                WARN("radio interface: node %d: %llu %s", node, (unsigned long long)vt_ri[node].errors[i],
                     ri_err_names[i]);
    // A run that reached its end time lasted until then; any other, until
    // its last wake.
    if (pwr_enabled())
        pwr_finish(!strcmp(reason, "end time reached") || !strcmp(reason, "no more events")
                   ? (ctxt->has_vtime_end ? ctxt->vtime_end_us : vt_now) : vt_now, welcome.seed);
    return status;
}

// Shared memory: create (zero-filled) and map one object, after unlinking any
// leftover so that a new run never sees the tables of the previous one.
static void *shm_create(const char *name, size_t size, int *fd)
{
    void *ptr;

    // One server per table: the server holds an exclusive lock on it while it
    // runs, so a second server on the same socket path fails here instead of
    // sharing (or unlinking) the first one's radio states and links. A table
    // left over by a crashed run is not locked: it is reused, emptied.
    *fd = shm_open(name, O_CREAT | O_RDWR, 0666);
    FATAL_ON(*fd < 0, 1, "shm_open %s: %m", name);
    FATAL_ON(flock(*fd, LOCK_EX | LOCK_NB) < 0 && errno == EWOULDBLOCK, 1,
             "%s: another server uses this socket path; give each run its own", name);
    FATAL_ON(ftruncate(*fd, 0) < 0 || ftruncate(*fd, size) < 0, 1, "ftruncate %s: %m", name);
    ptr = mmap(0, size, PROT_READ | PROT_WRITE, MAP_SHARED, *fd, 0);
    FATAL_ON(ptr == MAP_FAILED, 1, "mmap %s: %m", name);
    return ptr;
}

int main(int argc, char **argv)
{
    struct sigaction sa = { .sa_handler = signal_handler };
    static struct ctxt ctxt = {
        .addr.sun_family = AF_UNIX
    };
    char shm_name[192], shm_infos_name[192];
    int shm_fd, shm_infos_fd;
    void *ptr, *infos_ptr;
    int status = 0;
    int i;

    // No SA_RESTART, so that poll() and recv() return on SIGINT and SIGTERM
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);
    // Colours only on a terminal: a log file gets plain text
    g_enable_color_traces = isatty(STDERR_FILENO);

    parse_commandline(&ctxt, argc, argv);

    // Shared memory for CCA, and for nodes and edges information. Their names
    // belong to the socket path.
    sim_shm_name(shm_name, sizeof(shm_name), "wssimcca", ctxt.addr.sun_path);
    sim_shm_name(shm_infos_name, sizeof(shm_infos_name), "wssimnodesinfos", ctxt.addr.sun_path);
    ptr = shm_create(shm_name, SIM_SHM_CCA_SIZE, &shm_fd);
    infos_ptr = shm_create(shm_infos_name, SIM_SHM_INFOS_SIZE, &shm_infos_fd);
    nodes_infos_flat_map = (struct node_infos *)infos_ptr;
    vt_cca_shm = ptr;
    apply_groups(&ctxt);

    fd_limit = min(increase_limit_fd(), ARRAY_SIZE(fds));
    FATAL_ON(ctxt.vtime_nodes && ctxt.vtime_nodes > fd_limit - 1, 1,
             "--vtime=%d: only %d connections possible", ctxt.vtime_nodes, fd_limit - 1);
    for (i = 0; i < fd_limit; i++) {
        fds[i].fd = -1;
        conns[i].node = -1;
    }
    listen_socket(&ctxt);
    if (ctl_path) {
        FATAL_ON(!ctxt.vtime_nodes, 1, "--vtime-control needs --vtime");
        ctl_listen();
        if (med_central())
            intf_set_dynamic();
    }

    if (ctxt.vtime_nodes)
        status = vt_run(&ctxt);
    else
        serve(&ctxt);

    // Graceful teardown
    INFO("Graceful teardown");
    for (i = 1; i < fd_limit; i++)
        if (fds[i].fd >= 0)
            close(fds[i].fd);
    if (fds[0].fd >= 0)
        close(fds[0].fd);
    unlink(ctxt.addr.sun_path);
    if (ctl_path)
        unlink(ctl_path);
    munmap(ptr, SIM_SHM_CCA_SIZE);
    munmap(infos_ptr, SIM_SHM_INFOS_SIZE);
    shm_unlink(shm_name);         // before the lock goes with the close
    shm_unlink(shm_infos_name);
    close(shm_fd);
    close(shm_infos_fd);
    return status;
}
