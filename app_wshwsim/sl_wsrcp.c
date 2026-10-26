/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2021, Silicon Labs
 * Main authors:
 *     - Jérôme Pouiller <jerome.pouiller@silabs.com>
 */
#define _GNU_SOURCE
#include <pty.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <getopt.h>
#include <unistd.h>
#include <signal.h>
#include <fcntl.h>
#include <sys/select.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <sys/un.h>
#ifdef HAVE_LIBPCAP
#  include <pcap/pcap.h>
#endif

#include "mbed-client-libservice/ns_trace.h"
#include "nanostack-event-loop/eventOS_scheduler.h"
#include "nanostack/mac/sw_mac.h"

#include "ns_port/critical.h"
#include "common/bus_uart.h"
#include "common/os_scheduler.h"
#include "common/os_timer.h"
#include "common/slist.h"
#include "common/log.h"
#include "common/fhss_timer.h"
#include "sl_wsrcp.h"
#include "sl_wsrcp_mac.h"
#include "sl_rf_driver.h"
#include "version.h"
#include <sched.h>
#include <pthread.h>

#define TRACE_GROUP  "main"


// See warning in wsmac.h
struct wsmac_ctxt g_ctxt = { };
// FIXME: should be const
mac_description_storage_size_t g_storage_sizes = {
    .device_description_table_size = ARRAY_SIZE(g_ctxt.neighbor_timings),
    .key_description_table_size = 4,
    .key_lookup_size = 1,
    .key_usage_size = 3,
};

void print_help(FILE *stream, int exit_code) {
    fprintf(stream, "\n");
    fprintf(stream, "Start Wi-SUN MAC emulation\n");
    fprintf(stream, "\n");
    fprintf(stream, "Usage:\n");
    fprintf(stream, "  wisun-mac [OPTIONS] UART_DEVICE_NET SOCKET_RF\n");
    fprintf(stream, "\n");
    fprintf(stream, "Options:\n");
    fprintf(stream, "  -m, --eui64=ADDR      Set MAC address (EUI64) to ADDR (default: random)\n");
    fprintf(stream, "  -T, --trace=TAG[,TAG] Enable traces marked with TAG. Valid tags: rf, chan,\n");
    fprintf(stream, "                        bus, hdlc, hif\n");
    fprintf(stream, "  -c, --pcap=FILE       Dump RF data to FILE\n");
    fprintf(stream, "  -w, --wireshark       Invoke wireshark and dump RF data into\n");
    fprintf(stream, "\n");
    fprintf(stream, "Examples:\n");
    fprintf(stream, "  wisun-mac /dev/pts/7 /tmp/rf_server\n");
    exit(exit_code);
}

void configure_pcap_output(struct wsmac_ctxt *ctxt, const char *filename)
{
#ifdef HAVE_LIBPCAP
    ctxt->pcap_ctxt = pcap_open_dead(DLT_IEEE802_15_4_NOFCS, 0xFFFF);
    pcap_set_immediate_mode(ctxt->pcap_ctxt, 1);
    ctxt->pcap_dumper = pcap_dump_open(ctxt->pcap_ctxt, filename);
    FATAL_ON(!ctxt->pcap_dumper, 1, "%s: %s", optarg, pcap_geterr(ctxt->pcap_ctxt));
#else
    FATAL(1, "Support for libcpap not compiled");
#endif
}

static void invoke_wireshark(struct wsmac_ctxt *ctxt)
{
    int ret;
    char file[] = "/tmp/wireshark-ws-XXXXXX";

    mktemp(file);
    mkfifo(file, 0600);
    ret = fork();
    FATAL_ON(ret < 0, 2);
    if (!ret) {
        execlp("wireshark", "wireshark", "-k", "-i", file, NULL);
        printf("wireshark invocation fail\n");
        exit(2);
    } else {
        configure_pcap_output(ctxt, file);
    }
}

void configure_mac(struct wsmac_ctxt *ctxt, const char *str)
{
    uint8_t *val = ctxt->eui64;
    int ret;

    ret = sscanf(str, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx:%hhx:%hhx%*c",
                 &val[0], &val[1], &val[2], &val[3],
                 &val[4], &val[5], &val[6], &val[7]);
    FATAL_ON(ret != 8, 1, "malformated EUI64");
}

void fill_random(void *dest, size_t len)
{
    int rnd = open("/dev/urandom", O_RDONLY);

    read(rnd, dest, len);
    close(rnd);
}

int pty_open(const char *dest_path, int bitrate, bool hardflow)
{
    static const struct {
        int val;
        int symbolic;
    } conversion[] = {
        { 9600, B9600 },
        { 19200, B19200 },
        { 38400, B38400 },
        { 57600, B57600 },
        { 115200, B115200 },
        { 230400, B230400 },
        { 460800, B460800 },
        { 921600, B921600 },
    };
    char pty_path[255];
    struct termios tty = { };
    int sym_bitrate = -1;
    int master, slave;
    int i, ret;

    for (i = 0; i < ARRAY_SIZE(conversion); i++)
        if (conversion[i].val == bitrate)
            sym_bitrate = conversion[i].symbolic;
    if (sym_bitrate < 0)
        FATAL(1, "invalid bitrate: %d", bitrate);
    cfsetispeed(&tty, sym_bitrate);
    cfsetospeed(&tty, sym_bitrate);
    cfmakeraw(&tty);
    tty.c_cc[VTIME] = 0;
    tty.c_cc[VMIN] = 1;
    tty.c_iflag &= ~IXON;
    tty.c_iflag &= ~IXOFF;
    tty.c_iflag &= ~IXANY;
    tty.c_cflag &= ~HUPCL;
    tty.c_cflag |= CLOCAL;
    if (hardflow)
        tty.c_cflag |= CRTSCTS;
    else
        tty.c_cflag &= ~CRTSCTS;
    ret = openpty(&master, &slave, pty_path, &tty, NULL);
    if (ret < 0)
        FATAL(1, "openpty: %m");
    ret = unlink(dest_path);
    if (ret < 0 && errno != ENOENT)
        FATAL(1, "unlink: %m");
    ret = symlink(pty_path, dest_path);
    if (ret < 0)
        FATAL(1, "symlink: %m");
    return master;
}

void configure(struct wsmac_ctxt *ctxt, int argc, char *argv[])
{
    static const struct {
        char *name;
        int val;
    } valid_traces[] = {
        { "rf",   TR_RF },
        { "chan", TR_CHAN },
        { "bus",  TR_BUS },
        { "hdlc", TR_HDLC },
        { "hif",  TR_HIF },
    };
    static const struct option opt_list[] = {
        { "eui64",     required_argument, 0, 'm' },
        { "pcap",      required_argument, 0, 'c' },
        { "trace",     required_argument, 0, 'T' },
        { "wireshark", no_argument,       0, 'w' },
        { "help",      no_argument,       0, 'h' },
        { 0,           0,                 0,  0  }
    };
    char *tag;
    int opt, i;

    fill_random(ctxt->eui64, sizeof(ctxt->eui64));
    ctxt->eui64[0] &= ~1;
    ctxt->eui64[0] |= 2;
    while ((opt = getopt_long(argc, argv, "hm:c:T:w", opt_list, NULL)) != -1) {
        switch (opt) {
            case 'm':
                configure_mac(ctxt, optarg);
                break;
            case 'T':
                tag = strtok(optarg, ",");
                do {
                    for (i = 0; i < ARRAY_SIZE(valid_traces); i++) {
                        if (!strcmp(valid_traces[i].name, tag)) {
                            g_enabled_traces |= valid_traces[i].val;
                            break;
                        }
                    }
                    if (i == ARRAY_SIZE(valid_traces))
                        FATAL(1, "invalid tag: %s", tag);
                } while ((tag = strtok(NULL, ",")));
                break;
            case 'c':
                configure_pcap_output(ctxt, optarg);
                break;
            case 'w':
                invoke_wireshark(ctxt);
                break;
            case 'h':
                print_help(stdout, 0);
                break;
            case '?':
            default:
                print_help(stderr, 1);
                break;
        }
    }
    if (argc != optind + 2)
        print_help(stderr, 1);
    ctxt->rf_fd = rf_sim_connect(argv[optind + 1]);
    ctxt->uart.data_fd = pty_open(argv[optind + 0], 115200, false);
    ctxt->uart.trig_fd = ctxt->uart.data_fd;
    g_rf_host.rf_fd = ctxt->rf_fd;
#ifdef HAVE_LIBPCAP
    g_rf_host.pcap_dumper = ctxt->pcap_dumper;
#endif
    // The CCA looks at the neighbours the host pushed with
    // ns_fhss_ws_update_neighbor() (sl_wsrcp_mac.c).
    g_rf_host.neighbors = ctxt->neighbor_timings;
    g_rf_host.neighbors_count = ARRAY_SIZE(ctxt->neighbor_timings);
}

struct mac_api_s *init_mac_api(int rcp_driver_id)
{
    struct mac_api_s *rcp_mac_api;
    int ret;

    rcp_mac_api = ns_sw_mac_create(rcp_driver_id, &g_storage_sizes);
    BUG_ON(!rcp_mac_api);

    // Initialize SW MAC
    ret = rcp_mac_api->mac_initialize(rcp_mac_api,
                                            wsmac_mcps_data_confirm,
                                            wsmac_mcps_data_indication,
                                            wsmac_mcps_purge_confirm,
                                            wsmac_mlme_confirm,
                                            wsmac_mlme_indication,
                                            0); // Parent ID?
    WARN_ON(ret);

    ret = rcp_mac_api->mac_mcps_extension_enable(rcp_mac_api,
                                                       wsmac_mcps_data_indication_ext,
                                                       wsmac_mcps_data_confirm_ext,
                                                       wsmac_mcps_ack_data_req_ext);
    WARN_ON(ret);
    return rcp_mac_api;
}

void kill_handler(int signal)
{
    exit(3);
}

static void wsmac_rf_ready(void)
{
    phy_rf_rx_now();
}

static void wsmac_uart_ready(void)
{
    wsmac_rx_host(&g_ctxt);
}

static bool wsmac_uart_pending(void)
{
    return g_ctxt.uart.uart_next_frame_ready;
}

static bool wsmac_cca_in_progress(void)
{
    return g_ctxt.rf_frame_cca_progress;
}

int main(int argc, char *argv[])
{
    struct wsmac_ctxt *ctxt = &g_ctxt;

    signal(SIGINT, kill_handler);
    signal(SIGHUP, kill_handler);

    /* cpu_set_t my_set;
    CPU_ZERO(&my_set);
    CPU_SET(0, &my_set);
    sched_setaffinity(0, sizeof(cpu_set_t), &my_set); */

    mbed_trace_init();
    mbed_trace_config_set(TRACE_ACTIVE_LEVEL_ALL | TRACE_MODE_COLOR);
    platform_critical_init();
    ns_port_init();
    eventOS_scheduler_init();
    INFO("Silicon Labs Wi-SUN RCP simulation %s", version_fw_str);
    configure(ctxt, argc, argv);
    ctxt->rcp_driver_id = virtual_rf_device_register(PHY_LINK_15_4_SUBGHZ_TYPE, 2043);
    if (ctxt->rcp_driver_id < 0)
        tr_err("%s: arm_net_phy_register: %d", __func__, ctxt->rcp_driver_id);
    ctxt->rf_driver = arm_net_phy_driver_pointer(ctxt->rcp_driver_id);
    BUG_ON(!ctxt->rf_driver);
    arm_net_phy_mac64_set(ctxt->eui64, ctxt->rcp_driver_id);
    ctxt->rcp_mac_api = init_mac_api(ctxt->rcp_driver_id);

    wsmac_reset_ind(ctxt, true);
    // Same inputs, in the same order, as the loop that was here: the event
    // loop dispatches one event per iteration, so radio and UART input is
    // served in between.
    ns_port_posix_add_fd(ctxt->rf_fd, false, wsmac_rf_ready, NULL);
    ns_port_posix_add_fd(ctxt->uart.trig_fd, false, wsmac_uart_ready, wsmac_uart_pending);
    ns_port_posix_add_scheduler(true);
    ns_port_posix_set_busy(wsmac_cca_in_progress);
    ns_port_run();

    return 0;
}

