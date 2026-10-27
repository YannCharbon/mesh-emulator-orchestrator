#include <stdint.h>
#include <stdbool.h>
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

#include "sim_proto.h"

#define MAX_NODES SIM_MAX_NODES

struct node_infos *nodes_infos_flat_map;

struct ctxt {
    struct sockaddr_un addr;
    uint64_t node_graph[MAX_NODES][MAX_NODES / 64];
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
        nodes_in_group[nodes_in_group_idx++] = start_node;
        while ((range = strtok(NULL, ","))) {
            unsigned long cur_node = strtoul(range, &endptr, 0);
            nodes_in_group[nodes_in_group_idx++] = cur_node;
        }

        int i = 0, j = 0;
        while (nodes_in_group[i] != -1) {
            if (nodes_in_group[i] >= MAX_NODES) {
                free(str_copy);
                return -1;
            }
            i++;
        }
        i = 0;
        while (nodes_in_group[i] != -1) {
            while (nodes_in_group[j] != -1) {
                if (i != j) {
                    sim_link(nodes_infos_flat_map, nodes_in_group[i], nodes_in_group[j])->rssi = rssi;
                    sim_link(nodes_infos_flat_map, nodes_in_group[j], nodes_in_group[i])->rssi = rssi;
                }
                j++;
            }
            j = 0;
            i++;
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
    exit(exit_code);
}

void parse_commandline(struct ctxt *ctxt, int argc, char *argv[])
{
    const char *opts_short = "hlf::g:";
    static const struct option opts_long[] = {
        { "group", required_argument, 0,  'g' },
        { "fpath", optional_argument, 0, 'f'},
        { "dump",  no_argument,       0,  'l' },
        { "help",  no_argument,       0,  'h' },
        { 0,       0,                 0,   0  }
    };
    uint64_t mask[MAX_NODES / 64];
    bool dump = false, has_filter = false;
    bool dump2file = false;
    char dump_path[4096];
    int opt, i, ret;

    while ((opt = getopt_long(argc, argv, opts_short, opts_long, NULL)) != -1) {
        switch (opt) {
            case 'g':
                ret = bitmap_parse(optarg, mask, MAX_NODES / 64);
                ret = rssi_map_parse_for_one_group(optarg);
                FATAL_ON(ret, 1, "Bad mask: %s", optarg);
                graph_apply_mask(ctxt->node_graph, mask);
                has_filter = true;
                break;
            case 'f':
               if (optarg == NULL && optind < argc
                    && argv[optind][0] != '-')
                {
                    optarg = argv[optind++];
                }
                if (optarg == NULL)
                {
                    sprintf(dump_path, "%s", "/tmp/graph.txt");
                } else {
                    strncpy(dump_path, optarg, sizeof(dump_path)-2);
                    dump_path[sizeof(dump_path)-1] = '\n';
                }
                    dump2file = true;
                break;
            case 'l':
                dump = true;
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
    if (!has_filter)
        memset(ctxt->node_graph, 0xFF, sizeof(ctxt->node_graph));
    for (i = 0; i < MAX_NODES; i++)
        bitmap_clr(i, ctxt->node_graph[i], MAX_NODES / 64);
    if (dump) {
        FATAL_ON(!has_filter, 1, "No graph to dump");
        graph_dump(ctxt);
        if (dump2file) {
            graph_dump_to_file(ctxt, dump_path);
        }
    }
    if (optind >= argc)
        FATAL(1, "Expected argument: socket path");
    if (optind + 1 < argc)
        FATAL(1, "Too many arguments argument: %s", argv[optind + 1]);
    FATAL_ON(strlen(argv[optind]) >= sizeof(ctxt->addr.sun_path), 1);
    strcpy(ctxt->addr.sun_path, argv[optind]);
}

// A connection, and the node it carries once its hello arrived.
struct conn {
    int node;                   // -1 until the hello
};

static struct pollfd fds[MAX_NODES + 1];
static struct conn conns[MAX_NODES + 1];
static int slot_of_node[MAX_NODES];   // index in fds, or 0 if not connected
static int fd_limit;

static void conn_close(int slot)
{
    if (conns[slot].node >= 0) {
        INFO("node %d disconnected", conns[slot].node);
        slot_of_node[conns[slot].node] = 0;
    } else {
        DEBUG("Disconnect fd %d", fds[slot].fd);
    }
    close(fds[slot].fd);
    fds[slot].fd = -1;
    fds[slot].events = 0;
    conns[slot].node = -1;
}

static void conn_hello(int slot, const void *buf, int len)
{
    const struct sim_hello *hello = buf;

    if (len != sizeof(*hello) || memcmp(hello->magic, "xh", 2)) {
        WARN("fd %d: expected a hello, dropping the connection", fds[slot].fd);
        conn_close(slot);
    } else if (hello->node_id >= MAX_NODES) {
        WARN("fd %d: node ID %d out of range (max %d)", fds[slot].fd, hello->node_id, MAX_NODES - 1);
        conn_close(slot);
    } else if (slot_of_node[hello->node_id]) {
        WARN("fd %d: node %d is already connected", fds[slot].fd, hello->node_id);
        conn_close(slot);
    } else {
        conns[slot].node = hello->node_id;
        slot_of_node[hello->node_id] = slot;
        INFO("node %d connected", hello->node_id);
    }
}

// Relay a frame to the sender's neighbours, stamped with the sender's ID.
static void relay(uint64_t node_graph[MAX_NODES][MAX_NODES / 64], int slot, void *buf, int len)
{
    struct sim_frame_hdr *hdr = buf;
    int src = conns[slot].node;
    int j, dst, ret;

    if (len < (int)sizeof(*hdr) || memcmp(hdr->magic, "xx", 2) ||
        hdr->len != len - sizeof(*hdr)) {
        WARN("node %d: malformed frame (%d bytes), dropped", src, len);
        return;
    }
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

int main(int argc, char **argv)
{
    struct sigaction sa = { .sa_handler = signal_handler };

    // No SA_RESTART, so that poll() returns on SIGINT and SIGTERM
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);

    // Shared memory for CCA. Unlink first, so that a new run never sees the
    // tables of the previous one: a new object is created zero-filled.
    const char* shm_name = "/wssimcca";
    const int shm_size = SIM_SHM_CCA_SIZE;

    shm_unlink(shm_name);
    int shm_fd = shm_open(shm_name, O_CREAT | O_RDWR, 0666);
    FATAL_ON(shm_fd < 0, 1, "shm_open %s: %m", shm_name);
    FATAL_ON(ftruncate(shm_fd, shm_size) < 0, 1, "ftruncate %s: %m", shm_name);
    void* ptr = mmap(0, shm_size, PROT_READ | PROT_WRITE, MAP_SHARED, shm_fd, 0);
    FATAL_ON(ptr == MAP_FAILED, 1, "mmap %s: %m", shm_name);

    // Shared memory for nodes and edges information
    const char* shm_infos_name = "/wssimnodesinfos";
    const long shm_infos_size = SIM_SHM_INFOS_SIZE;

    shm_unlink(shm_infos_name);
    int shm_infos_fd = shm_open(shm_infos_name, O_CREAT | O_RDWR, 0666);
    FATAL_ON(shm_infos_fd < 0, 1, "shm_open %s: %m", shm_infos_name);
    FATAL_ON(ftruncate(shm_infos_fd, shm_infos_size) < 0, 1, "ftruncate %s: %m", shm_infos_name);
    void* infos_ptr = mmap(0, shm_infos_size, PROT_READ | PROT_WRITE, MAP_SHARED, shm_infos_fd, 0);
    FATAL_ON(infos_ptr == MAP_FAILED, 1, "mmap %s: %m", shm_infos_name);
    nodes_infos_flat_map = (struct node_infos *)infos_ptr;

    static char buf[sizeof(struct sim_frame_hdr) + 4096];
    int i;
    int on = 1;
    int ret, len;
    static struct ctxt ctxt = {
        .addr.sun_family = AF_UNIX
    };

    fd_limit = min(increase_limit_fd(), ARRAY_SIZE(fds));
    parse_commandline(&ctxt, argc, argv);
    for (i = 0; i < fd_limit; i++) {
        fds[i].fd = -1;
        conns[i].node = -1;
    }

    fds[0].events = POLLIN;
    fds[0].fd = socket(AF_UNIX, SOCK_SEQPACKET, 0); // use SOCK_SEQPACKET or SOCK_STREAM
    FATAL_ON(fds[0].fd < 0, 1, "socket: %s: %m", ctxt.addr.sun_path);
    ret = setsockopt(fds[0].fd, SOL_SOCKET, SO_REUSEADDR, (char *)&on, sizeof(on));
    FATAL_ON(ret < 0, 1, "setsockopt: %s: %m", ctxt.addr.sun_path);
    unlink(ctxt.addr.sun_path);
    ret = bind(fds[0].fd, (struct sockaddr *)&ctxt.addr, sizeof(ctxt.addr));
    FATAL_ON(ret < 0, 1, "bind: %s: %m", ctxt.addr.sun_path);
    ret = listen(fds[0].fd, 4096);
    FATAL_ON(ret < 0, 1, "listen: %s: %m", ctxt.addr.sun_path);

    while (!stop) {
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
            len = read(fds[i].fd, buf, sizeof(buf));
            if (len < 1)
                conn_close(i);
            else if (conns[i].node < 0)
                conn_hello(i, buf, len);
            else
                relay(ctxt.node_graph, i, buf, len);
        }
    }

    // Graceful teardown
    INFO("Graceful teardown");
    for (i = 1; i < fd_limit; i++)
        if (fds[i].fd >= 0)
            close(fds[i].fd);
    close(fds[0].fd);
    unlink(ctxt.addr.sun_path);
    munmap(ptr, shm_size);
    munmap(infos_ptr, shm_infos_size);
    close(shm_fd);
    close(shm_infos_fd);
    shm_unlink(shm_name);
    shm_unlink(shm_infos_name);
    return 0;
}
