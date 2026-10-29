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
#include <stdlib.h>
#include <time.h>

#include "sim_proto.h"
#include "ns_port_sim.h"
#include "sim_power.h"

#define MAX_NODES SIM_MAX_NODES

struct node_infos *nodes_infos_flat_map;

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

static int rssi_map_parse_for_one_group(char *str)
{
    char *range;
    char *endptr;
    int nodes_in_group[MAX_NODES] = {-1};
    memset(nodes_in_group, -1, sizeof(nodes_in_group));
    int nodes_in_group_idx = 0;

    char *str_copy = strdup(str);
    if (str_copy == NULL) {
        return -1;
    }

    char *rest = str_copy;
    char *indexes_part, *rssi_part;
    float rssi = 0.0;
    indexes_part = __strtok_r(rest, ":", &rest);


    if (indexes_part == NULL || strstr(str, ":") == NULL) {
        // No RSSI has been specified
        free(str_copy);
        return 0;
    }

    rssi_part = __strtok_r(rest, ":", &rest);

    if (rssi_part != NULL) {
        rssi = (float)atof(rssi_part);
    }

    strcpy(str_copy, str);
    rest = str_copy;
    indexes_part = __strtok_r(rest, ":", &rest);

    // Map the RSSI
    range = strtok(indexes_part, ",");
    unsigned long start_node = strtoul(range, &endptr, 0);
    unsigned long end_node;
    if (*endptr == '-') {
        range = ++endptr;
        end_node = strtoul(range, &endptr, 0);

        if (end_node >= MAX_NODES) {
            free(str_copy);
            return -1;
        }
        for (int i = start_node; i <= end_node; i++)
            for (int j = start_node; j <= end_node; j++)
                if (i != j)
                    sim_link(nodes_infos_flat_map, i, j)->rssi = rssi;
    } else {
        // A list of IDs: count them, at most MAX_NODES (more is malformed)
        nodes_in_group[nodes_in_group_idx++] = start_node;
        while ((range = strtok(NULL, ","))) {
            if (nodes_in_group_idx >= MAX_NODES) {
                free(str_copy);
                return -1;
            }
            nodes_in_group[nodes_in_group_idx++] = strtoul(range, &endptr, 0);
        }
        for (int i = 0; i < nodes_in_group_idx; i++) {
            if (nodes_in_group[i] < 0 || nodes_in_group[i] >= MAX_NODES) {
                free(str_copy);
                return -1;
            }
        }
        for (int i = 0; i < nodes_in_group_idx; i++) {
            for (int j = 0; j < nodes_in_group_idx; j++) {
                if (i != j) {
                    sim_link(nodes_infos_flat_map, nodes_in_group[i], nodes_in_group[j])->rssi = rssi;
                    sim_link(nodes_infos_flat_map, nodes_in_group[j], nodes_in_group[i])->rssi = rssi;
                }
            }
        }
    }

    free(str_copy);

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

    printf("\nRSSI map:\n");
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
    fprintf(stream, "  -g, --group=NODES[:RSSI]  link these nodes (e.g. 0-2, or 2,5:-67.0)\n");
    fprintf(stream, "  -l, --dump                print the topology and the RSSI map\n");
    fprintf(stream, "  -f, --fpath[=FILE]        with -l, also write the edges to FILE (/tmp/graph.txt)\n");
    fprintf(stream, "  --vtime=N                 virtual time: conduct N nodes (docs/VIRTUAL_TIME.md)\n");
    fprintf(stream, "  --vtime-end=SECONDS       virtual time: end the run at this time\n");
    fprintf(stream, "  --drift=[IDS:]PPM         virtual time: clock error of these nodes (all if no IDS), in ppm;\n");
    fprintf(stream, "                            repeatable, the last one naming a node wins\n");
    fprintf(stream, "  --drift-random=PPM        virtual time: each node draws its clock error in [-PPM, +PPM]\n");
    fprintf(stream, "                            from WSSIM_SEED (a --drift naming the node takes precedence)\n");
    fprintf(stream, "  --power=PROFILE           virtual time: record power, every node with this profile\n");
    fprintf(stream, "                            (docs/POWER_MODEL.md)\n");
    fprintf(stream, "  --power-node=IDS:PROFILE  another profile for these nodes (3, 2-5, 1,4); repeatable\n");
    fprintf(stream, "  --power-mode=[IDS:]MODE   profile values of this PHY mode (1a ... 5), common, or auto (the\n");
    fprintf(stream, "                            default: the mode the node's MAC configured); repeatable\n");
    fprintf(stream, "  --power-out=FILE          the power report (default power.json)\n");
    fprintf(stream, "  --power-log=FILE          also log every state interval of every node\n");
    fprintf(stream, "  --power-cell=MAH[:V[:EFF]] battery life for this cell (capacity, voltage, converter efficiency)\n");
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
};

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

void parse_commandline(struct ctxt *ctxt, int argc, char *argv[])
{
    const char *opts_short = "hlf::g:";
    static const struct option opts_long[] = {
        { "group", required_argument, 0,  'g' },
        { "fpath", optional_argument, 0, 'f'},
        { "dump",  no_argument,       0,  'l' },
        { "help",  no_argument,       0,  'h' },
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
        { 0,       0,                 0,   0  }
    };
    uint64_t mask[MAX_NODES / 64];
    char *end;
    double d;
    int opt;

    while ((opt = getopt_long(argc, argv, opts_short, opts_long, NULL)) != -1) {
        switch (opt) {
            case 'g':
                FATAL_ON(bitmap_parse(optarg, mask, MAX_NODES / 64), 1, "Bad mask: %s", optarg);
                if (!ctxt->groups)
                    ctxt->groups = calloc(argc, sizeof(*ctxt->groups));
                FATAL_ON(!ctxt->groups, 1, "calloc: %m");
                ctxt->groups[ctxt->num_groups++] = optarg;
                break;
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
            case 'h':
                print_help(stdout, 0);
                break;
            case '?':
                print_help(stderr, 1);
                break;
            default:
                break;
        }
    }
    FATAL_ON(ctxt->has_vtime_end && !ctxt->vtime_nodes, 1, "--vtime-end needs --vtime");
    FATAL_ON(pwr_enabled() && !ctxt->vtime_nodes, 1, "--power needs --vtime");
    FATAL_ON((drift_specs_n || drift_random_ppb) && !ctxt->vtime_nodes, 1, "--drift needs --vtime");
    FATAL_ON(ctxt->dump && !ctxt->num_groups, 1, "No graph to dump");
    if (optind >= argc)
        FATAL(1, "Expected argument: socket path");
    if (optind + 1 < argc)
        FATAL(1, "Too many arguments argument: %s", argv[optind + 1]);
    FATAL_ON(strlen(argv[optind]) >= sizeof(ctxt->addr.sun_path), 1);
    strcpy(ctxt->addr.sun_path, argv[optind]);
}

// Build the graph and the RSSI map from the -g options. The RSSI map lives in
// shared memory, whose names depend on the socket path: this runs after both.
static void apply_groups(struct ctxt *ctxt)
{
    uint64_t mask[MAX_NODES / 64];
    int i;

    for (i = 0; i < ctxt->num_groups; i++) {
        FATAL_ON(bitmap_parse(ctxt->groups[i], mask, MAX_NODES / 64), 1, "Bad mask: %s", ctxt->groups[i]);
        FATAL_ON(rssi_map_parse_for_one_group(ctxt->groups[i]), 1, "Bad mask: %s", ctxt->groups[i]);
        graph_apply_mask(ctxt->node_graph, mask);
    }
    if (!ctxt->num_groups)
        memset(ctxt->node_graph, 0xFF, sizeof(ctxt->node_graph));
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
        slot_of_node[conns[slot].node] = 0;
        num_connected--;
    } else {
        DEBUG("Disconnect fd %d", fds[slot].fd);
    }
    close(fds[slot].fd);
    fds[slot].fd = -1;
    fds[slot].events = 0;
    conns[slot].node = -1;
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
    } else if (hello->node_id >= MAX_NODES) {
        WARN("fd %d: node ID %d out of range (max %d)", fds[slot].fd, hello->node_id, MAX_NODES - 1);
        conn_close(slot);
    } else if (slot_of_node[hello->node_id]) {
        WARN("fd %d: node %d is already connected", fds[slot].fd, hello->node_id);
        conn_close(slot);
    } else if (!(hello->flags & SIM_HELLO_VTIME) != !vtime) {
        WARN("fd %d: node %d runs on %s time, this server on %s time: refused", fds[slot].fd,
             hello->node_id, (hello->flags & SIM_HELLO_VTIME) ? "virtual" : "wall-clock",
             vtime ? "virtual" : "wall-clock");
        conn_close(slot);
    } else if (vtime && num_connected >= vtime_nodes) {
        WARN("fd %d: node %d: the %d nodes of --vtime are already connected: refused",
             fds[slot].fd, hello->node_id, vtime_nodes);
        conn_close(slot);
    } else {
        conns[slot].node = hello->node_id;
        slot_of_node[hello->node_id] = slot;
        num_connected++;
        if (vtime)
            fds[slot].events = 0; // only hang-ups until the run starts
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
        if (vtime && num_connected == ctxt->vtime_nodes)
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
 * An event is either a frame delivery (frame != NULL; one event per
 * receiver, all sharing one reference-counted copy of the frame) or a node's
 * deadline (frame == NULL). A node has at most one live deadline: its
 * sequence number is in vt_deadline_seq[node], and any other deadline entry
 * of that node in the heap is stale and skipped when it reaches the head.
 */
#define VT_SAME_TIME_CAP 100000

struct vt_frame {
    int refs;
    int len;
    uint8_t data[];
};

struct vt_event {
    uint64_t t;
    uint64_t seq;
    int node;
    struct vt_frame *frame;     // NULL: the node's deadline
};

static struct vt_event *vt_heap;
static size_t vt_heap_len, vt_heap_cap;
static uint64_t vt_seq = 1;                     // 0 means "no deadline"
static uint64_t vt_deadline_seq[MAX_NODES];
static int vt_nodes[MAX_NODES];                 // connected node IDs, ascending
static int vt_num_nodes;
static uint64_t vt_now;                         // time of the last wake
static uint64_t vt_wakes, vt_frames_in, vt_frames_out;

static bool vt_before(const struct vt_event *a, const struct vt_event *b)
{
    if (a->t != b->t)
        return a->t < b->t;
    if (a->node != b->node)
        return a->node < b->node;
    return a->seq < b->seq;
}

static void vt_push(uint64_t t, int node, struct vt_frame *frame)
{
    struct vt_event ev = { .t = t, .seq = vt_seq++, .node = node, .frame = frame };
    size_t i, parent;

    if (vt_heap_len == vt_heap_cap) {
        vt_heap_cap = vt_heap_cap ? vt_heap_cap * 2 : 1024;
        vt_heap = realloc(vt_heap, vt_heap_cap * sizeof(*vt_heap));
        FATAL_ON(!vt_heap, 1, "realloc: %m");
    }
    if (!frame)
        vt_deadline_seq[node] = ev.seq;
    else
        frame->refs++;
    for (i = vt_heap_len++; i > 0; i = parent) {
        parent = (i - 1) / 2;
        if (!vt_before(&ev, &vt_heap[parent]))
            break;
        vt_heap[i] = vt_heap[parent];
    }
    vt_heap[i] = ev;
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
    return !ev->frame && vt_deadline_seq[ev->node] != ev->seq;
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

// Queue a frame from src for its neighbours at the current time.
static void vt_queue_frame(struct ctxt *ctxt, int src, void *msg, int len)
{
    struct sim_frame_hdr *hdr = msg;
    struct vt_frame *frame;
    int i;

    if (!frame_check(src, msg, len))
        return;
    hdr->src = src;
    vt_frames_in++;
    frame = malloc(sizeof(*frame) + len);
    FATAL_ON(!frame, 1, "malloc: %m");
    frame->refs = 1;            // held until the loop below ends
    frame->len = len;
    memcpy(frame->data, msg, len);
    for (i = 0; i < vt_num_nodes; i++)
        if (bitmap_get(vt_nodes[i], ctxt->node_graph[src], MAX_NODES / 64))
            vt_push(vt_now, vt_nodes[i], frame);
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
            if (pwr_enabled())
                pwr_idle(node, vt_now, idle->next_us);
            vt_set_deadline(node, idle->next_us);
            return 0;
        }
        if (len >= 2 && !memcmp(buf, "xx", 2))
            vt_queue_frame(ctxt, node, buf, len);
        else if (pwr_enabled() && len == sizeof(struct sim_radio_report) && !memcmp(buf, "xr", 2))
            pwr_radio(node, vt_now, (const struct sim_radio_report *)buf);
        else if (pwr_enabled() && len == sizeof(struct nsim_wake) && !memcmp(buf, "xc", 2))
            pwr_wake_report(node, (const struct nsim_wake *)buf);
        else
            WARN("node %d: unexpected message (%zd bytes, \"%.2s\"), dropped", node, len, buf);
    }
}

// Wake node at time t: "xa", then its frames due at t in queue order (they
// are at the head of the heap). The advance says how many frames follow: the
// node may read the "xa" before they are in its socket, as they are separate
// messages, so it waits for exactly that many.
static int vt_wake(struct ctxt *ctxt, int node, uint64_t t)
{
    static struct vt_frame **frames;
    static size_t frames_cap;
    struct nsim_advance adv = { .magic = "xa", .now_us = t };
    struct vt_event ev;
    size_t num = 0, i;
    int ret = 0;

    while (vt_heap_len && vt_heap[0].t == t && vt_heap[0].node == node) {
        ev = vt_pop();
        if (!ev.frame) {
            if (ev.seq == vt_deadline_seq[node])
                vt_deadline_seq[node] = 0; // consumed: the node reports its next one
            continue;
        }
        if (num == frames_cap) {
            frames_cap = frames_cap ? frames_cap * 2 : 64;
            frames = realloc(frames, frames_cap * sizeof(*frames));
            FATAL_ON(!frames, 1, "realloc: %m");
        }
        frames[num++] = ev.frame;
    }
    FATAL_ON(num > UINT16_MAX, 1, "node %d: %zu frames due at %" PRIu64 " us", node, num, t);
    adv.frames = num;
    vt_now = t;
    vt_wakes++;
    if (pwr_enabled())
        pwr_wake(node, t, num);
    if (vt_send(node, &adv, sizeof(adv)))
        ret = -1;
    for (i = 0; i < num; i++) {
        if (!ret && vt_send(node, frames[i]->data, frames[i]->len))
            ret = -1;
        vt_frames_out++;
        vt_unref(frames[i]);
    }
    if (ret)
        return ret;
    return vt_read_until_idle(ctxt, node);
}

static void vt_log_head(void)
{
    size_t i, n = vt_heap_len < 10 ? vt_heap_len : 10;

    WARN("queue head (%zu events):", vt_heap_len);
    // The heap array is not sorted; its first entries are a good sample.
    for (i = 0; i < n; i++)
        WARN("  t=%llu us node=%d seq=%llu %s%s", (unsigned long long)vt_heap[i].t, vt_heap[i].node,
             (unsigned long long)vt_heap[i].seq, vt_heap[i].frame ? "frame" : "deadline",
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
    // No more connections: the run is complete.
    close(fds[0].fd);
    fds[0].fd = -1;
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

    clock_gettime(CLOCK_MONOTONIC, &start);
    // Welcome the nodes one at a time, in ID order, and read each one's first
    // run at time 0 until its "xi": no two nodes ever run at once.
    for (i = 0; i < vt_num_nodes; i++) {
        node = vt_nodes[i];
        if (pwr_enabled())
            pwr_wake(node, 0, 0);
        welcome.drift_ppb = drift_of(node, welcome.seed);
        if (welcome.drift_ppb)
            INFO("virtual time: node %d clock drift %+.3f ppm", node, welcome.drift_ppb / 1000.0);
        if (vt_send(node, &welcome, sizeof(welcome)) || vt_read_until_idle(ctxt, node)) {
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
    vt_quit_all();
    wall = timespec_diff(&start, &end);
    INFO("virtual time: end (%s)", reason);
    INFO("virtual time: reached %llu.%06llu s, %llu wakes, %llu frames sent, %llu frames delivered",
         (unsigned long long)(vt_now / 1000000), (unsigned long long)(vt_now % 1000000),
         (unsigned long long)vt_wakes, (unsigned long long)vt_frames_in,
         (unsigned long long)vt_frames_out);
    INFO("virtual time: wall %.3f s, %.6f wall s per simulated s", wall,
         vt_now ? wall / (vt_now / 1e6) : 0.0);
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

    shm_unlink(name);
    *fd = shm_open(name, O_CREAT | O_RDWR, 0666);
    FATAL_ON(*fd < 0, 1, "shm_open %s: %m", name);
    FATAL_ON(ftruncate(*fd, size) < 0, 1, "ftruncate %s: %m", name);
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

    parse_commandline(&ctxt, argc, argv);

    // Shared memory for CCA, and for nodes and edges information. Their names
    // belong to the socket path.
    sim_shm_name(shm_name, sizeof(shm_name), "wssimcca", ctxt.addr.sun_path);
    sim_shm_name(shm_infos_name, sizeof(shm_infos_name), "wssimnodesinfos", ctxt.addr.sun_path);
    ptr = shm_create(shm_name, SIM_SHM_CCA_SIZE, &shm_fd);
    infos_ptr = shm_create(shm_infos_name, SIM_SHM_INFOS_SIZE, &shm_infos_fd);
    nodes_infos_flat_map = (struct node_infos *)infos_ptr;
    apply_groups(&ctxt);

    fd_limit = min(increase_limit_fd(), ARRAY_SIZE(fds));
    FATAL_ON(ctxt.vtime_nodes && ctxt.vtime_nodes > fd_limit - 1, 1,
             "--vtime=%d: only %d connections possible", ctxt.vtime_nodes, fd_limit - 1);
    for (i = 0; i < fd_limit; i++) {
        fds[i].fd = -1;
        conns[i].node = -1;
    }
    listen_socket(&ctxt);

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
    munmap(ptr, SIM_SHM_CCA_SIZE);
    munmap(infos_ptr, SIM_SHM_INFOS_SIZE);
    close(shm_fd);
    close(shm_infos_fd);
    shm_unlink(shm_name);
    shm_unlink(shm_infos_name);
    return status;
}
